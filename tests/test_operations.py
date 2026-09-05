from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import secrets
import stat
from types import SimpleNamespace
from uuid import uuid4

from dotenv import dotenv_values
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from backend.app.config.settings import Settings
from backend.app.db import models as m
from backend.app.db.repositories.leases import acquire_lease, owns_lease
from backend.app.db.session import make_engine, make_session_factory
from backend.app.db.transport import validate_database_transport
from backend.app.db import readiness as database_readiness
from backend.app.scheduler import jobs, worker
from backend.app.scheduler.locking import LeaseLost, fenced_site_work, site_lease_key
from backend.app.services import control
from docker import entrypoint
from docker.entrypoint import database_url_from_environment
from scripts.bootstrap import bootstrap, migrate
from scripts.grant_runtime import grant_runtime_privileges, verify_runtime_role
from scripts.init_env import SECRET_NAMES, generate_env, main as env_main
import scripts.bootstrap as bootstrap_module
import scripts.deployment_preflight as deployment_preflight


def local_settings(tmp_path, **overrides):
    return Settings(_env_file=None, environment="test", database_url=f"sqlite:///{tmp_path / 'operations.sqlite'}",
                    agent_mode="fixture", provider_mode="fixture", **overrides)


def set_production_migration_pins(monkeypatch, *, head="0001_canonical"):
    for key, value in {
        "SEO_RELEASE_IMAGE": "sha256:" + "a" * 64,
        "SEO_MIGRATION_EXPECTED_DATABASE": "seo",
        "SEO_MIGRATION_EXPECTED_SYSTEM_IDENTIFIER": "1234567890",
        "SEO_MIGRATION_MODE": "upgrade",
        "SEO_MIGRATION_EXPECTED_SCHEMA_HEADS": head,
    }.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def operations_db(tmp_path):
    settings = local_settings(tmp_path)
    engine = make_engine(settings.database_url)
    m.Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        site = control.create_site(session, name="Operations fixture", base_url="https://example.test", fixture=True)
        site_id = site.id
    yield settings, factory, site_id
    engine.dispose()


def test_environment_generation_is_private_distinct_and_repeatable(tmp_path, capsys):
    target = tmp_path / ".env"
    assert env_main(["--path", str(target)]) == 0
    original = target.read_bytes()
    values = dotenv_values(target)
    credentials = [values[key] for key in SECRET_NAMES]
    assert all(value and len(value) >= 64 for value in credentials)
    assert len(set(credentials)) == len(SECRET_NAMES)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not generate_env(target)
    assert target.read_bytes() == original
    output = capsys.readouterr().out
    assert all(value not in output for value in credentials)


def test_existing_environment_is_not_merged_or_overwritten(tmp_path):
    target = tmp_path / ".env"
    original = b"# operator-owned\nCUSTOM=keep-every-byte\nAPI_TOKEN=already-configured\n"
    target.write_bytes(original)
    assert not generate_env(target)
    assert target.read_bytes() == original


def test_demo_bootstrap_twice_preserves_canonical_history(tmp_path):
    settings = local_settings(tmp_path)
    first = bootstrap(settings, demo=True)
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    with factory() as session:
        initial = {model.__tablename__: session.scalar(select(func.count()).select_from(model))
                   for model in (m.Site, m.Page, m.Action, m.Evidence, m.JobRun, m.StrategyVersion)}
        site = session.get(m.Site, first["site_id"])
        assert site.config_json["source_mode"] == "fixture"
        assert "demo" in site.name.lower() and "example.test" in site.name
        assert not site.production_enabled and site.autonomy_level == 1
        assert not site.conversion_definition.get("verified")
        assert initial["pages"] >= 3 and initial["evidence"] > 0
    second = bootstrap(settings, demo=True)
    with factory() as session:
        repeated = {model.__tablename__: session.scalar(select(func.count()).select_from(model))
                    for model in (m.Site, m.Page, m.Action, m.Evidence, m.JobRun, m.StrategyVersion)}
        assert session.scalar(text("SELECT version_num FROM alembic_version"))
    engine.dispose()
    assert first["status"] == "created" and second["status"] == "existing"
    assert first["site_id"] == second["site_id"] and first["job_id"] == second["job_id"]
    assert first["cycle_status"] == second["cycle_status"] == "completed"
    assert repeated == initial


