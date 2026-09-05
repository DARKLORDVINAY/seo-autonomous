"""Read-only preflight for the locked verification deployment package."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
import re

from sqlalchemy import select, text

from backend.app.config.settings import Settings
from backend.app.db import models as m
from backend.app.db.readiness import verify_schema_revision
from backend.app.db.session import make_engine
from backend.app.db.transport import normalize_database_environment
from scripts.grant_runtime import verify_runtime_role


DATABASE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
SERVER_IDENTITY = re.compile(r"[1-9][0-9]{0,19}")
SCHEMA_HEAD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
UNINITIALIZED_SCHEMA = "uninitialized"
MIGRATION_SCHEMA = "public"
MIGRATION_LOCK_KEY = 7_311_091_880_431_117_337


@dataclass(frozen=True)
class MigrationTarget:
    database: str
    server_identity: str
    mode: str
    schema_heads: tuple[str, ...]


def checked_migration_target(
    *, database: str, server_identity: str, mode: str, schema_heads: str,
) -> MigrationTarget:
    """Validate independently supplied target pins without consulting the DSN."""
    if not DATABASE_NAME.fullmatch(database) or database.lower() in {"postgres", "template0", "template1"}:
        raise ValueError("Expected migration database must be one explicit bounded name")
    if not SERVER_IDENTITY.fullmatch(server_identity) or int(server_identity) >= 2**64:
        raise ValueError("Expected PostgreSQL system identifier must be one explicit unsigned value")
    if mode not in {"bootstrap", "upgrade"}:
        raise ValueError("Migration mode must be the exact literal bootstrap or upgrade")
    if mode == "bootstrap":
        if schema_heads != UNINITIALIZED_SCHEMA:
            raise ValueError("Bootstrap migration requires the explicit uninitialized schema state")
        heads: tuple[str, ...] = ()
    else:
        parts = tuple(schema_heads.split(","))
        if (not parts or UNINITIALIZED_SCHEMA in parts or parts != tuple(sorted(set(parts)))
                or any(not SCHEMA_HEAD.fullmatch(head) for head in parts)):
            raise ValueError("Upgrade migration requires sorted, unique predecessor schema heads")
        heads = parts
    return MigrationTarget(
        database=database,
        server_identity=server_identity,
        mode=mode,
        schema_heads=heads,
    )


def migration_target_from_environment(values: Mapping[str, str] | None = None) -> MigrationTarget:
    values = os.environ if values is None else values
    return checked_migration_target(
        database=values.get("SEO_MIGRATION_EXPECTED_DATABASE", ""),
        server_identity=values.get("SEO_MIGRATION_EXPECTED_SYSTEM_IDENTIFIER", ""),
        mode=values.get("SEO_MIGRATION_MODE", ""),
        schema_heads=values.get("SEO_MIGRATION_EXPECTED_SCHEMA_HEADS", ""),
    )


def required_migration_target(
    *, environment: str | None, explicit: MigrationTarget | None = None,
    values: Mapping[str, str] | None = None,
) -> MigrationTarget | None:
    """Require release/target pins for every production owner migration."""
    values = os.environ if values is None else values
    verification_only = values.get("VERIFICATION_ONLY")
    if verification_only not in {None, "false", "true"}:
        raise ValueError("VERIFICATION_ONLY must be the exact literal true or false")
    normalized = normalize_database_environment(environment)
    if verification_only == "true" and normalized != "production":
        raise ValueError("Verification-only migration requires the production environment")
    if normalized != "production":
        return explicit
    checked_image(values.get("SEO_RELEASE_IMAGE", ""))
    configured = migration_target_from_environment(values)
    if explicit is not None and explicit != configured:
        raise ValueError("Injected migration target differs from the independent environment pins")
    return configured


def pin_migration_search_path(connection) -> None:
    """Override role/database defaults before any unqualified PostgreSQL DDL."""
    if connection.dialect.name != "postgresql":
        return
    connection.exec_driver_sql(f"SET LOCAL search_path TO {MIGRATION_SCHEMA}")
    schemas = connection.execute(text("SELECT pg_catalog.current_schemas(false)")).scalar_one()
    if tuple(schemas) != (MIGRATION_SCHEMA,):
        raise ValueError("Owner migration search path could not be pinned to the application schema")


def acquire_migration_lock(connection) -> None:
    """Serialize owner migrations without waiting behind an unbounded operation."""
    if connection.dialect.name != "postgresql":
        return
    acquired = connection.execute(text(
        "SELECT pg_catalog.pg_try_advisory_xact_lock(:key)"
    ), {"key": MIGRATION_LOCK_KEY}).scalar_one()
    if acquired is not True:
        raise ValueError("Another owner migration already holds the release lock")


def verify_migration_target(connection, expected: MigrationTarget) -> dict:
    """Compare read-only target observations with independent operator pins."""
    if connection.dialect.name != "postgresql":
        raise ValueError("Pinned verification migrations require PostgreSQL")
    database, server_identity = connection.execute(text("""
        SELECT pg_catalog.current_database(), control.system_identifier::text
        FROM pg_catalog.pg_control_system() AS control
    """)).one()
    if database != expected.database or server_identity != expected.server_identity:
        raise ValueError("Migration database or PostgreSQL system identity differs from the release pin")

    version_table_kind = connection.execute(text("""
        SELECT relation.relkind
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = 'public' AND relation.relname = 'alembic_version'
    """)).scalar_one_or_none()
    version_table_exists = version_table_kind is not None
    if expected.mode == "bootstrap":
        objects = tuple(connection.execute(text("""
            SELECT object_kind || ':' || object_name
            FROM (
              SELECT 'relation' AS object_kind, relation.relname AS object_name
              FROM pg_catalog.pg_class AS relation
              JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
              WHERE namespace.nspname = 'public'
              UNION ALL
              SELECT 'routine', routine.proname
              FROM pg_catalog.pg_proc AS routine
              JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = routine.pronamespace
              WHERE namespace.nspname = 'public'
              UNION ALL
              SELECT 'type', type.typname
              FROM pg_catalog.pg_type AS type
              JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = type.typnamespace
              WHERE namespace.nspname = 'public'
              UNION ALL
              SELECT 'extension', extension.extname
              FROM pg_catalog.pg_extension AS extension
              JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = extension.extnamespace
              WHERE namespace.nspname = 'public'
            ) AS public_objects
            ORDER BY object_kind, object_name
        """)).scalars())
        if version_table_exists or objects:
            raise ValueError("Bootstrap migration requires an uninitialized empty public schema")
        actual_heads: tuple[str, ...] = ()
    else:
        if not version_table_exists:
            raise ValueError("Upgrade migration requires an existing predecessor schema")
        if version_table_kind not in {"r", "p"}:
            raise ValueError("Migration version marker must be an ordinary public table")
        actual_heads = tuple(connection.execute(text(
            "SELECT version_num FROM public.alembic_version ORDER BY version_num"
        )).scalars())
        if actual_heads != expected.schema_heads:
            raise ValueError("Current schema heads differ from the independently pinned predecessor")
    return {
        "database": database,
        "server_identity": server_identity,
        "mode": expected.mode,
        "schema_heads": actual_heads,
        "migration_schema": MIGRATION_SCHEMA,
    }


def guard_migration_connection(connection, expected: MigrationTarget | None = None) -> dict | None:
    """Pin schema, serialize, and recheck the exact target on the DDL connection."""
    pin_migration_search_path(connection)
    acquire_migration_lock(connection)
    return verify_migration_target(connection, expected) if expected is not None else None


def preflight_migration_target(database_url: str, expected: MigrationTarget, *, environment: str) -> dict:
    """Verify target identity in an explicitly read-only transaction before migration."""
    engine = make_engine(database_url, environment=environment)
    try:
        with engine.connect() as connection:
            if connection.dialect.name != "postgresql":
                raise ValueError("Pinned verification migrations require PostgreSQL")
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            read_only = connection.execute(text(
                "SELECT pg_catalog.current_setting('transaction_read_only')"
            )).scalar_one()
            if read_only != "on":
                raise ValueError("Migration identity preflight could not enter read-only mode")
            pin_migration_search_path(connection)
            result = verify_migration_target(connection, expected)
            connection.rollback()
            return result
    finally:
        engine.dispose()


def checked_image(value: str) -> str:
    if not re.fullmatch(r"(?:[a-zA-Z0-9][a-zA-Z0-9._:/-]*@)?sha256:[0-9a-f]{64}", value):
        raise ValueError("An immutable image digest is required; mutable tags are not release pins")
    return value


def preflight(settings: Settings, image: str) -> dict:
    if not settings.verification_only or settings.environment != "production":
        raise ValueError("Preflight requires the production verification-only package")
    checked_image(image)
    engine = make_engine(settings.database_url, environment=settings.environment)
    try:
        with engine.connect() as connection:
            verify_runtime_role(connection)
            heads = verify_schema_revision(connection)
            sites = connection.execute(select(m.Site.autonomy_level, m.Site.production_enabled, m.Site.config_json)).all()
            for level, enabled, config in sites:
                if level != 1 or enabled or config.get("earned_categories"):
                    raise ValueError("Canonical site authority is incompatible with verification-only deployment")
    finally:
        engine.dispose()
    return {"status": "verified", "schema_heads": heads, "image": image,
            "site_count": len(sites), "autonomy_level": 1, "production_enabled": False,
            "production_write_budget": 0, "paid_api_budget_usd": 0,
            "provider_mode": "fixture", "worker_started": False,
            "limits": ["not a durable-host or backup attestation", "provider access remains disconnected"]}


def main() -> int:
    try:
        report = preflight(Settings(), os.environ.get("SEO_RELEASE_IMAGE", ""))
    except Exception as exc:
        # SQL/provider exception messages can include connection details.
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
