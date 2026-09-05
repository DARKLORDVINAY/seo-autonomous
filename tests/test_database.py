from __future__ import annotations

import io
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from backend.app.db.models import (
    APPEND_ONLY_TABLES, Action, ActionEvent, Approval, Base, Evidence, GA4Daily,
    ImmutableRecordError, Page, PageVersion, Revision, Site, Verification, utcnow,
)
from backend.app.db.repositories.canonical import get_page
from backend.app.db.repositories.leases import acquire_lease, owns_lease, release_lease, renew_lease
from backend.app.db.session import make_engine, make_session_factory

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def session():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        yield session
    engine.dispose()


def site_and_page(session, suffix="a"):
    site = Site(name=f"Site {suffix}", base_url=f"https://{suffix}.example")
    session.add(site)
    session.flush()
    page = Page(site_id=site.id, url=f"https://{suffix}.example/home", external_id="1")
    session.add(page)
    session.flush()
    return site, page


def audit_rows(session):
    site, page = site_and_page(session)
    revision = Revision(site_id=site.id, page_id=page.id, kind="update_title", before_hash="a" * 64,
                        revision_hash="b" * 64, reason="Improve accuracy", created_by="agent:content")
    evidence = Evidence(site_id=site.id, source="https://a.example", source_type="crawl", content={"title": "Observed"}, confidence=1)
    session.add_all([revision, evidence])
    session.flush()
    action = Action(site_id=site.id, revision_id=revision.id, kind="update_title", risk="MEDIUM", actor="executor",
                    reason="approved test", idempotency_key="once")
    verification = Verification(site_id=site.id, revision_id=revision.id, revision_hash=revision.revision_hash,
                                verifier_id="independent:reviewer", verdict="PASS", confidence=.9,
                                independent=True, action_safe=True)
    approval = Approval(site_id=site.id, revision_id=revision.id, revision_hash=revision.revision_hash, approved_by="human:owner")
    session.add_all([action, verification, approval])
    session.flush()
    event = ActionEvent(site_id=site.id, action_id=action.id, event_type="proposed")
    version = PageVersion(site_id=site.id, page_id=page.id, action_id=action.id, version_number=1, content_hash="a" * 64)
    session.add_all([event, version])
    session.commit()
    return site, page, {"actions": action, "action_events": event, "evidence": evidence, "revisions": revision,
                        "verifications": verification, "approvals": approval, "page_versions": version}


def test_canonical_tables_and_sqlite_foreign_keys(session):
    expected = {"sites", "pages", "page_versions", "queries", "query_clusters", "gsc_daily", "ga4_daily",
                "serp_snapshots", "ai_search_snapshots", "crawl_snapshots", "crawl_issues", "opportunities",
                "tasks", "task_dependencies", "task_ownership", "agent_runs", "agent_findings", "claims",
                "evidence", "assumptions", "contradictions", "actions", "action_events", "action_batches",
                "experiments", "experiment_metrics", "failure_cases", "rollback_events", "policies",
                "guardrails", "strategy_versions", "calibration_records", "decision_logs", "mission_states"}
    assert expected <= set(inspect(session.get_bind()).get_table_names())
    assert session.scalar(text("PRAGMA foreign_keys")) == 1
    assert session.scalar(text("PRAGMA recursive_triggers")) == 1


def test_unknown_conversion_and_indexing_remain_unknown(session):
    site, page = site_and_page(session)
    row = GA4Daily(site_id=site.id, page_id=page.id, date=date(2026, 1, 1), landing_page="/home", sessions=20)
    session.add(row)
    session.commit()
    assert row.qualified_conversions is None and row.conversion_value is None
    assert page.indexed_status == "unknown" and page.crawlable is None
    assert site.autonomy_level == 1 and site.production_enabled is False
    assert page.created_at.tzinfo is not None