def test_real_registration_requires_name_and_performs_no_observation(tmp_path, monkeypatch):
    settings = local_settings(tmp_path)
    with pytest.raises(ValueError, match="explicit --name"):
        bootstrap(settings, domain="business.example.com")
    assert not (tmp_path / "operations.sqlite").exists()

    def forbidden(*args, **kwargs):
        raise AssertionError("Real registration must not ingest, call agents or publish")

    monkeypatch.setattr(control, "run_cycle", forbidden)
    monkeypatch.setattr(control, "ingest_site", forbidden)
    result = bootstrap(settings, domain="business.example.com", name="Configured business")
    again = bootstrap(settings, domain="https://business.example.com/", name="Ignored replacement")
    engine = make_engine(settings.database_url)
    with make_session_factory(engine)() as session:
        site = session.get(m.Site, result["site_id"])
        assert site.name == "Configured business"
        assert site.config_json["source_mode"] == "live"
        assert site.autonomy_level == 1 and site.production_enabled is False
        for model in (m.Page, m.Evidence, m.AgentRun, m.JobRun):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    engine.dispose()
    assert result["site_id"] == again["site_id"]


@pytest.mark.parametrize("domain", ["http://business.example.com", "https://business.example.com/path", "https://127.0.0.1"])
def test_invalid_real_origin_does_not_migrate(tmp_path, domain):
    with pytest.raises(ValueError):
        bootstrap(local_settings(tmp_path), domain=domain, name="Business")
    assert not (tmp_path / "operations.sqlite").exists()


def test_cadence_uses_configured_timezone_and_period_keys(tmp_path):
    settings = local_settings(tmp_path, scheduler_timezone="America/New_York")
    schedule = worker.describe_schedule(settings)
    assert [(row["job"], row["local_time"], row["days"]) for row in schedule] == [
        (jobs.DAILY_CYCLE, "05:00", "daily"), (jobs.INTEGRITY_CRAWL, "12:00", "daily"),
        (jobs.EVENING_MEASUREMENT, "19:00", "daily"), (jobs.WEEKLY_REVIEW, "06:00", "mon"),
    ]
    instant = datetime(2026, 9, 1, 2, tzinfo=timezone.utc)
    assert jobs.period_key(jobs.DAILY_CYCLE, instant, settings.scheduler_timezone).endswith("2026-08-31")
    assert jobs.period_key(jobs.WEEKLY_REVIEW, instant + timedelta(days=3), settings.scheduler_timezone).endswith("2026-08-31")
    assert [row["may_call_models"] for row in schedule] == [True, False, False, False]
    scheduler = worker.build_scheduler(None, settings, startup_catchup=False)
    next_daily = scheduler.get_job(jobs.DAILY_CYCLE).trigger.get_next_fire_time(None, instant)
    assert next_daily.hour == 5 and str(next_daily.tzinfo) == "America/New_York"
    assert {job.id for job in scheduler.get_jobs()} == set(jobs.JOB_NAMES) | {"worker-heartbeat"}


def test_restart_catchup_is_bounded_to_current_day_and_week(tmp_path):
    settings = local_settings(tmp_path, scheduler_timezone="UTC")
    instant = datetime(2026, 9, 2, 14, tzinfo=timezone.utc)
    slots = dict(worker.due_slots(settings, instant))
    assert set(slots) == {jobs.DAILY_CYCLE, jobs.INTEGRITY_CRAWL, jobs.WEEKLY_REVIEW}
    assert slots[jobs.DAILY_CYCLE] == instant.replace(hour=5)
    assert slots[jobs.WEEKLY_REVIEW] == datetime(2026, 8, 31, 6, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="timezone-aware"):
        worker.due_slots(settings, datetime(2026, 9, 2))


@pytest.mark.parametrize("job_name", [jobs.INTEGRITY_CRAWL, jobs.EVENING_MEASUREMENT, jobs.WEEKLY_REVIEW])
def test_observation_jobs_are_durable_idempotent_and_never_call_specialists(operations_db, monkeypatch, job_name):
    settings, factory, site_id = operations_db

    def forbidden(*args, **kwargs):
        raise AssertionError("No paid specialist is allowed in observation jobs")

    monkeypatch.setattr(control, "run_specialists", forbidden)
    before = datetime(2026, 9, 1, 19, tzinfo=timezone.utc)
    first = jobs.run_observation_job(factory, site_id, job_name, settings, scheduled_for=before)
    second = jobs.run_observation_job(factory, site_id, job_name, settings, scheduled_for=before)
    assert first["status"] == second["status"] == "completed"
    assert first["job_id"] == second["job_id"] and second["idempotent_replay"]
    assert first["result"]["model_calls"] == 0 and first["result"]["production_mutations"] == 0
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(m.JobRun)) == 1
        assert session.scalar(select(func.count()).select_from(m.AgentRun)) == 0
        events = list(session.scalars(select(m.ActionEvent.event_type).where(
            m.ActionEvent.action_id == first["result"]["audit_action_id"]).order_by(m.ActionEvent.created_at)))
        assert events == ["scheduler_started", "scheduler_completed"]
        site = session.get(m.Site, site_id)
        assert site.autonomy_level == 1 and site.production_enabled is False
        assert session.scalar(select(func.count()).select_from(m.StrategyVersion)) == 1


