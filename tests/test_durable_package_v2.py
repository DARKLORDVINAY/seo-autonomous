"""Provider-neutral package checks; no external database or hosting is created."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
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
from backend.app import main as main_module
from scripts import backup_database
from scripts import deployment_preflight
from scripts.backup_database import backup
from scripts.bootstrap import migrate
from scripts.deployment_preflight import (
    MigrationTarget,
    checked_image,
    checked_migration_target,
    preflight_migration_target,
    verify_migration_target,
)
from scripts.verify_backup import REQUIRED_FIELDS, verify_backup_receipt


BACKUP_IDENTITIES = {
    "expected_database": "seo_checkpoint",
    "expected_server_identity": "7439284610293847561",
    "expected_server_address": "192.0.2.10",
    "expected_server_port": 5432,
    "expected_schema_heads": ("0002_runtime_role_split",),
    "tls_server_name": "db.example.test",
    "release_image": "registry.test/seo@sha256:" + "a" * 64,
    "release_commit": "b" * 40,
    "runtime_identity": "api-worker-config:sha256:" + "c" * 64,
    "checkpoint_identity": "checkpoint:20260904T120000Z",
}


SOURCE_METADATA = {
    "database": BACKUP_IDENTITIES["expected_database"],
    "server_identity": BACKUP_IDENTITIES["expected_server_identity"],
    "server_address": "192.0.2.10",
    "server_port": 5432,
    "server_version": "17.11",
    "server_version_num": "170011",
    "schema_heads": list(BACKUP_IDENTITIES["expected_schema_heads"]),
}


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
            assert main_module.readiness(session, verification_settings()) == {"status": "ready"}
            session.execute(text("UPDATE alembic_version SET version_num='unknown_future_head'"))
            session.commit()
            with pytest.raises(HTTPException) as error:
                main_module.readiness(session, verification_settings())
            assert error.value.status_code == 503
    finally:
        engine.dispose()


def test_migration_target_pins_distinguish_bootstrap_from_upgrade():
    bootstrap = checked_migration_target(
        database="seo", server_identity="1234567890", mode="bootstrap",
        schema_heads="uninitialized",
    )
    assert bootstrap == MigrationTarget("seo", "1234567890", "bootstrap", ())
    upgrade = checked_migration_target(
        database="seo", server_identity="1234567890", mode="upgrade",
        schema_heads="0001_canonical,0001_parallel",
    )
    assert upgrade.schema_heads == ("0001_canonical", "0001_parallel")

    for values in (
        {"database": "postgres", "server_identity": "1234567890", "mode": "bootstrap",
         "schema_heads": "uninitialized"},
        {"database": "seo", "server_identity": "0000000001", "mode": "bootstrap",
         "schema_heads": "uninitialized"},
        {"database": "seo", "server_identity": "18446744073709551616", "mode": "bootstrap",
         "schema_heads": "uninitialized"},
        {"database": "seo", "server_identity": "1234567890", "mode": "upgrade",
         "schema_heads": "uninitialized"},
        {"database": "seo", "server_identity": "1234567890", "mode": "upgrade",
         "schema_heads": "z_head,a_head"},
    ):
        with pytest.raises(ValueError):
            checked_migration_target(**values)


def test_migration_target_preflight_is_read_only_and_precedes_identity_check(monkeypatch):
    events = []

    class Result:
        def scalar_one(self):
            return "on"

    class Connection:
        dialect = SimpleNamespace(name="postgresql")

        def __enter__(self):
            events.append("connect")
            return self

        def __exit__(self, *_args):
            events.append("close")

        def exec_driver_sql(self, sql):
            events.append(sql)

        def execute(self, statement, *_args):
            assert "transaction_read_only" in str(statement)
            events.append("confirm-read-only")
            return Result()

        def rollback(self):
            events.append("rollback")

    class Engine:
        def connect(self):
            return Connection()

        def dispose(self):
            events.append("dispose")

    expected = MigrationTarget("seo", "1234567890", "upgrade", ("0001_canonical",))
    monkeypatch.setattr(deployment_preflight, "make_engine", lambda *_args, **_kwargs: Engine())
    monkeypatch.setattr(
        deployment_preflight,
        "pin_migration_search_path",
        lambda _connection: events.append("pin-search-path"),
    )
    monkeypatch.setattr(
        deployment_preflight,
        "verify_migration_target",
        lambda _connection, target: events.append(("verify-target", target)) or {"ok": True},
    )

    assert preflight_migration_target(
        "postgresql+psycopg://owner@db/seo", expected, environment="production",
    ) == {"ok": True}
    assert events == [
        "connect",
        "SET TRANSACTION READ ONLY",
        "confirm-read-only",
        "pin-search-path",
        ("verify-target", expected),
        "rollback",
        "close",
        "dispose",
    ]


def test_migration_target_verifier_rejects_identity_head_and_marker_spoofing():
    class Result:
        def __init__(self, *, one=None, scalar=None, values=()):
            self._one = one
            self._scalar = scalar
            self._values = values

        def one(self):
            return self._one

        def scalar_one_or_none(self):
            return self._scalar

        def scalars(self):
            return iter(self._values)

    class Connection:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(
            self, *, database="seo", identity="1234567890", kind="r",
            heads=("0001_canonical",), objects=(),
        ):
            self.database = database
            self.identity = identity
            self.kind = kind
            self.heads = heads
            self.objects = objects

        def execute(self, statement):
            sql = str(statement)
            if "pg_control_system" in sql:
                return Result(one=(self.database, self.identity))
            if "relation.relname = 'alembic_version'" in sql:
                return Result(scalar=self.kind)
            if "SELECT version_num" in sql:
                return Result(values=self.heads)
            if "AS public_objects" in sql:
                return Result(values=self.objects)
            raise AssertionError(sql)

    expected = MigrationTarget("seo", "1234567890", "upgrade", ("0001_canonical",))
    assert verify_migration_target(Connection(), expected)["schema_heads"] == ("0001_canonical",)
    for connection, match in (
        (Connection(database="wrong"), "identity"),
        (Connection(identity="9999999999"), "identity"),
        (Connection(kind="v"), "ordinary public table"),
        (Connection(heads=("other",)), "schema heads"),
    ):
        with pytest.raises(ValueError, match=match):
            verify_migration_target(connection, expected)

    bootstrap = MigrationTarget("seo", "1234567890", "bootstrap", ())
    assert verify_migration_target(Connection(kind=None), bootstrap)["schema_heads"] == ()
    with pytest.raises(ValueError, match="empty public schema"):
        verify_migration_target(Connection(kind=None, objects=("routine:hostile",)), bootstrap)


def test_existing_tables_without_migration_stamp_are_not_ready():
    engine = make_engine("sqlite://")
    m.Base.metadata.create_all(engine)
    try:
        with make_session_factory(engine)() as session:
            with pytest.raises(HTTPException) as error:
                main_module.readiness(session, verification_settings())
            assert error.value.status_code == 503
    finally:
        engine.dispose()


def fake_commands(*, dump_status=0, inspect_status=0, dump_bytes=b"PGDMP-not-a-real-backup", timeout=False,
                  dump_stderr=b"", inspect_stderr=b"", metadata=None, post_metadata=None,
                  metadata_stderr=b"", version_stderr=b""):
    calls = []
    metadata_calls = 0

    def run(command, **kwargs):
        nonlocal metadata_calls
        calls.append((command, kwargs["env"]))
        assert kwargs.get("shell") is not True
        assert all("postgresql://" not in arg and "password" not in arg.replace("--no-password", "") for arg in command)
        if command[1:] == ["--version"]:
            return SimpleNamespace(returncode=0, stdout=f"{command[0]} (PostgreSQL) 17.11\n".encode(),
                                   stderr=version_stderr)
        if command[0] == "psql":
            observed = (post_metadata if metadata_calls else metadata) or SOURCE_METADATA
            metadata_calls += 1
            return SimpleNamespace(returncode=0, stdout=json.dumps(observed).encode() + b"\n",
                                   stderr=metadata_stderr)
        if command[0] == "pg_dump":
            kwargs["stdout"].write(dump_bytes)
            if timeout:
                raise subprocess.TimeoutExpired(command[0], 1)
            return SimpleNamespace(returncode=dump_status,
                                   stderr=dump_stderr or (b"private diagnostic" if dump_status else b""))
        assert command[:2] == ["pg_restore", "--list"]
        return SimpleNamespace(returncode=inspect_status, stderr=inspect_stderr)
    return calls, run


def create_backup(directory, run, *, service="configured_source"):
    return backup(service, directory, writers_stopped=True, run=run, **BACKUP_IDENTITIES)


def receipt_path(directory: Path, receipt: dict) -> Path:
    return directory / receipt["bundle"] / receipt["receipt"]


def archive_path(directory: Path, receipt: dict) -> Path:
    return directory / receipt["bundle"] / receipt["archive"]


def verification_pins(receipt: dict, **changes):
    pins = {
        "expected_service": receipt["source_service"],
        "expected_database": BACKUP_IDENTITIES["expected_database"],
        "expected_server_identity": BACKUP_IDENTITIES["expected_server_identity"],
        "expected_server_address": BACKUP_IDENTITIES["expected_server_address"],
        "expected_server_port": BACKUP_IDENTITIES["expected_server_port"],
        "expected_tls_server_name": BACKUP_IDENTITIES["tls_server_name"],
        "expected_schema_heads": BACKUP_IDENTITIES["expected_schema_heads"],
        "expected_release_image": BACKUP_IDENTITIES["release_image"],
        "expected_release_commit": BACKUP_IDENTITIES["release_commit"],
        "expected_runtime_identity": BACKUP_IDENTITIES["runtime_identity"],
        "expected_checkpoint_identity": BACKUP_IDENTITIES["checkpoint_identity"],
        "expected_archive_sha256": receipt["sha256"],
    }
    pins.update(changes)
    return pins


def verify_receipt(path: Path, receipt: dict, *, run, **changes):
    return verify_backup_receipt(path, run=run, **verification_pins(receipt, **changes))


def test_backup_is_private_checked_and_does_not_overwrite_or_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "ambient-dummy-password-not-allowed")
    monkeypatch.setenv("PGHOST", "unintended.example.test")
    monkeypatch.setenv("PGSSLMODE", "disable")
    monkeypatch.setenv("PGOPTIONS", "-c search_path=attacker")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-reach-a-child")
    calls, run = fake_commands()
    directory = tmp_path / "private"
    first = create_backup(directory, run)
    second = create_backup(directory, run)
    assert first["archive"] != second["archive"]
    assert len(list(directory.glob("*.backup"))) == 2
    assert not list(directory.glob(".*.partial"))
    assert all({path.name for path in bundle.iterdir()} == {
        bundle.name.removesuffix(".backup") + ".dump",
        bundle.name.removesuffix(".backup") + ".json",
    } for bundle in directory.glob("*.backup"))
    assert first["sha256"] == hashlib.sha256(b"PGDMP-not-a-real-backup").hexdigest()
    assert first["archive_list_verified"] and not first["restore_verified"] and not first["services_restarted"]
    assert first["warnings_present"] is False and first["pg_dump_stderr_empty"] is True
    assert first["source_database"] == BACKUP_IDENTITIES["expected_database"]
    assert first["source_server_identity"] == BACKUP_IDENTITIES["expected_server_identity"]
    assert first["source_server_address"] == BACKUP_IDENTITIES["expected_server_address"]
    assert first["source_server_port"] == BACKUP_IDENTITIES["expected_server_port"]
    assert first["source_tls_server_name"] == BACKUP_IDENTITIES["tls_server_name"]
    assert first["transport_policy"] == "verified-tls" and first["source_identity_stable"] is True
    assert first["source_identity_pre_dump_sha256"] == first["source_identity_post_dump_sha256"]
    assert first["schema_heads"] == list(BACKUP_IDENTITIES["expected_schema_heads"])
    assert first["release_image"] == BACKUP_IDENTITIES["release_image"]
    assert first["release_commit"] == BACKUP_IDENTITIES["release_commit"]
    assert all(path.stat().st_mode & 0o077 == 0 for path in directory.rglob("*"))
    assert all(
        "PGPASSWORD" not in env and "PGHOST" not in env
        and "PGOPTIONS" not in env and "UNRELATED_SECRET" not in env
        and env["PGSSLMODE"] == "verify-full" and env["PGGSSENCMODE"] == "disable"
        and env["LC_ALL"] == "C"
        for _, env in calls
    )
    assert all(command[0] in {"pg_dump", "pg_restore", "psql"} for command, _ in calls)
    connection_arguments = [
        argument
        for command, _ in calls
        if command[0] in {"psql", "pg_dump"}
        for argument in command
        if argument.startswith("--dbname=")
    ]
    assert connection_arguments
    assert all(
        "service=configured_source" in argument
        and "dbname=seo_checkpoint" in argument
        and "host=db.example.test" in argument
        and "hostaddr=192.0.2.10" in argument
        and "port=5432" in argument
        and "sslmode=verify-full" in argument
        and "gssencmode=disable" in argument
        for argument in connection_arguments
    )


@pytest.mark.parametrize("options", [
    {"dump_status": 1}, {"inspect_status": 1}, {"dump_bytes": b""}, {"timeout": True},
    {"dump_stderr": b"pg_dump: warning: synthetic"},
    {"inspect_stderr": b"pg_restore: warning: synthetic"},
])
def test_failed_or_partial_backups_are_not_promoted(tmp_path, options):
    _, run = fake_commands(**options)
    directory = tmp_path / "private"
    with pytest.raises((RuntimeError, subprocess.TimeoutExpired)):
        create_backup(directory, run)
    assert not list(directory.glob("*.backup"))
    assert len(list(directory.glob(".*.partial"))) == 1


@pytest.mark.parametrize("options", [
    {"version_stderr": b"synthetic warning"},
    {"metadata_stderr": b"synthetic warning"},
    {"metadata": {**SOURCE_METADATA, "database": "wrong_database"}},
    {"metadata": {**SOURCE_METADATA, "server_identity": "1111111111111111111"}},
    {"metadata": {**SOURCE_METADATA, "schema_heads": ["wrong_head"]}},
])
def test_source_identity_or_preflight_warning_mismatch_fails_before_staging(tmp_path, options):
    _, run = fake_commands(**options)
    directory = tmp_path / "private"
    with pytest.raises(RuntimeError):
        create_backup(directory, run)
    assert not list(directory.glob("*.backup"))
    assert not list(directory.glob(".*.partial"))


def test_post_dump_source_identity_change_blocks_promotion(tmp_path):
    changed = {**SOURCE_METADATA, "server_identity": "1111111111111111111"}
    _, run = fake_commands(post_metadata=changed)
    directory = tmp_path / "private"
    with pytest.raises(RuntimeError, match="Post-dump source identity differs"):
        create_backup(directory, run)
    assert not list(directory.glob("*.backup"))
    assert len(list(directory.glob(".*.partial"))) == 1


def test_backup_refuses_unconfirmed_or_unsafe_targets(tmp_path):
    calls, run = fake_commands()
    with pytest.raises(ValueError, match="quiesced"):
        backup("source", tmp_path / "backup", writers_stopped=False, run=run, **BACKUP_IDENTITIES)
    with pytest.raises(ValueError, match="named libpq"):
        backup("postgresql://owner:dummy@host/db", tmp_path / "backup", writers_stopped=True, run=run,
               **BACKUP_IDENTITIES)
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private"):
        backup("source", public, writers_stopped=True, run=run, **BACKUP_IDENTITIES)
    with pytest.raises(ValueError, match="numeric IP"):
        backup(
            "source",
            tmp_path / "private",
            writers_stopped=True,
            run=run,
            **{**BACKUP_IDENTITIES, "expected_server_address": "db.example.test"},
        )
    with pytest.raises(ValueError, match="TLS server name"):
        backup(
            "source",
            tmp_path / "private",
            writers_stopped=True,
            run=run,
            **{**BACKUP_IDENTITIES, "tls_server_name": "first.example,second.example"},
        )
    assert calls == []


def test_local_socket_transport_escape_is_confined_to_disposable_restore_fixture(tmp_path, monkeypatch):
    local_identity = {
        **BACKUP_IDENTITIES,
        "expected_database": "v2_backup_" + "a" * 32,
        "expected_server_address": "unix-socket",
        "expected_server_port": 0,
        "tls_server_name": "local-socket-test",
    }
    local_metadata = {
        **SOURCE_METADATA,
        "database": local_identity["expected_database"],
        "server_address": "unix-socket",
        "server_port": 0,
    }
    _, run = fake_commands(metadata=local_metadata, post_metadata=local_metadata)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "disposable-restore-fixture")
    receipt = backup(
        "disposable_ci_source",
        tmp_path / "private",
        writers_stopped=True,
        run=run,
        _allow_local_socket_test_adapter=True,
        **local_identity,
    )
    assert receipt["transport_policy"] == "local-socket-test"
    with pytest.raises(ValueError, match="restricted to the disposable restore test"):
        backup(
            "unapproved_source",
            tmp_path / "other",
            writers_stopped=True,
            run=run,
            _allow_local_socket_test_adapter=True,
            **local_identity,
        )


def test_backup_receipt_verifier_rechecks_private_pair_checksum_and_archive(tmp_path):
    _, dump_run = fake_commands()
    directory = tmp_path / "private"
    receipt = create_backup(directory, dump_run, service="source")
    calls = []

    def inspect(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stderr=b"")

    verified = verify_receipt(receipt_path(directory, receipt), receipt, run=inspect)
    assert verified["status"] == "verified" and verified["restore_verified"] is False
    assert verified["safe_to_restore_over_active_database"] is False
    assert verified["checkpoint_identity"] == BACKUP_IDENTITIES["checkpoint_identity"]
    assert calls[0][0][:2] == ["pg_restore", "--list"]
    assert calls[0][1]["stdout"] is subprocess.DEVNULL and calls[0][1]["stderr"] is subprocess.PIPE
    assert set(calls[0][1]["env"]) <= {"HOME", "PATH", "LC_ALL", "TZ"}


def test_backup_receipt_verifier_rejects_tampering_and_unsafe_paths(tmp_path):
    _, dump_run = fake_commands()
    directory = tmp_path / "private"
    receipt = create_backup(directory, dump_run, service="source")
    receipt_file = receipt_path(directory, receipt)
    archive_file = archive_path(directory, receipt)
    archive_file.write_bytes(archive_file.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="size differs"):
        verify_receipt(
            receipt_file,
            receipt,
            run=lambda *_args, **_kwargs: pytest.fail("must not parse tampered archive"),
        )
    archive_file.chmod(0o644)
    with pytest.raises(ValueError, match="group/other"):
        verify_receipt(receipt_file, receipt, run=lambda *_args, **_kwargs: pytest.fail("must fail on mode"))


def test_backup_receipt_verifier_rejects_extra_bundle_members(tmp_path):
    _, dump_run = fake_commands()
    directory = tmp_path / "private"
    receipt = create_backup(directory, dump_run, service="source")
    receipt_file = receipt_path(directory, receipt)
    extra = receipt_file.parent / "unexpected-member"
    extra.write_bytes(b"not part of the atomic bundle")
    extra.chmod(0o600)
    with pytest.raises(ValueError, match="exactly its declared"):
        verify_receipt(
            receipt_file,
            receipt,
            run=lambda *_args, **_kwargs: pytest.fail("must reject before archive parsing"),
        )


@pytest.mark.parametrize(("pin", "value"), [
    ("expected_service", "other_source"),
    ("expected_database", "other_database"),
    ("expected_server_identity", "1111111111111111111"),
    ("expected_server_address", "192.0.2.11"),
    ("expected_server_port", 5433),
    ("expected_tls_server_name", "other.example.test"),
    ("expected_schema_heads", ("other_head",)),
    ("expected_release_image", "registry.test/seo@sha256:" + "d" * 64),
    ("expected_release_commit", "d" * 40),
    ("expected_runtime_identity", "other-runtime"),
    ("expected_checkpoint_identity", "other-checkpoint"),
    ("expected_archive_sha256", "d" * 64),
])
def test_backup_receipt_verifier_requires_independent_exact_pins(tmp_path, pin, value):
    _, dump_run = fake_commands()
    directory = tmp_path / "private"
    receipt = create_backup(directory, dump_run, service="source")
    with pytest.raises(ValueError, match="independently supplied expected pin"):
        verify_receipt(
            receipt_path(directory, receipt),
            receipt,
            run=lambda *_args, **_kwargs: pytest.fail("must reject before archive parsing"),
            **{pin: value},
        )


def test_validly_shaped_receipt_identity_mutation_is_rejected_by_external_pin(tmp_path):
    _, dump_run = fake_commands()
    directory = tmp_path / "private"
    receipt = create_backup(directory, dump_run, service="source")
    receipt_file = receipt_path(directory, receipt)
    changed = json.loads(receipt_file.read_text())
    changed["release_commit"] = "c" * 40
    receipt_file.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="independently supplied expected pin"):
        verify_receipt(
            receipt_file,
            receipt,
            run=lambda *_args, **_kwargs: pytest.fail("must reject before archive parsing"),
        )


def test_late_failure_never_publishes_orphan_archive_or_receipt(tmp_path, monkeypatch):
    _, run = fake_commands()
    directory = tmp_path / "private"
    monkeypatch.setattr(backup_database, "_sync_directory",
                        lambda _path: (_ for _ in ()).throw(OSError("synthetic late sync failure")))
    with pytest.raises(OSError, match="synthetic late sync failure"):
        create_backup(directory, run)
    assert not list(directory.glob("*.backup"))
    staging = list(directory.glob(".*.partial"))
    assert len(staging) == 1
    assert len(list(staging[0].glob("*.dump"))) == 1
    assert len(list(staging[0].glob("*.json"))) == 1


def test_post_rename_sync_failure_leaves_a_complete_not_split_bundle(tmp_path, monkeypatch):
    _, run = fake_commands()
    directory = tmp_path / "private"
    real_sync = backup_database._sync_directory
    calls = []

    def fail_parent_sync(path):
        calls.append(Path(path))
        if len(calls) == 2:
            raise OSError("synthetic parent sync failure")
        real_sync(path)

    monkeypatch.setattr(backup_database, "_sync_directory", fail_parent_sync)
    with pytest.raises(OSError, match="synthetic parent sync failure"):
        create_backup(directory, run)
    bundles = list(directory.glob("*.backup"))
    assert len(bundles) == 1 and not list(directory.glob(".*.partial"))
    assert sorted(path.suffix for path in bundles[0].iterdir()) == [".dump", ".json"]


def test_publication_renames_one_complete_bundle_and_refuses_collision(tmp_path, monkeypatch):
    _, run = fake_commands()
    directory = tmp_path / "private"
    observed = []
    real_rename = backup_database.os.rename

    def checked_rename(source, target):
        source, target = Path(source), Path(target)
        observed.append((source, target, sorted(path.suffix for path in source.iterdir())))
        assert source.name.endswith(".partial") and target.name.endswith(".backup")
        assert observed[-1][2] == [".dump", ".json"]
        real_rename(source, target)

    monkeypatch.setattr(backup_database.os, "rename", checked_rename)
    fixed = SimpleNamespace(hex="d" * 32)
    monkeypatch.setattr(backup_database.uuid, "uuid4", lambda: fixed)
    fixed_time = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(backup_database, "_utcnow", lambda: fixed_time)
    first = create_backup(directory, run)
    first_bytes = archive_path(directory, first).read_bytes()
    with pytest.raises(FileExistsError, match="nothing was overwritten"):
        create_backup(directory, run)
    assert archive_path(directory, first).read_bytes() == first_bytes
    assert len(observed) == 1 and len(list(directory.glob("*.backup"))) == 1


def test_verifier_requires_every_receipt_field(tmp_path):
    _, run = fake_commands()
    directory = tmp_path / "private"
    receipt = create_backup(directory, run)
    receipt_file = receipt_path(directory, receipt)
    original = json.loads(receipt_file.read_text())
    for field in sorted(REQUIRED_FIELDS):
        candidate = dict(original)
        candidate.pop(field)
        receipt_file.write_text(json.dumps(candidate))
        with pytest.raises(ValueError, match="missing required"):
            verify_receipt(
                receipt_file,
                receipt,
                run=lambda *_args, **_kwargs: pytest.fail("must fail before parsing"),
            )
    receipt_file.write_text(json.dumps(original))


def test_verifier_rejects_unknown_or_duplicate_receipt_fields(tmp_path):
    _, run = fake_commands()
    directory = tmp_path / "private"
    receipt = create_backup(directory, run)
    receipt_file = receipt_path(directory, receipt)
    original = receipt_file.read_text()
    content = json.loads(original)
    content["future_unreviewed_claim"] = True
    receipt_file.write_text(json.dumps(content))
    with pytest.raises(ValueError, match="unsupported integrity fields"):
        verify_receipt(
            receipt_file,
            receipt,
            run=lambda *_args, **_kwargs: pytest.fail("must fail before parsing"),
        )
    duplicate = original.rstrip().removesuffix("}") + ', "sha256": "' + "0" * 64 + '"}'
    receipt_file.write_text(duplicate)
    with pytest.raises(ValueError, match="duplicate JSON field"):
        verify_receipt(
            receipt_file,
            receipt,
            run=lambda *_args, **_kwargs: pytest.fail("must fail before parsing"),
        )


@pytest.mark.parametrize(("field", "value", "message"), [
    ("warnings_present", True, "warning claims"),
    ("pg_dump_stderr_empty", False, "warning claims"),
    ("dump_started_at", "not-a-time", "dump_started_at"),
    ("dump_completed_at", "2099-01-01T00:00:00+00:00", "chronological bounds"),
    ("pg_dump_version", "untrusted version", "pg_dump version"),
    ("schema_heads", ["z", "a"], "schema heads"),
    ("release_image", "latest", "release image"),
    ("source_server_identity", "descriptive-label", "server identity"),
])
def test_verifier_validates_warning_identity_version_and_time_fields(tmp_path, field, value, message):
    _, run = fake_commands()
    directory = tmp_path / "private"
    receipt = create_backup(directory, run)
    receipt_file = receipt_path(directory, receipt)
    content = json.loads(receipt_file.read_text())
    content[field] = value
    receipt_file.write_text(json.dumps(content))
    with pytest.raises(ValueError, match=message):
        verify_receipt(
            receipt_file,
            receipt,
            run=lambda *_args, **_kwargs: pytest.fail("must fail before parsing"),
        )


def test_verifier_fails_closed_on_fresh_archive_list_warning(tmp_path):
    _, run = fake_commands()
    directory = tmp_path / "private"
    receipt = create_backup(directory, run)
    with pytest.raises(RuntimeError, match="emitted stderr"):
        verify_receipt(
            receipt_path(directory, receipt),
            receipt,
            run=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stderr=b"synthetic warning"),
        )


def test_verification_overlay_is_literal_not_environment_selected():
    overlay = Path("docker-compose.verification.yml").read_text()
    for expected in ('ENVIRONMENT: production', 'AUTONOMY_LEVEL: "1"', 'PRODUCTION_ENABLED: "false"', 'MAX_DAILY_ACTIONS: "0"',
                     'MAX_DAILY_COST_USD: "0"', 'VERIFICATION_ONLY: "true"', 'profiles: [verified-scheduler]',
                     'restart: "no"', 'pull_policy: never'):
        assert expected in overlay
    assert "${AUTONOMY_LEVEL" not in overlay and "${PRODUCTION_ENABLED" not in overlay
    migrate = re.search(r"(?ms)^  migrate:\n(.*?)(?=^  api:)", overlay).group(1)
    assert 'VERIFICATION_ONLY: "true"' in migrate
    assert "ENVIRONMENT: production" in migrate
    assert "SEO_RELEASE_IMAGE: ${SEO_RELEASE_IMAGE:?Set the reviewed immutable image digest}" in migrate
    for name in (
        "SEO_MIGRATION_EXPECTED_DATABASE",
        "SEO_MIGRATION_EXPECTED_SYSTEM_IDENTIFIER",
        "SEO_MIGRATION_MODE",
        "SEO_MIGRATION_EXPECTED_SCHEMA_HEADS",
    ):
        assert f"{name}: ${{{name}:?" in migrate
    base = Path("docker-compose.yml").read_text()
    base_migrate = re.search(r"(?ms)^  migrate:\n(.*?)(?=^  api:)", base).group(1)
    assert "ENVIRONMENT: development" in base_migrate


def test_worker_compose_scope_has_no_human_bearer_or_benchmark_import_capability():
    compose = Path("docker-compose.yml").read_text()
    shared = compose.split("x-api-environment:", 1)[0]
    worker = re.search(r"(?ms)^  worker:\n(.*?)(?=^  mcp:)", compose).group(1)
    for name in ("API_TOKEN", "APPROVAL_TOKEN", "ADMIN_TOKEN", "BENCHMARK_EVALUATOR_PUBLIC_KEY_FILE"):
        assert name not in shared
        assert name not in worker
    assert "SERVICE_ROLE: worker" in worker
    assert "POSTGRES_WORKER_USER" in compose and "POSTGRES_WORKER_PASSWORD" in compose
    assert "POSTGRES_API_USER" in compose and "POSTGRES_API_PASSWORD" in compose
    assert "POSTGRES_APP_USER" not in compose and "POSTGRES_APP_PASSWORD" not in compose
    api = re.search(r"(?ms)^x-api-environment:.*?(?=^x-application:)", compose).group(0)
    assert all(name in api for name in ("API_TOKEN", "APPROVAL_TOKEN", "ADMIN_TOKEN"))
    assert "test: [CMD, python, -m, backend.app.scheduler, --healthcheck]" in worker


def test_api_readiness_applies_environment_scoped_database_gate(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'api-readiness.sqlite3'}"
    migrate(url)
    engine = make_engine(url)
    checked = []
    try:
        monkeypatch.setattr(main_module, "verify_database_readiness", lambda connection, *, environment, **kwargs:
                            checked.append((connection.dialect.name, environment, kwargs)))
        with make_session_factory(engine)() as session:
            assert main_module.readiness(session, SimpleNamespace(environment="production")) == {"status": "ready"}
    finally:
        engine.dispose()
    assert checked == [("sqlite", "production", {"profile": "api", "privilege_cache_seconds": 30})]


def test_api_readiness_fails_closed_on_runtime_privilege_drift(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'api-privilege-drift.sqlite3'}"
    migrate(url)
    engine = make_engine(url)
    try:
        def rejected(_connection, *, environment, **_):
            assert environment == "production"
            raise ValueError("runtime role privilege drift")

        monkeypatch.setattr(main_module, "verify_database_readiness", rejected)
        with make_session_factory(engine)() as session, pytest.raises(HTTPException) as error:
            main_module.readiness(session, SimpleNamespace(environment="production"))
        assert error.value.status_code == 503
        assert "runtime role privilege drift" not in error.value.detail
    finally:
        engine.dispose()


def test_optional_benchmark_key_mount_targets_api_only():
    overlay = Path("docker-compose.benchmark-attestation.yml").read_text()
    assert "  api:" in overlay and "  worker:" not in overlay
    assert "BENCHMARK_EVALUATOR_PUBLIC_KEY_HOST_FILE" in overlay
    assert "BENCHMARK_EXPECTED_EVALUATION_ID" in overlay
    assert "BENCHMARK_EXPECTED_CHALLENGE_SHA256" in overlay
    assert "BENCHMARK_EXPECTED_OBSERVATIONS_SHA256" in overlay
    assert "BENCHMARK_EXPECTED_PREDICTIONS_SHA256" in overlay
    assert "BENCHMARK_EXPECTED_TRUTH_COMMITMENT_SHA256" in overlay
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
