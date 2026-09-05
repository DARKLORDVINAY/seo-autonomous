"""Deterministic checks for the API/worker PostgreSQL capability split."""
from __future__ import annotations

import inspect
import os
from pathlib import Path
import secrets
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from backend.app.config.settings import Settings
from backend.app.contracts import CMSPage, GA4Row, GSCRow
from backend.app.db import models as m
from backend.app.db.models import APPEND_ONLY_TABLES, Base
from backend.app.db.session import make_engine, make_session_factory
from backend.app.integrations.common import ObservationBatch
from backend.app.scheduler import jobs, worker as scheduler_worker
from backend.app.services.measurement import evaluate_due_experiments
from backend.app.services import control
from scripts.bootstrap import migrate
from scripts.grant_runtime import (
    WORKER_INSERT_DELETE_TABLES,
    WORKER_INSERT_ONLY_TABLES,
    WORKER_INSERT_UPDATE_TABLES,
    WORKER_COLUMN_PRIVILEGES,
    WORKER_READ_ONLY_TABLES,
    grant_runtime_privileges,
    provision_runtime_roles,
    table_privileges,
    verify_runtime_role,
)


def test_worker_table_inventory_is_total_disjoint_and_explicit():
    groups = (
        WORKER_READ_ONLY_TABLES,
        WORKER_INSERT_ONLY_TABLES,
        WORKER_INSERT_UPDATE_TABLES,
        WORKER_INSERT_DELETE_TABLES,
    )
    assert set().union(*groups) == set(Base.metadata.tables)
    assert sum(map(len, groups)) == len(set().union(*groups))


@pytest.mark.parametrize(
    "table",
    ["sites", "mission_states", "policies", "strategy_versions", "approvals", "verifications"],
)
def test_worker_cannot_issue_or_modify_canonical_authority(table):
    assert table_privileges("worker", table) == ("SELECT",)


def test_worker_operational_writes_are_narrower_than_api_writes():
    assert table_privileges("worker", "job_leases") == ("SELECT", "INSERT", "UPDATE")
    assert table_privileges("worker", "execution_leases") == ("SELECT", "INSERT", "DELETE")
    assert table_privileges("worker", "evidence") == ("SELECT", "INSERT")
    assert table_privileges("worker", "pages") == ("SELECT", "INSERT", "UPDATE")
    # Natural-key dimensions stay table-level insert-only. Provider lookback
    # refreshes receive separate, explicit non-key column UPDATE grants below.
    assert table_privileges("worker", "gsc_daily") == ("SELECT", "INSERT")
    assert table_privileges("worker", "ga4_daily") == ("SELECT", "INSERT")
    assert table_privileges("api", "sites") == ("SELECT", "INSERT", "UPDATE", "DELETE")
    assert table_privileges("api", "approvals") == ("SELECT", "INSERT")
    assert all(table_privileges("api", table) == ("SELECT", "INSERT") for table in APPEND_ONLY_TABLES)
    assert WORKER_COLUMN_PRIVILEGES == {
        "gsc_daily": {"UPDATE": frozenset({
            "page_id", "clicks", "impressions", "position", "data_state",
            "is_fixture", "quality_flags_json",
        })},
        "ga4_daily": {"UPDATE": frozenset({
            "page_id", "sessions", "key_events", "qualified_conversions",
            "conversion_value", "is_fixture", "quality_flags_json",
        })},
        "sites": {"UPDATE": frozenset({"coordination_token"})},
        "mission_states": {
            "UPDATE": frozenset({"available_resources_json", "blockers_json", "updated_at"}),
        },
    }


def test_unknown_profiles_and_tables_fail_closed():
    with pytest.raises(ValueError, match="profile"):
        table_privileges("owner", "sites")
    with pytest.raises(ValueError, match="Unknown canonical table"):
        table_privileges("worker", "future_table")


def test_runtime_role_verifier_statically_covers_cluster_and_direct_system_acls():
    source = inspect.getsource(verify_runtime_role)
    assert "other_database_connect" in source
    assert "other_database_temporary" in source
    assert "direct_system_object_acl" in source
    assert "pg_attribute" in source and "aclexplode" in source
    assert "application-dedicated PostgreSQL cluster" in source