def test_shared_site_lease_blocks_observation_without_duplicate_job(operations_db):
    settings, factory, site_id = operations_db
    with factory() as session, fenced_site_work(session, site_id, owner="api-cycle") as handle:
        assert handle and handle.key == f"site-cycle:{site_id}"
        result = jobs.run_observation_job(factory, site_id, jobs.INTEGRITY_CRAWL, settings)
        assert result["status"] == "lease_busy"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(m.JobRun)) == 0


def test_stale_site_worker_cannot_commit_after_takeover(operations_db):
    _, factory, site_id = operations_db
    with factory() as session, fenced_site_work(session, site_id, owner="old-worker") as original:
        with factory() as competitor:
            competitor.execute(update(m.JobLease).where(m.JobLease.key == site_lease_key(site_id))
                               .values(expires_at=m.utcnow() - timedelta(seconds=1)))
            competitor.commit()
            replacement = acquire_lease(competitor, site_lease_key(site_id), "new-worker", site_id=site_id)
            competitor.commit()
        assert replacement.fencing_token > original.fencing_token
        site = session.get(m.Site, site_id)
        site.name = "Stale write"
        with pytest.raises(LeaseLost):
            session.commit()
        session.rollback()
    with factory() as session:
        assert session.get(m.Site, site_id).name == "Operations fixture"
        assert owns_lease(session, replacement)


def test_failed_observation_records_safe_failure_and_retries_same_durable_job(operations_db, monkeypatch):
    settings, factory, site_id = operations_db
    original = jobs.OPERATIONS[jobs.INTEGRITY_CRAWL]

    def fail(*args):
        raise RuntimeError("credential-value-must-never-be-recorded")

    monkeypatch.setitem(jobs.OPERATIONS, jobs.INTEGRITY_CRAWL, fail)
    failed = jobs.run_observation_job(factory, site_id, jobs.INTEGRITY_CRAWL, settings)
    assert failed["status"] == "failed"
    with factory() as session:
        run = session.get(m.JobRun, failed["job_id"])
        assert run.error == "RuntimeError"
        failure = session.scalar(select(m.FailureCase))
        assert failure and "credential-value" not in str(control.serialise(failure))
    monkeypatch.setitem(jobs.OPERATIONS, jobs.INTEGRITY_CRAWL, original)
    retried = jobs.run_observation_job(factory, site_id, jobs.INTEGRITY_CRAWL, settings)
    assert retried["status"] == "completed" and retried["job_id"] == failed["job_id"]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(m.JobRun)) == 1
        assert session.scalar(select(func.count()).select_from(m.ActionEvent).where(
            m.ActionEvent.event_type == "scheduler_started")) == 2


def test_disabled_worker_does_not_open_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(worker, "load_worker_settings", lambda: local_settings(tmp_path, scheduler_enabled=False))
    assert worker.main([]) == 2
    assert not (tmp_path / "operations.sqlite").exists()
    assert "Worker disabled" in capsys.readouterr().out


def test_worker_heartbeat_has_bounded_age(tmp_path):
    path = tmp_path / "heartbeat.json"
    assert not worker.heartbeat_healthy(path)
    worker.write_heartbeat(path)
    assert worker.heartbeat_healthy(path)
    stale = m.utcnow().timestamp() - 120
    os.utime(path, (stale, stale))
    assert not worker.heartbeat_healthy(path)


