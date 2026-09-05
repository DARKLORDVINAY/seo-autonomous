"""Provider-neutral package checks; no external database or hosting is created."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from sqlalchemy import text

from backend.app.config.settings import Settings
from backend.app.db import models as m
from backend.app.db.readiness import expected_heads, verify_schema_revision
from backend.app.db.session import make_engine, make_session_factory
from backend.app.main import readiness
from scripts.backup_database import backup
from scripts.bootstrap import migrate
from scripts.deployment_preflight import checked_image
from scripts.verify_backup import verify_backup_receipt


def verification_settings(**changes):
    return Settings(_env_file=None, **{
        "environment": "test", "verification_only": True, "autonomy_level": 1,
        "production_enabled": False, "shadow_mode": True,
        "provider_mode": "fixture", "agent_mode": "fixture",
        "max_daily_actions": 0, "max_daily_cost_usd": 0, **changes,
    })


@pytest.mark.parametrize("change", [
    {"autonomy_level": 2}, {"production_enabled": True}, {"shadow_mode": False},
    {"provider_mode": "live"}, {"max_daily_actions": 1}, {"max_daily_cost_usd": .01},
    {"openai_api_key": "dummy"}, {"google_application_credentials": "/dummy.json"},
    {"wordpress_application_password": "dummy"}, {"dataforseo_login": "dummy"},
    {"dataforseo_password": "dummy"}, {"github_token": "dummy"},
])
def test_verification_deployment_rejects_authority_budget_and_credential_expansion(change):
    with pytest.raises(ValueError, match="Verification-only"):
        verification_settings(**change)


def test_verification_deployment_has_hard_zero_settings():
    config = verification_settings()
    assert config.autonomy_level == 1 and not config.production_enabled
    assert config.max_daily_actions == config.max_daily_cost_usd == 0
    assert config.provider_mode == config.agent_mode == "fixture"


@pytest.mark.parametrize("value", ["spiral:latest", "spiral:local", "", "sha256:short", "--bad@sha256:" + "a" * 64])
def test_mutable_or_malformed_release_pins_fail(value):
    with pytest.raises(ValueError):
        checked_image(value)


def test_digest_pins_are_explicit_and_readiness_requires_real_migrations(tmp_path):
    assert checked_image("registry.test/seo@sha256:" + "a" * 64)
    assert checked_image("sha256:" + "b" * 64)
    url = f"sqlite:///{tmp_path / 'readiness.sqlite3'}"
    migrate(url)
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as session:
            assert verify_schema_revision(session) == tuple(sorted(expected_heads()))
            assert readiness(session) == {"status": "ready"}
            session.execute(text("UPDATE alembic_version SET version_num='unknown_future_head'"))
            session.commit()
            with pytest.raises(HTTPException) as error:
                readiness(session)
            assert error.value.status_code == 503
    finally:
        engine.dispose()


def test_existing_tables_without_migration_stamp_are_not_ready():
    engine = make_engine("sqlite://")
    m.Base.metadata.create_all(engine)
    try:
        with make_session_factory(engine)() as session:
            with pytest.raises(HTTPException) as error:
                readiness(session)
            assert error.value.status_code == 503
    finally:
        engine.dispose()


def fake_commands(*, dump_status=0, inspect_status=0, dump_bytes=b"PGDMP-not-a-real-backup", timeout=False):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs["env"]))
        assert kwargs.get("shell") is not True
        assert all("postgresql://" not in arg and "password" not in arg.replace("--no-password", "") for arg in command)
        if command[0] == "pg_dump":
            kwargs["stdout"].write(dump_bytes)
            if timeout:
                raise subprocess.TimeoutExpired(command[0], 1)
            return SimpleNamespace(returncode=dump_status, stderr=b"private diagnostic" if dump_status else b"")
        assert command[:2] == ["pg_restore", "--list"]
        return SimpleNamespace(returncode=inspect_status, stderr=b"")
    return calls, run


def test_backup_is_private_checked_and_does_not_overwrite_or_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "ambient-dummy-password-not-allowed")
    monkeypatch.setenv("PGHOST", "unintended.example.test")
    calls, run = fake_commands()
    directory = tmp_path / "private"
    first = backup("configured_source", directory, writers_stopped=True, run=run)
    second = backup("configured_source", directory, writers_stopped=True, run=run)
    assert first["archive"] != second["archive"]
    assert len(list(directory.glob("*.dump"))) == 2
    assert not list(directory.glob("*.partial"))
    assert first["sha256"] == hashlib.sha256(b"PGDMP-not-a-real-backup").hexdigest()
    assert first["archive_list_verified"] and not first["restore_verified"] and not first["services_restarted"]
    assert all(path.stat().st_mode & 0o077 == 0 for path in directory.iterdir())
    assert all("PGPASSWORD" not in env and "PGHOST" not in env and env["PGSERVICE"] == "configured_source"
               for _, env in calls)
    assert all(command[0] in {"pg_dump", "pg_restore"} for command, _ in calls)


@pytest.mark.parametrize("options", [
    {"dump_status": 1}, {"inspect_status": 1}, {"dump_bytes": b""}, {"timeout": True},
])
def test_failed_or_partial_backups_are_not_promoted(tmp_path, options):
    _, run = fake_commands(**options)
    directory = tmp_path / "private"
    with pytest.raises((RuntimeError, subprocess.TimeoutExpired)):
        backup("configured_source", directory, writers_stopped=True, run=run)
    assert not list(directory.glob("*.dump"))
    assert not list(directory.glob("*.json"))
    assert len(list(directory.glob("*.partial"))) == 1


def test_backup_refuses_unconfirmed_or_unsafe_targets(tmp_path):
    calls, run = fake_commands()
    with pytest.raises(ValueError, match="quiesced"):
        backup("source", tmp_path / "backup", writers_stopped=False, run=run)
    with pytest.raises(ValueError, match="named libpq"):
        backup("postgresql://owner:dummy@host/db", tmp_path / "backup", writers_stopped=True, run=run)
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private"):
        backup("source", public, writers_stopped=True, run=run)
    assert calls == []


def test_backup_receipt_verifier_rechecks_private_pair_checksum_and_archive(tmp_path):
    _, dump_run = fake_commands()
    directory = tmp_path / "private"
    receipt = backup("source", directory, writers_stopped=True, run=dump_run)
    calls = []

    def inspect(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    receipt_path = directory / receipt["archive"].replace(".dump", ".json")
    verified = verify_backup_receipt(receipt_path, run=inspect)
    assert verified["status"] == "verified" and verified["restore_verified"] is False
    assert verified["safe_to_restore_over_active_database"] is False
    assert calls[0][0][:2] == ["pg_restore", "--list"]
    assert calls[0][1]["stdout"] is subprocess.DEVNULL and calls[0][1]["stderr"] is subprocess.DEVNULL


def test_backup_receipt_verifier_rejects_tampering_and_unsafe_paths(tmp_path):
    _, dump_run = fake_commands()
    directory = tmp_path / "private"
    receipt = backup("source", directory, writers_stopped=True, run=dump_run)
    receipt_path = directory / receipt["archive"].replace(".dump", ".json")
    archive_path = directory / receipt["archive"]
    archive_path.write_bytes(archive_path.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="size differs"):
        verify_backup_receipt(receipt_path, run=lambda *_args, **_kwargs: pytest.fail("must not parse tampered archive"))
    archive_path.chmod(0o644)
    with pytest.raises(ValueError, match="group/other"):
        verify_backup_receipt(receipt_path)


def test_verification_overlay_is_literal_not_environment_selected():
    overlay = Path("docker-compose.verification.yml").read_text()
    for expected in ('AUTONOMY_LEVEL: "1"', 'PRODUCTION_ENABLED: "false"', 'MAX_DAILY_ACTIONS: "0"',
                     'MAX_DAILY_COST_USD: "0"', 'VERIFICATION_ONLY: "true"', 'profiles: [verified-scheduler]',
                     'restart: "no"', 'pull_policy: never'):
        assert expected in overlay
    assert "${AUTONOMY_LEVEL" not in overlay and "${PRODUCTION_ENABLED" not in overlay


def test_worker_compose_scope_has_no_human_bearer_or_benchmark_import_capability():
    compose = Path("docker-compose.yml").read_text()
    shared = compose.split("x-api-environment:", 1)[0]
    worker = re.search(r"(?ms)^  worker:\n(.*?)(?=^  mcp:)", compose).group(1)
    for name in ("API_TOKEN", "APPROVAL_TOKEN", "ADMIN_TOKEN", "BENCHMARK_EVALUATOR_PUBLIC_KEY_FILE"):
        assert name not in shared
        assert name not in worker
    assert "SERVICE_ROLE: worker" in worker
    api = re.search(r"(?ms)^x-api-environment:.*?(?=^x-application:)", compose).group(0)
    assert all(name in api for name in ("API_TOKEN", "APPROVAL_TOKEN", "ADMIN_TOKEN"))
    assert "test: [CMD, python, /app/docker/entrypoint.py, worker, --healthcheck]" in worker


def test_optional_benchmark_key_mount_targets_api_only():
    overlay = Path("docker-compose.benchmark-attestation.yml").read_text()
    assert "  api:" in overlay and "  worker:" not in overlay
    assert "BENCHMARK_EVALUATOR_PUBLIC_KEY_HOST_FILE" in overlay
    assert "BENCHMARK_EXPECTED_EVALUATION_ID" in overlay
    assert "BENCHMARK_EXPECTED_CHALLENGE_SHA256" in overlay
    assert "BENCHMARK_EXPECTED_EXECUTION_ENVIRONMENT_SHA256" in overlay
    assert "BENCHMARK_ATTESTATION_MAX_AGE_HOURS" in overlay
    assert "read_only: true" in overlay and "create_host_path: false" in overlay


def test_postgresql_metadata_trigger_uses_dialect_escaped_percent():
    from sqlalchemy.dialects.postgresql.psycopg import dialect
    from sqlalchemy.sql.elements import TextClause
    statements = []
    class Connection:
        dialect = SimpleNamespace(name="postgresql")
        def execute(self, statement):
            assert isinstance(statement, TextClause)
            compiled = str(statement.compile(dialect=dialect()))
            assert "append-only canonical record: %%" in compiled
            statements.append(compiled)
        def exec_driver_sql(self, statement):
            assert "RAISE EXCEPTION" not in statement
            statements.append(statement)
    m.install_append_only_triggers(Connection())
    assert len(statements) == 1 + 4 * len(m.APPEND_ONLY_TABLES)
