"""Real dump/restore drill, only in the explicitly configured disposable CI DB.

Local runs skip honestly when the PostgreSQL container is unavailable. No
production database is restored over, no host is provisioned, and only uniquely
named databases/role created by this fixture are removed during cleanup.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from backend.app.contracts import stable_hash
from backend.app.db import models as m
from backend.app.db.readiness import verify_schema_revision
from backend.app.db.session import make_engine, make_session_factory
from backend.app.services import control
from scripts.backup_database import backup
from scripts.bootstrap import migrate
from scripts.grant_runtime import grant_runtime_privileges, verify_runtime_role


def snapshot(engine):
    result = {}
    with engine.connect() as connection:
        result["schema_heads"] = list(verify_schema_revision(connection))
        for table in m.Base.metadata.sorted_tables:
            rows = [dict(row) for row in connection.execute(select(table)).mappings()]
            result[table.name] = {"count": len(rows), "rows_sha256": stable_hash(sorted(stable_hash(row) for row in rows))}
    return result


def test_actual_postgres_dump_restore_preserves_rows_audit_guards_and_roles(tmp_path, record_property):
    raw_url = os.environ.get("TEST_POSTGRES_URL")
    container = os.environ.get("TEST_POSTGRES_CONTAINER")
    if not raw_url or not container:
        pytest.skip("Actual dump/restore needs the explicitly configured disposable PostgreSQL CI container")
    if not re.fullmatch(r"[a-f0-9]{12,64}", container):
        pytest.fail("Expected an exact disposable container ID")
    url = make_url(raw_url)
    if url.database != "seo_ci" or url.host not in {"localhost", "127.0.0.1"}:
        pytest.fail("Restore drill is restricted to the existing loopback seo_ci test service")
    # Validate the provided container before giving it any archive bytes.
    inspected = subprocess.run(["docker", "inspect", "--format", "{{.Config.Image}}", container],
                               capture_output=True, check=True, timeout=10)
    assert inspected.stdout.decode().strip() == "postgres:17.11"
    owner = make_engine(raw_url, isolation_level="AUTOCOMMIT")
    token = uuid4().hex
    source_name, restored_name, role = "v2_backup_" + token, "v2_restore_" + token, "v2_restore_app_" + token[:20]
    created = []
    source = restored = runtime = None
    role_created = False
    try:
        for name in (source_name, restored_name):
            with owner.connect() as connection:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
            created.append(name)
        source_url = url.set(database=source_name).render_as_string(hide_password=False)
        restored_url = url.set(database=restored_name).render_as_string(hide_password=False)
        migrate(source_url)
        source, restored = make_engine(source_url), make_engine(restored_url)
        with make_session_factory(source)() as session:
            site = control.create_site(session, name="Synthetic backup verification", base_url="https://example.test", fixture=True)
            evidence = m.Evidence(site_id=site.id, source="fixture:backup-drill", source_type="engineering_fixture",
                content={"synthetic": True, "not_business_value": True}, confidence=1, is_fixture=True)
            session.add(evidence)
            action = control.local_audit(session, site.id, "backup_fixture", "test-only", "Known immutable row for restore verification", {"fixture": True})
            session.commit()
            evidence_id, action_id = evidence.id, action.id
        before = snapshot(source)

        def container_client(command, **kwargs):
            command = list(command)
            arguments = {key: value for key, value in kwargs.items() if key != "env"}
            if command[0] == "pg_dump":
                invoked = ["docker", "exec", "-i", container, "pg_dump", "--username", url.username,
                           "--dbname", source_name, *command[1:]]
            else:
                assert command[:2] == ["pg_restore", "--list"]
                invoked = ["docker", "exec", "-i", container, "pg_restore", "--list"]
                arguments["input"] = Path(command[2]).read_bytes()
            return subprocess.run(invoked, **arguments)

        receipt = backup("disposable_ci_source", tmp_path / "private-backups", writers_stopped=True, run=container_client)
        assert receipt["archive_list_verified"] and not receipt["restore_verified"]
        archive = (tmp_path / "private-backups" / receipt["archive"]).read_bytes()
        result = subprocess.run(["docker", "exec", "-i", container, "pg_restore", "--exit-on-error", "--single-transaction",
            "--no-owner", "--no-acl", "--username", url.username, "--dbname", restored_name],
            input=archive, capture_output=True, timeout=60, check=False)
        assert result.returncode == 0, "Fresh isolated PostgreSQL restore failed"
        assert snapshot(source) == before, "Backup must not mutate its source"
        assert snapshot(restored) == before, "All canonical table counts and logical row hashes must survive restoration"
        with restored.begin() as connection:
            grant_runtime_privileges(connection, role=role, password="disposable-fixture-only-strong-password")
        role_created = True
        runtime_url = url.set(database=restored_name, username=role,
                             password="disposable-fixture-only-strong-password").render_as_string(hide_password=False)
        runtime = make_engine(runtime_url)
        with runtime.connect() as connection:
            verify_runtime_role(connection)
            verify_schema_revision(connection)
        # Owner-level trigger protection survives, independently of ACLs.
        with restored.connect() as connection:
            with pytest.raises(DBAPIError):
                connection.execute(text("UPDATE evidence SET confidence=0 WHERE id=:id"), {"id": evidence_id})
            connection.rollback()
        with runtime.connect() as connection:
            with pytest.raises(DBAPIError):
                connection.execute(text("DELETE FROM actions WHERE id=:id"), {"id": action_id})
            connection.rollback()
        assert snapshot(restored) == before
        record_property("backup_restore", "actual_pg17_custom_archive_fresh_db_all_tables_hashes_and_guards_verified")
        record_property("schema_heads", before["schema_heads"])
        record_property("backup_sha256", receipt["sha256"])
        record_property("production_writes", 0)
    finally:
        for engine in (runtime, restored, source):
            if engine is not None:
                engine.dispose()
        for name in reversed(created):
            # Exact generated names created above; no force or broad targets.
            with owner.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{name}"'))
        if role_created:
            with owner.connect() as connection:
                connection.execute(text(f'DROP ROLE "{role}"'))
        owner.dispose()