def test_worker_health_requires_database_and_current_migration_head(tmp_path):
    path = tmp_path / "heartbeat.json"
    settings = local_settings(tmp_path)
    migrate(settings.database_url)
    worker.write_heartbeat(path)
    assert worker.worker_healthy(settings, path)
    engine = make_engine(settings.database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num='stale_release'"))
    finally:
        engine.dispose()
    assert not worker.worker_healthy(settings, path)


def test_production_privilege_readiness_cache_is_bounded_and_caches_failures(tmp_path, monkeypatch):
    from scripts import grant_runtime

    url = f"sqlite:///{tmp_path / 'readiness-cache.sqlite3'}"
    migrate(url, environment="test")
    engine = make_engine(url, environment="test")
    calls = []
    database_readiness.clear_privilege_readiness_cache()
    try:
        monkeypatch.setattr(grant_runtime, "verify_runtime_role",
                            lambda _connection, *, profile: calls.append(profile))
        with engine.connect() as connection:
            for _ in range(2):
                database_readiness.verify_database_readiness(
                    connection, environment="production", profile="api", privilege_cache_seconds=30,
                )
        assert calls == ["api"]

        database_readiness.clear_privilege_readiness_cache()

        def rejected(_connection, *, profile):
            calls.append(profile)
            raise ValueError("drift")

        monkeypatch.setattr(grant_runtime, "verify_runtime_role", rejected)
        with engine.connect() as connection:
            for _ in range(2):
                with pytest.raises(ValueError):
                    database_readiness.verify_database_readiness(
                        connection, environment="production", profile="api", privilege_cache_seconds=30,
                    )
        assert calls == ["api", "api"]
    finally:
        database_readiness.clear_privilege_readiness_cache()
        engine.dispose()


def test_daily_dispatch_preserves_core_config_and_takes_no_second_lease(operations_db, monkeypatch):
    settings, factory, site_id = operations_db
    now = datetime(2026, 9, 1, 5, tzinfo=timezone.utc)
    received = []

    def fake_core(session, identifier, passed_settings, *, idempotency_key):
        assert passed_settings is settings
        with fenced_site_work(session, identifier, owner="test-core") as handle:
            assert handle is not None
            received.append(idempotency_key)
            return {"status": "completed"}

    monkeypatch.setattr(control, "run_cycle", fake_core)
    result = jobs.run_scheduled_job(factory, settings, jobs.DAILY_CYCLE, site_id=site_id, scheduled_for=now)
    assert result[0]["status"] == "completed"
    assert received == ["scheduled:daily-cycle:2026-09-01"]


def test_runtime_database_url_handles_reserved_password_characters(monkeypatch):
    password = "p@ss:/?#$'word" * 3
    for key, value in {"POSTGRES_HOST": "db", "POSTGRES_PORT": "5432", "POSTGRES_DB": "seo",
                       "POSTGRES_USER": "seo_app", "POSTGRES_PASSWORD": password,
                       "DATABASE_URL": "postgresql://must-not-be-used/owner"}.items():
        monkeypatch.setenv(key, value)
    parsed = make_url(database_url_from_environment())
    assert parsed.username == "seo_app" and parsed.password == password
    assert parsed.host == "db" and parsed.database == "seo"


@pytest.mark.parametrize("sslmode", [None, "disable", "require", "verify-ca"])
def test_remote_production_database_url_fails_before_engine_creation(monkeypatch, sslmode):
    from backend.app.db import session as db_session

    url = "postgresql+psycopg://seo@database.example.test/seo"
    if sslmode:
        url += f"?sslmode={sslmode}"
    attempted = []
    monkeypatch.setattr(db_session, "create_engine", lambda *_args, **_kwargs: attempted.append(True))
    with pytest.raises(ValueError, match="sslmode=verify-full"):
        db_session.make_engine(url, environment="production")
    assert attempted == []


@pytest.mark.parametrize("environment,error", [
    (" production ", "sslmode=verify-full"), ("prod", "Database environment must be"),
])
def test_direct_engine_environment_cannot_bypass_policy_by_whitespace_or_alias(monkeypatch, environment, error):
    from backend.app.db import session as db_session

    attempted = []
    monkeypatch.setattr(db_session, "create_engine", lambda *_args, **_kwargs: attempted.append(True))
    with pytest.raises(ValueError, match=error):
        db_session.make_engine("postgresql+psycopg://seo@database.example.test/seo",
                               environment=environment)
    assert attempted == []


@pytest.mark.parametrize("query", [
    "host=database.example.test&sslmode=disable", "hostaddr=203.0.113.5&sslmode=disable",
    "service=unreviewed&sslmode=verify-full", "sslmode=verify-full&requiressl=0",
    "requiressl=0&sslmode=verify-full", "sslmode=verify-full&options=-csearchpath%3Devil",
])
def test_postgres_query_cannot_redirect_a_validated_local_production_url(monkeypatch, query):
    from backend.app.db import session as db_session

    attempted = []
    monkeypatch.setattr(db_session, "create_engine", lambda *_args, **_kwargs: attempted.append(True))
    with pytest.raises(ValueError, match="may not override connection identity"):
        db_session.make_engine(f"postgresql+psycopg://seo@db/seo?{query}", environment="production")
    assert attempted == []


@pytest.mark.parametrize("override", [
    {"sslmode": "disable"}, {"host": "other.example.test"}, {"service": "unreviewed"},
    {"sslrootcert": "/unreviewed/root.pem"}, {"ssl_min_protocol_version": "TLSv1"},
])
def test_engine_options_cannot_override_validated_production_connection(monkeypatch, override):
    from backend.app.db import session as db_session

    attempted = []
    monkeypatch.setattr(db_session, "create_engine", lambda *_args, **_kwargs: attempted.append(True))
    url = "postgresql+psycopg://seo@database.example.test/seo?sslmode=verify-full"
    with pytest.raises(ValueError, match="may not override"):
        db_session.make_engine(url, environment="production", connect_args=override)
    assert attempted == []


def test_sqlalchemy_url_plugin_is_rejected_before_production_engine_creation(monkeypatch):
    from backend.app.db import session as db_session

    attempted = []
    monkeypatch.setattr(db_session, "create_engine", lambda *_args, **_kwargs: attempted.append(True))
    url = ("postgresql+psycopg://seo@database.example.test/seo"
           "?sslmode=verify-full&plugin=unreviewed")
    with pytest.raises(ValueError, match="may not override connection identity"):
        db_session.make_engine(url, environment="production")
    assert attempted == []


def test_sqlalchemy_engine_plugins_are_rejected_before_production_engine_creation(monkeypatch):
    from backend.app.db import session as db_session

    attempted = []
    monkeypatch.setattr(db_session, "create_engine", lambda *_args, **_kwargs: attempted.append(True))
    url = "postgresql+psycopg://seo@database.example.test/seo?sslmode=verify-full"
    with pytest.raises(ValueError, match="engine options may not override"):
        db_session.make_engine(url, environment="production", plugins=[])
    assert attempted == []


def test_custom_pool_is_rejected_before_production_engine_creation(monkeypatch):
    from backend.app.db import session as db_session

    attempted = []
    monkeypatch.setattr(db_session, "create_engine", lambda *_args, **_kwargs: attempted.append(True))
    url = "postgresql+psycopg://seo@database.example.test/seo?sslmode=verify-full"
    with pytest.raises(ValueError, match="engine options may not override"):
        db_session.make_engine(url, environment="production", poolclass=object)
    assert attempted == []


def test_nested_psycopg_conninfo_override_is_rejected_before_engine_creation(monkeypatch):
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg
    from backend.app.db import session as db_session

    url = ("postgresql+psycopg://seo@database.example.test/seo?sslmode=verify-full"
           "&conninfo=hostaddr%3D203.0.113.9%20options%3D-csearch_path%3Devil")
    _, effective = PGDialect_psycopg().create_connect_args(make_url(url))
    nested = effective.pop("conninfo")
    params = conninfo_to_dict(make_conninfo(nested, **effective))
    assert params["hostaddr"] == "203.0.113.9"
    assert params["options"] == "-csearch_path=evil"

    attempted = []
    monkeypatch.setattr(db_session, "create_engine", lambda *_args, **_kwargs: attempted.append(True))
    with pytest.raises(ValueError, match="may not override connection identity"):
        db_session.make_engine(url, environment="production")
    assert attempted == []


@pytest.mark.parametrize("query", [
    "autocommit=false", "row_factory=hostile", "application_name=unreviewed",
])
def test_psycopg_wrapper_and_unreviewed_query_parameters_fail_closed(monkeypatch, query):
    from backend.app.db import session as db_session

    attempted = []
    monkeypatch.setattr(db_session, "create_engine", lambda *_args, **_kwargs: attempted.append(True))
    url = f"postgresql+psycopg://seo@database.example.test/seo?sslmode=verify-full&{query}"
    with pytest.raises(ValueError, match="override connection identity|unsupported connection parameters"):
        db_session.make_engine(url, environment="production")
    assert attempted == []


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://database.example.test?sslmode=verify-full",
    "postgresql+psycopg://seo@database.example.test?sslmode=verify-full",
    "postgresql+psycopg://database.example.test/seo?sslmode=verify-full",
])
def test_production_database_identity_may_not_fall_back_to_libpq_defaults(monkeypatch, url):
    from backend.app.db import session as db_session

    attempted = []
    monkeypatch.setattr(db_session, "create_engine", lambda *_args, **_kwargs: attempted.append(True))
    with pytest.raises(ValueError, match="explicit database username and database name"):
        db_session.make_engine(url, environment="production")
    assert attempted == []