def test_compose_initializes_an_application_dedicated_cluster():
    root = Path(__file__).resolve().parents[1]
    init_sql = (root / "docker/initdb/010-dedicated-cluster.sql").read_text(encoding="utf-8")
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert "REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC" in init_sql
    assert "WHERE datallowconn" in init_sql
    assert "010-dedicated-cluster.sql:/docker-entrypoint-initdb.d/010-dedicated-cluster.sql:ro" in compose


def test_direct_worker_forces_role_and_rejects_ambient_bearer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("SERVICE_ROLE", "api")
    for name in ("API_TOKEN", "APPROVAL_TOKEN", "ADMIN_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert scheduler_worker.load_worker_settings().service_role == "worker"
    monkeypatch.setenv("ADMIN_TOKEN", "x" * 40)
    with pytest.raises(ValueError, match="must not receive"):
        scheduler_worker.load_worker_settings()
    monkeypatch.delenv("ADMIN_TOKEN")
    monkeypatch.setenv("approval_token", "x" * 40)
    with pytest.raises(ValueError, match="must not receive"):
        scheduler_worker.load_worker_settings()


def test_direct_worker_health_builds_url_from_discrete_compose_credentials(tmp_path, monkeypatch):
    from docker import entrypoint

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for name in ("API_TOKEN", "APPROVAL_TOKEN", "ADMIN_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    expected = "postgresql+psycopg://seo_worker:not-printed@db/seo"
    observed = []

    def database_url_from_environment(*, environment=None):
        observed.append(environment)
        return expected

    monkeypatch.setattr(entrypoint, "database_url_from_environment", database_url_from_environment)
    settings = scheduler_worker.load_worker_settings()
    assert settings.service_role == "worker"
    assert settings.database_url == expected
    assert observed == ["production"]
    assert "DATABASE_URL" not in os.environ


def test_production_tick_rechecks_exact_worker_profile(monkeypatch):
    marker = object()
    observed = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def connection(self):
            return marker

    def verify(connection, *, environment, profile):
        observed.append((connection, environment, profile))

    monkeypatch.setattr("backend.app.db.readiness.verify_database_readiness", verify)
    settings = Settings(
        _env_file=None,
        environment="production",
        service_role="worker",
        database_url="postgresql+psycopg://seo_worker@db/seo",
    )
    jobs.verify_worker_tick(Session, settings)
    assert observed == [(marker, "production", "worker")]
    wrong = Settings(
        _env_file=None,
        environment="production",
        service_role="api",
        database_url="postgresql+psycopg://seo_api@db/seo",
        api_token="x" * 40,
    )
    with pytest.raises(ValueError, match="forced worker"):
        jobs.verify_worker_tick(Session, wrong)


def test_runtime_roles_and_passwords_must_be_distinct_before_sql():
    with pytest.raises(ValueError, match="roles must differ"):
        provision_runtime_roles(
            None,
            api_role="same_role",
            api_password="a" * 32,
            worker_role="same_role",
            worker_password="b" * 32,
        )
    with pytest.raises(ValueError, match="passwords must differ"):
        provision_runtime_roles(
            None,
            api_role="api_role",
            api_password="a" * 32,
            worker_role="worker_role",
            worker_password="a" * 32,
        )


def test_worker_measurement_recommends_but_does_not_modify_site_authority(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'worker-authority.sqlite3'}")
    Base.metadata.create_all(engine)
    try:
        with make_session_factory(engine)() as session:
            site = m.Site(
                name="Worker authority boundary",
                base_url="https://example.test",
                config_json={"earned_categories": ["update_title", "add_internal_link"]},
            )
            session.add(site)
            session.flush()
            for index in range(20):
                experiment = m.Experiment(
                    site_id=site.id,
                    name=f"Synthetic calibration {index}",
                    hypothesis="Prespecified failure",
                    status="closed",
                    baseline_start=m.utcnow().date() - timedelta(days=56),
                )
                session.add(experiment)
                session.flush()
                session.add(m.CalibrationRecord(
                    site_id=site.id,
                    experiment_id=experiment.id,
                    agent_name="content",
                    action_category="update_title",
                    predicted_confidence=0.99,
                    succeeded=False,
                    evaluable=True,
                    outcome_json={
                        "independent": True,
                        "is_primary_outcome": True,
                        "adjudication_source": str(uuid4()),
                    },
                ))
            session.commit()
            first = evaluate_due_experiments(session, site.id, authority_updates_allowed=False)
            second = evaluate_due_experiments(session, site.id, authority_updates_allowed=False)
            session.refresh(site)
            assert first["recommended_revocations"] == second["recommended_revocations"] == ["update_title"]
            assert first["revoked_categories"] == second["revoked_categories"] == []
            assert site.config_json["earned_categories"] == ["update_title", "add_internal_link"]
            recommendations = session.query(m.Action).filter_by(kind="recommend_autonomy_reduction").all()
            assert len(recommendations) == 1
            assert recommendations[0].payload_json["authority_update"] is False
    finally:
        engine.dispose()


def test_worker_verifier_output_is_preview_not_authoritative_row(tmp_path, monkeypatch):
    from backend.app.agents import runtime as agent_runtime
    from backend.app.services import agent_audit, execution

    engine = make_engine(f"sqlite:///{tmp_path / 'worker-verifier.sqlite3'}")
    Base.metadata.create_all(engine)
    blocked = []

    async def fake_draft(*_args, **_kwargs):
        return {
            "before_fingerprint": snapshot.fingerprint,
            "proposal": {
                "title": "Accurate example service title",
                "reason": "Clarifies the evidenced page topic",
                "evidence_ids": [evidence.id],
                "confidence": 0.8,
                "uncertainty": ["No outcome data yet"],
            },
        }

    async def fake_verifier(*_args, **_kwargs):
        return {"verification": {
            "verdict": "PASS",
            "verifier_id": "sceptical-verifier",
            "independent": True,
            "confidence": 0.8,
            "reasons": ["Synthetic bounded review"],
            "evidence_ids": [evidence.id],
            "alternative_explanations": ["No change may be needed"],
            "checks": {},
            "action_safe": True,
        }}

    def forbidden_record(*_args, **_kwargs):
        blocked.append(True)
        raise AssertionError("Worker must not call the authoritative verification writer")

    try:
        with make_session_factory(engine)() as session:
            site = m.Site(name="Worker verifier boundary", base_url="https://example.test", config_json={})
            session.add(site)
            session.flush()
            snapshot = CMSPage(
                external_id="page:1",
                url="https://example.test/service",
                title="Example service",
                content="A factual service description.",
            )
            page = m.Page(
                site_id=site.id,
                url=snapshot.url,
                external_id=snapshot.external_id,
                title=snapshot.title,
                content_html=snapshot.content,
                metadata_json={"cms_snapshot": snapshot.model_dump(mode="json")},
            )
            evidence = m.Evidence(
                site_id=site.id,
                source="fixture:worker-boundary",
                source_type="engineering_fixture",
                content={"page_topic": "example service"},
                owner="ingestion",
                confidence=1,
                is_fixture=True,
            )
            session.add_all([page, evidence])
            session.commit()
            monkeypatch.setattr(agent_runtime, "draft_metadata", fake_draft)
            monkeypatch.setattr(agent_runtime, "verify_proposal", fake_verifier)
            monkeypatch.setattr(agent_audit, "runtime_options", lambda *_args, **_kwargs: {})
            monkeypatch.setattr(execution, "record_verification", forbidden_record)
            result = control.propose_model_title(
                session,
                site,
                SimpleNamespace(page_id=page.id),
                {"kind": "test"},
                [{"id": evidence.id}],
                [],
                Settings(_env_file=None, environment="test", service_role="worker"),
                "task:test",
            )
            assert result["status"] == "awaiting_api_verification"
            assert result["authoritative_verification_recorded"] is False
            assert result["verification_preview"]["verdict"] == "PASS"
            assert blocked == []
            assert session.query(m.Revision).count() == 1
            assert session.query(m.Verification).count() == 0
    finally:
        engine.dispose()


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_URL"), reason="Actual role split requires TEST_POSTGRES_URL")
def test_actual_postgres_worker_cannot_write_site_authority_or_verdicts():
    """Use only a uniquely named disposable database and roles in CI."""
    token = uuid4().hex[:16]
    database = f"role_split_{token}"
    api_role, worker_role = f"role_api_{token}", f"role_worker_{token}"
    api_password, worker_password = secrets.token_hex(32), secrets.token_hex(32)
    owner_url = make_url(os.environ["TEST_POSTGRES_URL"])
    owner = make_engine(owner_url.render_as_string(hide_password=False))
    test_url = owner_url.set(database=database).render_as_string(hide_password=False)
    migration = api = worker = None
    try:
        with owner.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'CREATE DATABASE "{database}"'))
        migrate(test_url)
        migration = make_engine(test_url)
        with migration.begin() as connection:
            provision_runtime_roles(
                connection,
                api_role=api_role,
                api_password=api_password,
                worker_role=worker_role,
                worker_password=worker_password,
            )
        api = make_engine(owner_url.set(
            database=database, username=api_role, password=api_password,
        ).render_as_string(hide_password=False))
        worker = make_engine(owner_url.set(
            database=database, username=worker_role, password=worker_password,
        ).render_as_string(hide_password=False))
        with api.connect() as connection:
            verify_runtime_role(connection, profile="api")
            with pytest.raises(ValueError, match="forbidden"):
                verify_runtime_role(connection, profile="worker")
        with make_session_factory(api)() as session:
            site = m.Site(name="Role boundary fixture", base_url="https://example.test")
            session.add(site)
            session.flush()
            session.add(m.MissionState(site_id=site.id, objective="Synthetic role-boundary objective"))
            revision = m.Revision(
                site_id=site.id,
                kind="update_title",
                before_hash="a" * 64,
                revision_hash="b" * 64,
                reason="Synthetic role boundary",
                created_by="test-api",
            )
            session.add(revision)
            session.flush()
            session.add_all([
                m.Verification(
                    site_id=site.id,
                    revision_id=revision.id,
                    revision_hash=revision.revision_hash,
                    verifier_id="test-reviewer",
                    verdict="BLOCK",
                    confidence=1,
                ),
                m.Approval(
                    site_id=site.id,
                    revision_id=revision.id,
                    revision_hash=revision.revision_hash,
                    approved_by="test-reviewer",
                    decision="REJECT",
                    reason="Synthetic role boundary",
                ),
            ])
            session.commit()
            site_id, revision_id = site.id, revision.id
        with worker.connect() as connection:
            probes = []

            def record_probe(*_args):
                probes.append(True)

            event.listen(worker, "before_cursor_execute", record_probe)
            try:
                verify_runtime_role(connection, profile="worker")
            finally:
                event.remove(worker, "before_cursor_execute", record_probe)
            assert len(probes) <= 3
            assert connection.scalar(text("SELECT autonomy_level FROM sites WHERE id=:id"), {"id": site_id}) == 1
            assert connection.scalar(text("SELECT count(*) FROM approvals WHERE revision_id=:id"), {"id": revision_id}) == 1
            for statement, parameters in (
                ("UPDATE sites SET autonomy_level=5 WHERE id=:id", {"id": site_id}),
                ("INSERT INTO approvals (id) VALUES (:id)", {"id": str(uuid4())}),
                ("INSERT INTO verifications (id) VALUES (:id)", {"id": str(uuid4())}),
            ):
                with pytest.raises(DBAPIError):
                    connection.execute(text(statement), parameters)
                connection.rollback()
        # Historical table/column grants must make readiness fail until the
        # owner reapplies the exact profile.
        with migration.begin() as connection:
            connection.execute(text(f'GRANT INSERT ON approvals TO "{worker_role}"'))
            connection.execute(text(f'GRANT UPDATE (version_num) ON alembic_version TO "{worker_role}"'))
        with worker.connect() as connection, pytest.raises(ValueError, match="forbidden"):
            verify_runtime_role(connection, profile="worker")
        with migration.begin() as connection:
            grant_runtime_privileges(
                connection,
                role=worker_role,
                password=worker_password,
                profile="worker",
            )
        for drift in (
            f'ALTER ROLE "{worker_role}" NOLOGIN CONNECTION LIMIT 0 VALID UNTIL \'2000-01-01\'',
            f'REVOKE CONNECT ON DATABASE "{database}" FROM "{worker_role}"',
        ):
            with migration.begin() as connection:
                connection.execute(text(drift))
                with pytest.raises(ValueError):
                    verify_runtime_role(connection, role=worker_role, profile="worker")
                grant_runtime_privileges(
                    connection,
                    role=worker_role,
                    password=worker_password,
                    profile="worker",
                )
        # Executable GUC defaults must not survive reprovisioning. A replica
        # default would otherwise disable the canonical ordinary triggers.
        with migration.begin() as connection:
            connection.execute(text(f'ALTER ROLE "{worker_role}" SET session_replication_role=replica'))
            with pytest.raises(ValueError, match="only the pinned"):
                verify_runtime_role(connection, role=worker_role, profile="worker")
            grant_runtime_privileges(
                connection,
                role=worker_role,
                password=worker_password,
                profile="worker",
            )
        # Search paths do not constrain explicitly qualified objects. Even a
        # SECURITY DEFINER routine in another schema must be denied and found.
        with migration.begin() as connection:
            connection.execute(text("CREATE SCHEMA runtime_side_door"))
            connection.execute(text("""
                CREATE FUNCTION runtime_side_door.escalate() RETURNS integer
                LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'
            """))
            connection.execute(text(f'GRANT USAGE ON SCHEMA runtime_side_door TO "{worker_role}"'))
            connection.execute(text(
                f'GRANT EXECUTE ON FUNCTION runtime_side_door.escalate() TO "{worker_role}"',
            ))
            with pytest.raises(ValueError, match="other non-system schemas|non-system sequences or routines"):
                verify_runtime_role(connection, role=worker_role, profile="worker")
            grant_runtime_privileges(
                connection,
                role=worker_role,
                password=worker_password,
                profile="worker",
            )
            connection.execute(text("DROP SCHEMA runtime_side_door CASCADE"))
        with migration.begin() as connection:
            connection.execute(text("CREATE VIEW unexpected_runtime_view AS SELECT id FROM sites"))
            connection.execute(text(f'GRANT SELECT ON unexpected_runtime_view TO "{worker_role}"'))
            with pytest.raises(ValueError, match="forbidden"):
                verify_runtime_role(connection, role=worker_role, profile="worker")
            grant_runtime_privileges(
                connection,
                role=worker_role,
                password=worker_password,
                profile="worker",
            )
            connection.execute(text("DROP VIEW unexpected_runtime_view"))
        # A grant that merely duplicates ordinary PUBLIC catalogue access is
        # still a role-specific capability and must be rejected. PUBLIC itself
        # remains untouched.
        with migration.begin() as connection:
            connection.execute(text(f'GRANT SELECT ON pg_catalog.pg_class TO "{worker_role}"'))
            with pytest.raises(ValueError, match="direct ACL"):
                verify_runtime_role(connection, role=worker_role, profile="worker")
            connection.execute(text(f'REVOKE SELECT ON pg_catalog.pg_class FROM "{worker_role}"'))
            connection.execute(text(
                f'GRANT SELECT (relname) ON pg_catalog.pg_class TO "{worker_role}"',
            ))
            with pytest.raises(ValueError, match="direct ACL"):
                verify_runtime_role(connection, role=worker_role, profile="worker")
            connection.execute(text(
                f'REVOKE SELECT (relname) ON pg_catalog.pg_class FROM "{worker_role}"',
            ))
            connection.execute(text(
                f'GRANT EXECUTE ON FUNCTION pg_catalog.current_database() TO "{worker_role}"',
            ))
            with pytest.raises(ValueError, match="direct ACL"):
                verify_runtime_role(connection, role=worker_role, profile="worker")
            connection.execute(text(
                f'REVOKE EXECUTE ON FUNCTION pg_catalog.current_database() FROM "{worker_role}"',
            ))
        # Database privileges are cluster-wide. A runtime login must not reach
        # even the owner fixture database outside its selected application DB.
        with migration.begin() as connection:
            connection.execute(text(
                f'GRANT CONNECT, TEMPORARY ON DATABASE "{owner_url.database}" TO "{worker_role}"',
            ))
            with pytest.raises(ValueError, match="another database"):
                verify_runtime_role(connection, role=worker_role, profile="worker")
            connection.execute(text(
                f'REVOKE CONNECT, TEMPORARY ON DATABASE "{owner_url.database}" FROM "{worker_role}"',
            ))
        with worker.connect() as connection:
            verify_runtime_role(connection, profile="worker")
            # The inert column permits a policy-stabilising row lock, but no
            # authority-bearing site field is writable.
            connection.execute(text("SELECT id FROM sites WHERE id=:id FOR UPDATE"), {"id": site_id})
            connection.rollback()
        with make_session_factory(worker)() as session:
            first_gsc = ObservationBatch(
                rows=[GSCRow(
                    date=date(2026, 9, 1), page="https://example.test/service",
                    query="controlled test query", country="USA", device="DESKTOP",
                    clicks=2, impressions=40, position=8.5, data_state="partial",
                )],
                source="google_search_console", quality_flags=["partial_data"], complete=False,
            )
            first_ga4 = ObservationBatch(
                rows=[GA4Row(
                    date=date(2026, 9, 1), landing_page="/service", sessions=5,
                    key_events=1, qualified_conversions=None, conversion_value=None,
                    quality_flags=["attribution_lag"],
                )],
                source="google_analytics_4", quality_flags=["partial_data"], complete=False,
            )
            control.ingest_batch(session, session.get(m.Site, site_id), "gsc", first_gsc)
            control.ingest_batch(session, session.get(m.Site, site_id), "ga4", first_ga4)
            session.commit()

            refreshed_gsc = ObservationBatch(
                rows=[GSCRow(
                    date=date(2026, 9, 1), page="https://example.test/service",
                    query="controlled test query", country="USA", device="DESKTOP",
                    clicks=4, impressions=80, position=6.25, data_state="final",
                )],
                source="google_search_console", quality_flags=[], complete=True,
            )
            refreshed_ga4 = ObservationBatch(
                rows=[GA4Row(
                    date=date(2026, 9, 1), landing_page="/service", sessions=9,
                    key_events=2, qualified_conversions=1, conversion_value=25,
                )],
                source="google_analytics_4", quality_flags=[], complete=True,
            )
            control.ingest_batch(session, session.get(m.Site, site_id), "gsc", refreshed_gsc)
            control.ingest_batch(session, session.get(m.Site, site_id), "ga4", refreshed_ga4)
            session.commit()
            gsc = session.query(m.GSCDaily).filter_by(site_id=site_id).one()
            ga4 = session.query(m.GA4Daily).filter_by(site_id=site_id).one()
            assert (gsc.clicks, gsc.impressions, gsc.position, gsc.data_state) == (4, 80, 6.25, "final")
            assert (ga4.sessions, ga4.key_events, ga4.qualified_conversions, ga4.conversion_value) == (9, 2, 1, 25)

            run = m.JobRun(site_id=site_id, job_name="boundary-test", idempotency_key="one", owner="test-worker")
            session.add(run)
            session.commit()
            run.status = "completed"
            mission = session.query(m.MissionState).filter_by(site_id=site_id).one()
            mission.blockers_json = ["Synthetic provider unavailable"]
            session.commit()
            mission.autonomy_level = 5
            with pytest.raises(DBAPIError):
                session.commit()
            session.rollback()
        with worker.connect() as connection:
            for statement in (
                "UPDATE gsc_daily SET query='forbidden-key-change' WHERE site_id=:site_id",
                "UPDATE gsc_daily SET date='2026-09-02' WHERE site_id=:site_id",
                "UPDATE ga4_daily SET landing_page='/forbidden-key-change' WHERE site_id=:site_id",
                "UPDATE ga4_daily SET channel='Paid Search' WHERE site_id=:site_id",
            ):
                with pytest.raises(DBAPIError):
                    connection.execute(text(statement), {"site_id": site_id})
                connection.rollback()
    finally:
        for engine in (worker, api, migration):
            if engine is not None:
                engine.dispose()
        with owner.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
            connection.execute(text(f'DROP ROLE IF EXISTS "{worker_role}"'))
            connection.execute(text(f'DROP ROLE IF EXISTS "{api_role}"'))
        owner.dispose()
