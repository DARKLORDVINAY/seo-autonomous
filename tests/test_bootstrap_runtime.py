"""Production bootstrap is a runtime operation, separate from owner migration."""
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text

from backend.app.config.settings import Settings
from backend.app.db import models as m
from backend.app.db.session import make_engine, make_session_factory
from backend.app.services import control
import scripts.bootstrap as bootstrap_module
import scripts.grant_runtime as runtime_roles


@pytest.fixture
def production_bootstrap(tmp_path, monkeypatch):
    for name in (
        "SEO_RELEASE_IMAGE", "SEO_MIGRATION_EXPECTED_DATABASE", "SEO_MIGRATION_EXPECTED_SYSTEM_IDENTIFIER",
        "SEO_MIGRATION_MODE", "SEO_MIGRATION_EXPECTED_SCHEMA_HEADS", "VERIFICATION_ONLY",
    ):
        monkeypatch.delenv(name, raising=False)
    local_url = f"sqlite:///{tmp_path / 'runtime-bootstrap.sqlite'}"
    bootstrap_module.migrate(local_url, environment="test")
    engine = make_engine(local_url, environment="test")
    settings = Settings(
        _env_file=None, environment="production", service_role="utility",
        database_url="postgresql+psycopg://seo_api@db/seo",
        provider_mode="fixture", agent_mode="fixture",
    )
    events, migration_calls, checked_connections = [], [], []
    actual_migrate = bootstrap_module.migrate
    actual_create_site = control.create_site

    def observed_migrate(*args, **kwargs):
        migration_calls.append((args, kwargs))
        return actual_migrate(*args, **kwargs)

    def local_engine(url, *, environment):
        assert url == settings.database_url and environment == "production"
        return engine

    def checked_role(connection, *, profile):
        # Schema/readiness and canonical writes use a real local database here.
        # The real restricted PostgreSQL login is checked by the Compose CI gate.
        assert profile == "api"
        events.append("api-role-readiness")
        checked_connections.append(connection)

    def checked_create_site(session, **kwargs):
        assert events[-1] == "api-role-readiness"
        assert session.connection() is checked_connections[-1]
        events.append("create-site")
        return actual_create_site(session, **kwargs)

    monkeypatch.setattr(bootstrap_module, "migrate", observed_migrate)
    monkeypatch.setattr(bootstrap_module, "make_engine", local_engine)
    monkeypatch.setattr(runtime_roles, "verify_runtime_role", checked_role)
    monkeypatch.setattr(control, "create_site", checked_create_site)
    yield SimpleNamespace(
        settings=settings, engine=engine, events=events, migration_calls=migration_calls,
    )
    engine.dispose()


@pytest.mark.parametrize("selection", [
    {"demo": True},
    {"domain": "https://business.example.com", "name": "Configured business"},
])
def test_production_bootstrap_checks_api_runtime_then_registers_without_owner_pins(production_bootstrap, selection):
    case = production_bootstrap
    first = bootstrap_module.bootstrap(case.settings, **selection)
    second = bootstrap_module.bootstrap(case.settings, **selection)
    assert case.migration_calls == []
    assert case.events == ["api-role-readiness", "create-site", "api-role-readiness"]
    assert first["status"] == "created" and second["status"] == "existing"
    assert first["site_id"] == second["site_id"]
    assert first["production_write"] is second["production_write"] is False
    with make_session_factory(case.engine)() as session:
        site = session.get(m.Site, first["site_id"])
        assert site.autonomy_level == 1 and not site.production_enabled
        assert session.scalar(select(func.count()).select_from(m.Site)) == 1
    if selection.get("demo"):
        assert first["cycle_status"] == second["cycle_status"] == "completed"
        assert first["job_id"] == second["job_id"]


@pytest.mark.parametrize("rejected_gate", ["schema", "api-role"])
def test_production_bootstrap_readiness_failure_prevents_site_writes(production_bootstrap, monkeypatch, rejected_gate):
    case = production_bootstrap
    if rejected_gate == "schema":
        with case.engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num='wrong-release'"))
        expected_error = "Database schema revision differs"
    else:
        def reject_role(connection, *, profile):
            assert profile == "api"
            raise ValueError("Runtime role has forbidden privileges")

        monkeypatch.setattr(runtime_roles, "verify_runtime_role", reject_role)
        expected_error = "Runtime role has forbidden privileges"
    with pytest.raises(ValueError, match=expected_error):
        bootstrap_module.bootstrap(case.settings, demo=True)
    assert case.migration_calls == [] and case.events == []
    with make_session_factory(case.engine)() as session:
        for model in (m.Site, m.Action, m.Evidence, m.JobRun):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_explicit_production_migrate_only_still_requires_owner_release_pins(production_bootstrap):
    case = production_bootstrap
    with pytest.raises(ValueError, match="immutable image digest"):
        bootstrap_module.bootstrap(case.settings, migrate_only=True)
    assert len(case.migration_calls) == 1 and case.events == []


def test_production_migrate_only_cli_still_requires_pins_without_exposing_credentials(
    production_bootstrap, tmp_path, monkeypatch, capsys,
):
    case = production_bootstrap
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://owner:private-owner-password@db/seo")
    assert bootstrap_module.main(["--migrate-only", "--env-file", str(tmp_path / "absent.env")]) == 1
    assert len(case.migration_calls) == 1 and case.events == []
    output = capsys.readouterr().out
    assert '"error_type": "ValueError"' in output
    assert "private-owner-password" not in output


@pytest.mark.parametrize("environment", ["development", "test"])
def test_local_bootstrap_still_migrates_an_empty_database(tmp_path, monkeypatch, environment):
    settings = Settings(
        _env_file=None, environment=environment,
        database_url=f"sqlite:///{tmp_path / 'local-bootstrap.sqlite'}",
    )
    calls = []
    actual_migrate = bootstrap_module.migrate

    def observed_migrate(url, **kwargs):
        calls.append((url, kwargs))
        return actual_migrate(url, **kwargs)

    monkeypatch.setattr(bootstrap_module, "migrate", observed_migrate)
    result = bootstrap_module.bootstrap(settings, demo=True)
    assert result["status"] == "created" and result["cycle_status"] == "completed"
    assert calls == [(settings.database_url, {"environment": environment})]