def test_remote_entrypoint_url_requires_and_preserves_verified_tls(monkeypatch):
    values = {"ENVIRONMENT": "production", "POSTGRES_HOST": "database.example.test",
              "POSTGRES_PORT": "5432", "POSTGRES_DB": "seo", "POSTGRES_USER": "seo_app",
              "POSTGRES_PASSWORD": "not-printed"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("POSTGRES_SSLMODE", raising=False)
    with pytest.raises(ValueError, match="sslmode=verify-full"):
        database_url_from_environment()
    monkeypatch.setenv("POSTGRES_SSLMODE", "verify-full")
    parsed = make_url(database_url_from_environment())
    assert parsed.query["sslmode"] == "verify-full" and parsed.query["gssencmode"] == "disable"


def test_transport_policy_accepts_local_compose_and_verified_remote_postgres():
    local = "postgresql+psycopg://seo@db/seo"
    remote = "postgresql+psycopg://seo@database.example.test/seo?sslmode=verify-full"
    assert validate_database_transport(local, environment="production") == local
    validated = make_url(validate_database_transport(remote, environment="production"))
    assert validated.query["sslmode"] == "verify-full" and validated.query["gssencmode"] == "disable"


def test_production_engine_pins_search_path_during_driver_startup(monkeypatch):
    from backend.app.db import session as db_session

    observed = []
    fake_engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    monkeypatch.setattr(
        db_session,
        "create_engine",
        lambda url, **options: observed.append((url, options)) or fake_engine,
    )
    assert db_session.make_engine(
        "postgresql+psycopg://owner@db/seo", environment="production",
    ) is fake_engine
    assert observed[0][1]["connect_args"] == {"options": "-csearch_path=public"}


@pytest.mark.parametrize("gssencmode", ["prefer", "require"])
def test_remote_production_database_cannot_substitute_gss_encryption_for_tls(monkeypatch, gssencmode):
    from backend.app.db import session as db_session

    attempted = []
    monkeypatch.setattr(db_session, "create_engine", lambda *_args, **_kwargs: attempted.append(True))
    url = ("postgresql+psycopg://seo@database.example.test/seo?sslmode=verify-full"
           f"&gssencmode={gssencmode}")
    with pytest.raises(ValueError, match="gssencmode=disable"):
        db_session.make_engine(url, environment="production")
    assert attempted == []


def test_owner_migration_rejects_remote_tls_downgrade_before_alembic_or_driver(monkeypatch):
    from backend.app.db import session as db_session

    attempted = []
    monkeypatch.setattr(db_session, "create_engine", lambda *_args, **_kwargs: attempted.append(True))
    monkeypatch.setenv("ENVIRONMENT", "production")
    set_production_migration_pins(monkeypatch)
    with pytest.raises(ValueError, match="sslmode=verify-full"):
        migrate("postgresql+psycopg://owner@database.example.test/seo?sslmode=require")
    assert attempted == []


def test_migration_entrypoint_defaults_to_production_transport_policy(monkeypatch):
    for key, value in {"POSTGRES_HOST": "database.example.test", "POSTGRES_PORT": "5432",
                       "POSTGRES_DB": "seo", "POSTGRES_USER": "owner", "POSTGRES_PASSWORD": "not-printed"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("POSTGRES_SSLMODE", raising=False)
    monkeypatch.setenv("SERVICE_ROLE", "api")
    set_production_migration_pins(monkeypatch)
    with pytest.raises(ValueError, match="sslmode=verify-full"):
        entrypoint.main(["migrate"])
    assert "ENVIRONMENT" not in os.environ


@pytest.mark.parametrize("image", ["spiral-max-seo:local", "registry.test/seo:latest", ""])
def test_verification_migration_rejects_mutable_image_before_database_or_migrate(monkeypatch, image):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("VERIFICATION_ONLY", "true")
    monkeypatch.setenv("SEO_RELEASE_IMAGE", image)
    attempted = []
    monkeypatch.setattr(
        entrypoint,
        "database_url_from_environment",
        lambda **_kwargs: attempted.append("database-url") or "sqlite://",
    )
    monkeypatch.setattr(bootstrap_module, "migrate", lambda *_args, **_kwargs: attempted.append("migrate"))

    with pytest.raises(ValueError, match="immutable image digest"):
        entrypoint.main(["migrate"])

    assert attempted == []


@pytest.mark.parametrize("image", [
    "registry.test/seo@sha256:" + "a" * 64,
    "sha256:" + "b" * 64,
])
def test_verification_migration_accepts_exact_digest_before_migrate(monkeypatch, image):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("VERIFICATION_ONLY", "true")
    set_production_migration_pins(monkeypatch)
    monkeypatch.setenv("SEO_RELEASE_IMAGE", image)
    monkeypatch.setattr(
        entrypoint,
        "database_url_from_environment",
        lambda **_kwargs: "postgresql+psycopg://owner@db/seo",
    )
    calls = []

    def target_preflight(database_url, expected, *, environment):
        calls.append(("preflight", database_url, expected.database, environment))

    def intercepted_migrate(
        database_url, *, environment=None, expected_target=None, post_migration=None,
    ):
        calls.append(("migrate", database_url, expected_target.database, environment))
        assert post_migration is not None
        raise RuntimeError("migrate intercepted")

    monkeypatch.setattr(deployment_preflight, "preflight_migration_target", target_preflight)
    monkeypatch.setattr(bootstrap_module, "migrate", intercepted_migrate)
    with pytest.raises(RuntimeError, match="migrate intercepted"):
        entrypoint.main(["migrate"])

    assert calls == [
        ("preflight", "postgresql+psycopg://owner@db/seo", "seo", "production"),
        ("migrate", "postgresql+psycopg://owner@db/seo", "seo", "production"),
    ]


@pytest.mark.parametrize("environment", ["test", "development"])
def test_verification_migration_rejects_nonproduction_before_database_or_migrate(monkeypatch, environment):
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("VERIFICATION_ONLY", "true")
    set_production_migration_pins(monkeypatch)
    attempted = []
    monkeypatch.setattr(
        entrypoint,
        "database_url_from_environment",
        lambda **_kwargs: attempted.append("database-url") or "sqlite://",
    )
    monkeypatch.setattr(bootstrap_module, "migrate", lambda *_args, **_kwargs: attempted.append("migrate"))

    with pytest.raises(ValueError, match="Verification-only migration requires the production environment"):
        entrypoint.main(["migrate"])

    assert attempted == []


@pytest.mark.parametrize(("name", "value"), [
    ("SEO_MIGRATION_EXPECTED_DATABASE", ""),
    ("SEO_MIGRATION_EXPECTED_SYSTEM_IDENTIFIER", "not-an-identifier"),
    ("SEO_MIGRATION_MODE", "initial"),
    ("SEO_MIGRATION_EXPECTED_SCHEMA_HEADS", "uninitialized"),
])
def test_verification_migration_rejects_invalid_target_pins_before_database(monkeypatch, name, value):
    pins = {
        "VERIFICATION_ONLY": "true",
        "SEO_RELEASE_IMAGE": "sha256:" + "a" * 64,
        "SEO_MIGRATION_EXPECTED_DATABASE": "seo",
        "SEO_MIGRATION_EXPECTED_SYSTEM_IDENTIFIER": "1234567890",
        "SEO_MIGRATION_MODE": "upgrade",
        "SEO_MIGRATION_EXPECTED_SCHEMA_HEADS": "0001_canonical",
    }
    pins[name] = value
    for key, configured in pins.items():
        monkeypatch.setenv(key, configured)
    attempted = []
    monkeypatch.setattr(
        entrypoint,
        "database_url_from_environment",
        lambda **_kwargs: attempted.append("database-url") or "sqlite://",
    )
    monkeypatch.setattr(bootstrap_module, "migrate", lambda *_args, **_kwargs: attempted.append("migrate"))

    with pytest.raises(ValueError):
        entrypoint.main(["migrate"])

    assert attempted == []


@pytest.mark.parametrize("value", ["TRUE", " true", "1"])
def test_malformed_verification_flag_fails_before_database(monkeypatch, value):
    monkeypatch.setenv("VERIFICATION_ONLY", value)
    attempted = []
    monkeypatch.setattr(
        entrypoint,
        "database_url_from_environment",
        lambda **_kwargs: attempted.append("database-url") or "sqlite://",
    )
    with pytest.raises(ValueError, match="exact literal"):
        entrypoint.main(["migrate"])
    assert attempted == []


def test_every_production_entrypoint_migration_requires_release_and_target_pins(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("VERIFICATION_ONLY", raising=False)
    for name in (
        "SEO_RELEASE_IMAGE",
        "SEO_MIGRATION_EXPECTED_DATABASE",
        "SEO_MIGRATION_EXPECTED_SYSTEM_IDENTIFIER",
        "SEO_MIGRATION_MODE",
        "SEO_MIGRATION_EXPECTED_SCHEMA_HEADS",
    ):
        monkeypatch.delenv(name, raising=False)
    attempted = []
    monkeypatch.setattr(
        entrypoint,
        "database_url_from_environment",
        lambda **_kwargs: attempted.append("database-url") or "sqlite://",
    )
    with pytest.raises(ValueError, match="immutable image digest"):
        entrypoint.main(["migrate"])
    assert attempted == []


@pytest.mark.parametrize("name", [
    "PGHOSTADDR", "PGOPTIONS", "PGREQUIRESSL", "PGSERVICE", "PGSERVICEFILE", "PGSSLMODE",
])
def test_production_engine_rejects_ambient_libpq_routing_overrides(monkeypatch, name):
    from backend.app.db import session as db_session

    monkeypatch.setenv(name, "host=unreviewed.example.test")
    attempted = []
    monkeypatch.setattr(db_session, "create_engine", lambda *_args, **_kwargs: attempted.append(True))
    with pytest.raises(ValueError, match="ambient connection overrides"):
        db_session.make_engine(
            "postgresql+psycopg://seo@database.example.test/seo?sslmode=verify-full",
            environment="production",
        )
    assert attempted == []


def test_migrate_only_reads_production_policy_from_selected_env_file(tmp_path, monkeypatch, capsys):
    from backend.app.db import session as db_session

    env_file = tmp_path / "production.env"
    env_file.write_text(
        "ENVIRONMENT=production\n"
        "DATABASE_URL=postgresql+psycopg://owner:not-printed@database.example.test/seo?sslmode=require\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    attempted = []
    monkeypatch.setattr(db_session, "create_engine", lambda *_args, **_kwargs: attempted.append(True))
    assert bootstrap_module.main(["--migrate-only", "--env-file", str(env_file)]) == 1
    assert attempted == [] and "not-printed" not in capsys.readouterr().out


def test_worker_entrypoint_forces_non_bearer_service_role(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///entrypoint-test.sqlite3")
    monkeypatch.setenv("SERVICE_ROLE", "api")
    gates = []

    def stop_exec(*_):
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(entrypoint, "verify_database_role", lambda profile="api": gates.append(profile))
    monkeypatch.setattr(entrypoint.os, "execv", stop_exec)
    with pytest.raises(RuntimeError, match="exec intercepted"):
        entrypoint.main(["worker"])
    assert os.environ["SERVICE_ROLE"] == "worker"
    assert gates == ["worker"]


def test_sqlite_is_not_treated_as_a_postgres_permissions_gate(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'roles.sqlite'}")
    with engine.connect() as connection, pytest.raises(ValueError, match="PostgreSQL"):
        verify_runtime_role(connection)
    engine.dispose()


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="Actual PostgreSQL role gate requires TEST_POSTGRES_URL")
def test_postgres_runtime_role_cannot_control_schema_or_immutable_records():
    """Dedicated real test database; verify grants with an actual restricted login."""
    identifier = uuid4().hex[:16]
    database, role = f"ops_db_{identifier}", f"ops_app_{identifier}"
    password = secrets.token_hex(32)
    owner_url = make_url(os.environ["TEST_POSTGRES_URL"])
    owner_engine = make_engine(owner_url.render_as_string(hide_password=False))
    test_url = owner_url.set(database=database).render_as_string(hide_password=False)
    migration_engine, runtime_engine = None, None
    try:
        with owner_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'CREATE DATABASE "{database}"'))
        migrate(test_url)
        migration_engine = make_engine(test_url)
        with migration_engine.begin() as connection:
            grant_runtime_privileges(connection, role=role, password=password)
        # Repeat provisioning, including revocation of a historical column grant.
        with migration_engine.begin() as connection:
            connection.execute(text(f'GRANT UPDATE (source) ON evidence TO "{role}"'))
        with migration_engine.connect() as connection, pytest.raises(ValueError, match="forbidden"):
            verify_runtime_role(connection, role=role)
        with migration_engine.begin() as connection:
            grant_runtime_privileges(connection, role=role, password=password)
        runtime_url = owner_url.set(database=database, username=role, password=password).render_as_string(hide_password=False)
        runtime_engine = make_engine(runtime_url)
        with runtime_engine.connect() as connection:
            verify_runtime_role(connection)
        with make_session_factory(runtime_engine)() as session:
            site = control.create_site(session, name="Runtime permissions test", base_url="https://example.test", fixture=True)
            session.add(m.Evidence(site_id=site.id, source="fixture:test", source_type="test", content={}, is_fixture=True))
            session.commit()
        forbidden = [
            "UPDATE evidence SET source='tampered'", "DELETE FROM evidence", "TRUNCATE evidence CASCADE",
            "ALTER TABLE evidence DISABLE TRIGGER ALL", "DROP TRIGGER evidence_immutable ON evidence",
            "SET session_replication_role=replica",
            "CREATE TABLE bypass_table (id integer)", "CREATE SCHEMA bypass_schema", "CREATE TEMP TABLE bypass_temp (id integer)",
            "UPDATE alembic_version SET version_num='forged'", f'SET ROLE "{owner_url.username}"',
        ]
        with runtime_engine.connect() as connection:
            for statement in forbidden:
                with pytest.raises(DBAPIError):
                    connection.execute(text(statement))
                connection.rollback()
        with migration_engine.connect() as connection, pytest.raises(ValueError, match="authority"):
            verify_runtime_role(connection)
    finally:
        if runtime_engine is not None:
            runtime_engine.dispose()
        if migration_engine is not None:
            migration_engine.dispose()
        with owner_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
            connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        owner_engine.dispose()