def test_tenant_reference_cannot_cross_sites(session):
    site_a, page_a = site_and_page(session, "a")
    site_b, _ = site_and_page(session, "b")
    session.commit()
    with pytest.raises(LookupError):
        get_page(session, site_b.id, page_a.id)
    session.add(PageVersion(site_id=site_b.id, page_id=page_a.id, version_number=1, content_hash="hash"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_json_defaults_are_independent(session):
    a, _ = site_and_page(session, "a")
    b, _ = site_and_page(session, "b")
    assert a.config_json is not b.config_json


def test_immutable_orm_update_and_delete(session):
    _, _, rows = audit_rows(session)
    evidence = rows["evidence"]
    evidence.source = "rewritten"
    with pytest.raises(ImmutableRecordError):
        session.flush()
    session.rollback()
    session.delete(evidence)
    with pytest.raises(ImmutableRecordError):
        session.flush()


@pytest.mark.parametrize("table", ["actions", "action_events", "evidence", "revisions", "verifications", "approvals", "page_versions"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_bulk_sql_cannot_bypass_audit_immutability(session, table, operation):
    _, _, rows = audit_rows(session)
    statement = f'UPDATE "{table}" SET id = id WHERE id = :id' if operation == "UPDATE" else f'DELETE FROM "{table}" WHERE id = :id'
    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(text(statement), {"id": rows[table].id})
    session.rollback()


@pytest.mark.parametrize("table", ["actions", "action_events", "evidence", "revisions", "verifications", "approvals", "page_versions"])
def test_sqlite_implicit_replace_deletion_cannot_bypass_audit_immutability(session, table):
    _, _, rows = audit_rows(session)
    statement = f'INSERT OR REPLACE INTO "{table}" SELECT * FROM "{table}" WHERE id = :id'
    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(text(statement), {"id": rows[table].id})
    session.rollback()


def test_verification_is_bound_to_actual_revision_hash(session):
    site, _, rows = audit_rows(session)
    session.add(Verification(site_id=site.id, revision_id=rows["revisions"].id, revision_hash="forged",
                             verifier_id="verifier", verdict="PASS", confidence=1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_idempotency_key_is_unique_per_site(session):
    site, _, _ = audit_rows(session)
    session.add(Action(site_id=site.id, kind="update_title", risk="MEDIUM", actor="x", reason="duplicate", idempotency_key="once"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_expiring_lease_recovery_fences_stale_worker(session):
    now = utcnow()
    first = acquire_lease(session, "observation", "worker-1", ttl_seconds=10, now=now)
    assert first
    session.commit()
    assert acquire_lease(session, "observation", "worker-2", now=now) is None
    session.commit()
    second = acquire_lease(session, "observation", "worker-2", ttl_seconds=10, now=now + timedelta(seconds=11))
    assert second and second.fencing_token == first.fencing_token + 1
    session.commit()
    assert not owns_lease(session, first, now=now + timedelta(seconds=12))
    assert renew_lease(session, first, now=now + timedelta(seconds=12)) is None
    assert not release_lease(session, first, now=now + timedelta(seconds=12))
    assert owns_lease(session, second, now=now + timedelta(seconds=12))
    assert release_lease(session, second, now=now + timedelta(seconds=12))
    session.commit()
    third = acquire_lease(session, "observation", "worker-3", now=now + timedelta(seconds=13))
    assert third and third.fencing_token == 3


def test_lease_key_cannot_be_taken_by_another_site(session):
    a, _ = site_and_page(session, "a")
    b, _ = site_and_page(session, "b")
    now = utcnow()
    assert acquire_lease(session, "scope", "a", site_id=a.id, ttl_seconds=1, now=now)
    session.commit()
    assert acquire_lease(session, "scope", "b", site_id=b.id, now=now + timedelta(seconds=2)) is None


def test_concurrent_workers_only_one_acquires_lease(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'leases.sqlite'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    def claim(worker):
        with factory() as session:
            result = acquire_lease(session, "one-job", str(worker))
            session.commit()
            return result
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(claim, range(4)))
    assert sum(result is not None for result in results) == 1
    engine.dispose()


def test_migration_matches_model_and_installs_triggers(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'migrate.sqlite'}")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend/app/db/migrations"))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
        diffs = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert diffs == []
        triggers = connection.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'" )).scalars().all()
        assert len(triggers) == 2 * len(APPEND_ONLY_TABLES)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002_runtime_role_split"
    engine.dispose()


def test_postgresql_migration_compiles_offline(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    stream = io.StringIO()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=stream)
    config.set_main_option("script_location", str(ROOT / "backend/app/db/migrations"))
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://unconnected/seo")
    command.upgrade(config, "head", sql=True)
    sql = stream.getvalue()
    assert "CREATE TABLE sites" in sql and "TIMESTAMP WITH TIME ZONE" in sql
    assert "CREATE TRIGGER" in sql and "BEFORE TRUNCATE" in sql
    assert "FOREIGN KEY(revision_id, site_id, revision_hash)" in sql


def test_direct_alembic_rejects_remote_production_tls_downgrade(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend/app/db/migrations"))
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://owner@database.example.test/seo?sslmode=require",
    )
    with pytest.raises(ValueError, match="sslmode=verify-full"):
        command.upgrade(config, "head", sql=True)


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="Live Postgres gate requires TEST_POSTGRES_URL")
def test_postgresql_live_migration_audit_and_leases():
    """Run in CI against actual PostgreSQL; never claim SQLite substitutes for it."""
    from scripts.bootstrap import migrate

    migrate(os.environ["TEST_POSTGRES_URL"], environment="test")
    engine = make_engine(os.environ["TEST_POSTGRES_URL"])
    with engine.connect() as connection:
        factory = make_session_factory(connection)
        with factory() as session:
            _, _, rows = audit_rows(session)
            assert rows["evidence"].created_at.tzinfo is not None
            with pytest.raises(DBAPIError, match="append-only"):
                session.execute(text("UPDATE evidence SET source='tampered'"))
            session.rollback()
            with pytest.raises(DBAPIError, match="append-only"):
                session.execute(text("TRUNCATE evidence CASCADE"))
            session.rollback()
            first = acquire_lease(session, "job:" + uuid4().hex, "worker-1")
            session.commit()
            assert first
    engine.dispose()


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="Live Postgres gate requires TEST_POSTGRES_URL")
def test_direct_production_alembic_pins_hostile_default_and_persists(monkeypatch):
    """A fresh disposable database proves direct Alembic's target and transaction gates."""
    owner_url = make_url(os.environ["TEST_POSTGRES_URL"])
    database = "migration_gate_" + uuid4().hex[:16]
    owner_engine = make_engine(owner_url.render_as_string(hide_password=False), environment="test")
    target_engine = None
    try:
        with owner_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            system_identifier = connection.scalar(text(
                "SELECT system_identifier::text FROM pg_catalog.pg_control_system()"
            ))
            connection.execute(text(f'CREATE DATABASE "{database}"'))
        target_url = owner_url.set(database=database).render_as_string(hide_password=False)
        target_engine = make_engine(target_url, environment="test")
        with target_engine.begin() as connection:
            connection.execute(text("CREATE SCHEMA hostile"))
            connection.execute(text(
                "CREATE TABLE hostile.alembic_version (version_num varchar(32) PRIMARY KEY)"
            ))
            connection.execute(text(
                "INSERT INTO hostile.alembic_version VALUES ('hostile_marker')"
            ))
        with owner_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'ALTER DATABASE "{database}" SET search_path TO hostile, public'))
        target_engine.dispose()
        target_engine = None

        for key, value in {
            "ENVIRONMENT": "production",
            "DATABASE_URL": target_url,
            "SEO_RELEASE_IMAGE": "sha256:" + "a" * 64,
            "SEO_MIGRATION_EXPECTED_DATABASE": database,
            "SEO_MIGRATION_EXPECTED_SYSTEM_IDENTIFIER": system_identifier,
            "SEO_MIGRATION_MODE": "bootstrap",
            "SEO_MIGRATION_EXPECTED_SCHEMA_HEADS": "uninitialized",
        }.items():
            monkeypatch.setenv(key, value)
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "backend/app/db/migrations"))
        command.upgrade(config, "head")

        target_engine = make_engine(target_url, environment="test")
        with target_engine.connect() as connection:
            assert connection.scalar(text(
                "SELECT version_num FROM public.alembic_version"
            )) == "0002_runtime_role_split"
            assert connection.scalar(text(
                "SELECT version_num FROM hostile.alembic_version"
            )) == "hostile_marker"
    finally:
        if target_engine is not None:
            target_engine.dispose()
        with owner_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(
                "SELECT pg_catalog.pg_terminate_backend(pid) FROM pg_catalog.pg_stat_activity "
                "WHERE datname = :database AND pid <> pg_catalog.pg_backend_pid()"
            ), {"database": database})
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database}"'))
        owner_engine.dispose()
