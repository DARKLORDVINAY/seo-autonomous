"""Provision exact API/worker PostgreSQL roles after owner-run migrations."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys

from psycopg import sql
from sqlalchemy import text
from sqlalchemy.engine import Connection

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.db.models import APPEND_ONLY_TABLES, Base
from backend.app.db.session import make_engine


ROLE_PROFILES = ("api", "worker")
STANDARD_TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")

# The worker is an operational scheduler, not an authority issuer. Keep this
# inventory explicit: adding a table without deciding its worker capability is
# a migration/startup failure, not an implicit grant.
WORKER_READ_ONLY_TABLES = frozenset({
    "action_batches",
    "approvals",
    "assumptions",
    "contradictions",
    "mission_states",
    "page_entities",
    "policies",
    "query_clusters",
    "sites",
    "strategy_versions",
    "task_dependencies",
    "task_ownership",
    "verifications",
})
WORKER_INSERT_ONLY_TABLES = frozenset({
    "action_events",
    "actions",
    "agent_findings",
    "ai_search_snapshots",
    "calibration_records",
    "claim_evidence",
    "claims",
    "crawl_issues",
    "crawl_snapshots",
    "decision_logs",
    "evidence",
    "experiment_metrics",
    "failure_cases",
    "ga4_daily",
    "gsc_daily",
    "guardrails",
    "page_versions",
    "queries",
    "revisions",
    "rollback_events",
    "serp_snapshots",
})
WORKER_INSERT_UPDATE_TABLES = frozenset({
    "agent_runs",
    "experiments",
    "job_leases",
    "job_runs",
    "opportunities",
    "pages",
    "tasks",
})
WORKER_INSERT_DELETE_TABLES = frozenset({"execution_leases"})
WORKER_COLUMN_PRIVILEGES = {
    # Provider lookback windows overlap. Only observation values may be
    # refreshed; natural-key identity and tenant/date dimensions stay frozen.
    "gsc_daily": {
        "UPDATE": frozenset({
            "page_id", "clicks", "impressions", "position", "data_state",
            "is_fixture", "quality_flags_json",
        }),
    },
    "ga4_daily": {
        "UPDATE": frozenset({
            "page_id", "sessions", "key_events", "qualified_conversions",
            "conversion_value", "is_fixture", "quality_flags_json",
        }),
    },
    # PostgreSQL SELECT ... FOR UPDATE requires some UPDATE capability. This
    # inert field permits coordination without exposing site policy fields.
    "sites": {"UPDATE": frozenset({"coordination_token"})},
    # Scheduled ingestion refreshes operational availability only; mission
    # objective, autonomy, budgets, constraints and critical path stay frozen.
    "mission_states": {
        "UPDATE": frozenset({"available_resources_json", "blockers_json", "updated_at"}),
    },
}


def _worker_table_inventory() -> frozenset[str]:
    groups = (
        WORKER_READ_ONLY_TABLES,
        WORKER_INSERT_ONLY_TABLES,
        WORKER_INSERT_UPDATE_TABLES,
        WORKER_INSERT_DELETE_TABLES,
    )
    combined: set[str] = set()
    for group in groups:
        overlap = combined & group
        if overlap:
            raise ValueError(f"Worker table capability inventory overlaps: {sorted(overlap)}")
        combined.update(group)
    actual = set(Base.metadata.tables)
    if combined != actual:
        raise ValueError(
            f"Worker table capability inventory mismatch; missing={sorted(actual - combined)}, "
            f"unknown={sorted(combined - actual)}"
        )
    return frozenset(combined)


def table_privileges(profile: str, table: str) -> tuple[str, ...]:
    """Return the exact privileges for one known canonical table/profile."""
    if profile not in ROLE_PROFILES:
        raise ValueError("Runtime role profile must be api or worker")
    if table not in Base.metadata.tables:
        raise ValueError("Unknown canonical table")
    if profile == "api":
        return ("SELECT", "INSERT") if table in APPEND_ONLY_TABLES else ("SELECT", "INSERT", "UPDATE", "DELETE")
    _worker_table_inventory()
    if table in WORKER_READ_ONLY_TABLES:
        return ("SELECT",)
    if table in WORKER_INSERT_ONLY_TABLES:
        return ("SELECT", "INSERT")
    if table in WORKER_INSERT_UPDATE_TABLES:
        return ("SELECT", "INSERT", "UPDATE")
    if table in WORKER_INSERT_DELETE_TABLES:
        return ("SELECT", "INSERT", "DELETE")
    raise ValueError("Worker table capability is not classified")


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
        raise ValueError("Role and schema names must be simple PostgreSQL identifiers")
    return value


def _role_state(connection: Connection, role: str | None):
    return connection.execute(text("""
        SELECT r.oid, r.rolname, r.rolcanlogin, r.rolsuper, r.rolcreatedb, r.rolcreaterole,
               r.rolreplication, r.rolbypassrls, r.rolinherit, r.rolconnlimit,
               r.rolvaliduntil IS NULL OR r.rolvaliduntil > CURRENT_TIMESTAMP AS login_unexpired,
               EXISTS (SELECT 1 FROM pg_auth_members am WHERE am.member = r.oid) AS has_membership,
               (SELECT count(*) FROM pg_class c WHERE c.relowner = r.oid)
                 + (SELECT count(*) FROM pg_namespace n WHERE n.nspowner = r.oid)
                 + (SELECT count(*) FROM pg_proc p WHERE p.proowner = r.oid)
                 + (SELECT count(*) FROM pg_database d WHERE d.datdba = r.oid) AS owned_count
        FROM pg_roles r WHERE r.rolname = COALESCE(CAST(:role AS text), current_user)
    """), {"role": role}).mappings().one_or_none()


def _reject_role_authority(
    connection: Connection, role: str | None, *, require_runtime_state: bool = True,
) -> str:
    state = _role_state(connection, role)
    if state is None:
        raise ValueError("Runtime role does not exist")
    if any(state[key] for key in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")):
        raise ValueError("Runtime role has administrative authority")
    if state["has_membership"]:
        raise ValueError("Runtime role must not have other role memberships")
    if state["owned_count"]:
        raise ValueError("Runtime role must not own database, schema, table, sequence or function objects")
    if require_runtime_state:
        if not state["rolcanlogin"] or state["rolconnlimit"] == 0 or not state["login_unexpired"]:
            raise ValueError("Runtime role must retain an available dedicated login capability")
        if state["rolinherit"]:
            raise ValueError("Runtime role must not inherit authority")
    return state["rolname"]


def grant_runtime_privileges(
    connection: Connection, *, role: str, password: str, schema: str = "public", profile: str = "api",
) -> None:
    """Caller owns the transaction; no automatic future-table mutation privileges.

    This is for an application-dedicated PostgreSQL cluster. PUBLIC grants are
    removed from its application schema/database so a runtime role cannot
    recover rights through PUBLIC. Other-database isolation is verified but
    deliberately not mutated here. Re-run after each migration to grant only
    the current canonical table set.
    """
    if connection.dialect.name != "postgresql":
        raise ValueError("Runtime role grants require PostgreSQL")
    if profile not in ROLE_PROFILES:
        raise ValueError("Runtime role profile must be api or worker")
    if profile == "worker":
        _worker_table_inventory()
    role, schema = _identifier(role), _identifier(schema)
    if len(password) < 32 or "\x00" in password:
        raise ValueError("Runtime password must contain at least 32 characters")
    if connection.engine.url.password and password == connection.engine.url.password:
        raise ValueError("Migration and runtime passwords must be distinct")
    current_role = connection.scalar(text("SELECT current_user"))
    if current_role == role:
        raise ValueError("Migration owner and runtime role must be distinct")
    database = connection.scalar(text("SELECT current_database()"))
    state = _role_state(connection, role)
    if state is not None:
        # Availability drift and INHERIT without memberships are repairable;
        # administrative authority, membership, or object ownership are not.
        _reject_role_authority(connection, role, require_runtime_state=False)
    non_system_schemas = list(connection.scalars(text("""
        SELECT nspname FROM pg_namespace
         WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'
         ORDER BY nspname
    """)))
    if schema not in non_system_schemas:
        raise ValueError("Application schema must be an existing non-system schema")
    cursor = connection.connection.driver_connection.cursor()
    try:
        if state is None:
            cursor.execute(sql.SQL("CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                                   "NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1 "
                                   "VALID UNTIL 'infinity' PASSWORD {}").format(
                                       sql.Identifier(role), sql.Literal(password)))
        else:
            cursor.execute(sql.SQL("ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                                   "NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT -1 "
                                   "VALID UNTIL 'infinity' PASSWORD {}").format(
                                       sql.Identifier(role), sql.Literal(password)))
        # A stale owner-authored role default is executable configuration. In
        # particular session_replication_role=replica disables ordinary audit
        # and FK triggers at login. Remove both applicable role scopes first.
        cursor.execute(sql.SQL("ALTER ROLE {} RESET ALL").format(sql.Identifier(role)))
        cursor.execute(sql.SQL("ALTER ROLE {} IN DATABASE {} RESET ALL").format(
            sql.Identifier(role), sql.Identifier(database),
        ))
        cursor.execute(sql.SQL("ALTER ROLE {} SET search_path TO {}").format(
            sql.Identifier(role), sql.Identifier(schema),
        ))
        # Connection and schema usage are necessary; schema/database creation and
        # temporary objects are deliberately absent from the runtime capability.
        cursor.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)))
        cursor.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(sql.Identifier(database), sql.Identifier(role)))
        cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), sql.Identifier(role)))
        # Qualified names bypass search_path. Remove effective access throughout
        # every non-system schema so an out-of-schema SECURITY DEFINER routine,
        # view, foreign table, or sequence cannot become a side door.
        for namespace in non_system_schemas:
            for recipient in (sql.SQL("PUBLIC"), sql.Identifier(role)):
                cursor.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(
                    sql.Identifier(namespace), recipient,
                ))
                for kind in ("TABLES", "SEQUENCES", "ROUTINES"):
                    cursor.execute(sql.SQL("REVOKE ALL ON ALL {} IN SCHEMA {} FROM {}").format(
                        sql.SQL(kind), sql.Identifier(namespace), recipient,
                    ))
        cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
        for recipient in (sql.SQL("PUBLIC"), sql.Identifier(role)):
            cursor.execute(sql.SQL("REVOKE SET, ALTER SYSTEM ON PARAMETER session_replication_role FROM {}").format(recipient))
        cursor.execute(sql.SQL("ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON ROUTINES FROM PUBLIC"))
        for recipient in (sql.SQL("PUBLIC"), sql.Identifier(role)):
            cursor.execute(sql.SQL("ALTER DEFAULT PRIVILEGES REVOKE ALL ON TABLES FROM {}").format(recipient))
            cursor.execute(sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA {} REVOKE ALL ON TABLES FROM {}").format(
                sql.Identifier(schema), recipient))
        for table in sorted(Base.metadata.tables):
            # Table REVOKE alone does not remove historical column grants.
            columns = sql.SQL(", ").join(sql.Identifier(column.name) for column in Base.metadata.tables[table].columns)
            for recipient in (sql.SQL("PUBLIC"), sql.Identifier(role)):
                cursor.execute(sql.SQL("REVOKE SELECT ({}), INSERT ({}), UPDATE ({}), REFERENCES ({}) "
                                       "ON TABLE {}.{} FROM {}").format(
                    columns, columns, columns, columns, sql.Identifier(schema), sql.Identifier(table), recipient))
            privileges = ", ".join(table_privileges(profile, table))
            cursor.execute(sql.SQL("GRANT {} ON TABLE {}.{} TO {}").format(
                sql.SQL(privileges), sql.Identifier(schema), sql.Identifier(table), sql.Identifier(role)))
            if profile == "worker":
                for privilege, names in WORKER_COLUMN_PRIVILEGES.get(table, {}).items():
                    column_list = sql.SQL(", ").join(sql.Identifier(name) for name in sorted(names))
                    cursor.execute(sql.SQL("GRANT {} ({}) ON TABLE {}.{} TO {}").format(
                        sql.SQL(privilege), column_list, sql.Identifier(schema), sql.Identifier(table),
                        sql.Identifier(role),
                    ))
        # Migration version is readable for diagnostics but never writable by the app.
        for recipient in (sql.SQL("PUBLIC"), sql.Identifier(role)):
            cursor.execute(sql.SQL(
                "REVOKE SELECT (version_num), INSERT (version_num), UPDATE (version_num), "
                "REFERENCES (version_num) ON TABLE {}.alembic_version FROM {}"
            ).format(sql.Identifier(schema), recipient))
        cursor.execute(sql.SQL("GRANT SELECT ON TABLE {}.alembic_version TO {}").format(
            sql.Identifier(schema), sql.Identifier(role)))
    finally:
        cursor.close()
    verify_runtime_role(connection, role=role, schema=schema, profile=profile)


def verify_runtime_role(
    connection: Connection, *, role: str | None = None, schema: str = "public", profile: str = "api",
) -> None:
    """Fail startup if a supposedly restricted PostgreSQL login can control schema/audit."""
    if connection.dialect.name != "postgresql":
        raise ValueError("Runtime role verification requires PostgreSQL")
    if profile not in ROLE_PROFILES:
        raise ValueError("Runtime role profile must be api or worker")
    if profile == "worker":
        _worker_table_inventory()
    current_session_role = role is None
    if role is not None:
        _identifier(role)
    _identifier(schema)
    role = _identifier(_reject_role_authority(connection, role))
    authority = connection.execute(text("""
        SELECT
          has_parameter_privilege(:role, 'session_replication_role', 'SET')
            OR has_parameter_privilege(:role, 'session_replication_role', 'ALTER SYSTEM') AS trigger_control,
          current_setting('session_replication_role') = 'origin' AS trigger_execution_origin,
          has_database_privilege(:role, current_database(), 'CREATE')
            OR has_database_privilege(:role, current_database(), 'TEMPORARY') AS database_create,
          has_database_privilege(:role, current_database(), 'CONNECT') AS database_connect,
          (SELECT count(*) FROM pg_database other_database
            WHERE other_database.datname <> current_database()
              AND other_database.datallowconn
              AND has_database_privilege(:role, other_database.oid, 'CONNECT'))
            AS other_database_connect,
          (SELECT count(*) FROM pg_database other_database
            WHERE other_database.datname <> current_database()
              AND other_database.datallowconn
              AND has_database_privilege(:role, other_database.oid, 'TEMPORARY'))
            AS other_database_temporary,
          (SELECT count(*) FROM pg_namespace writable
             WHERE has_schema_privilege(:role, writable.oid, 'CREATE')) AS writable_schemas,
          has_schema_privilege(:role, :schema, 'USAGE') AS schema_usage,
          (SELECT count(*) FROM pg_namespace readable
             WHERE readable.nspname !~ '^pg_' AND readable.nspname <> 'information_schema'
               AND readable.nspname <> :schema
               AND has_schema_privilege(:role, readable.oid, 'USAGE')) AS unexpected_schema_usage,
          current_schemas(false) = ARRAY[CAST(:schema AS name)] AS exact_search_path,
          (SELECT count(*) = 1 AND bool_and(
                    role_setting.setrole = (SELECT oid FROM pg_roles WHERE rolname = :role)
                AND role_setting.setdatabase = 0
                AND config.value = 'search_path=' || quote_ident(:schema))
             FROM pg_db_role_setting role_setting
             CROSS JOIN LATERAL unnest(role_setting.setconfig) AS config(value)
            WHERE (role_setting.setrole = (SELECT oid FROM pg_roles WHERE rolname = :role)
                   AND role_setting.setdatabase IN (0, (SELECT oid FROM pg_database WHERE datname = current_database())))
               OR (role_setting.setrole = 0
                   AND role_setting.setdatabase IN (0, (SELECT oid FROM pg_database WHERE datname = current_database())))
          ) AS exact_configured_defaults,
          EXISTS (SELECT 1 FROM pg_class sequence
                    JOIN pg_namespace ns ON ns.oid = sequence.relnamespace
                   WHERE ns.nspname !~ '^pg_' AND ns.nspname <> 'information_schema'
                     AND sequence.relkind = 'S'
                     AND (has_sequence_privilege(:role, sequence.oid, 'USAGE')
                       OR has_sequence_privilege(:role, sequence.oid, 'SELECT')
                       OR has_sequence_privilege(:role, sequence.oid, 'UPDATE'))) AS sequence_access,
          EXISTS (SELECT 1 FROM pg_proc routine
                    JOIN pg_namespace ns ON ns.oid = routine.pronamespace
                   WHERE ns.nspname !~ '^pg_' AND ns.nspname <> 'information_schema'
                     AND has_function_privilege(:role, routine.oid, 'EXECUTE')) AS function_access,
          EXISTS (
              SELECT 1
                FROM (
                  SELECT namespace.nspacl AS acl
                    FROM pg_namespace namespace
                   WHERE namespace.nspname ~ '^pg_' OR namespace.nspname = 'information_schema'
                  UNION ALL
                  SELECT relation.relacl
                    FROM pg_class relation
                    JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
                   WHERE namespace.nspname ~ '^pg_' OR namespace.nspname = 'information_schema'
                  UNION ALL
                  SELECT attribute.attacl
                    FROM pg_attribute attribute
                    JOIN pg_class relation ON relation.oid = attribute.attrelid
                    JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
                   WHERE (namespace.nspname ~ '^pg_' OR namespace.nspname = 'information_schema')
                     AND attribute.attnum > 0 AND NOT attribute.attisdropped
                  UNION ALL
                  SELECT routine.proacl
                    FROM pg_proc routine
                    JOIN pg_namespace namespace ON namespace.oid = routine.pronamespace
                   WHERE namespace.nspname ~ '^pg_' OR namespace.nspname = 'information_schema'
                ) system_object_acl
                CROSS JOIN LATERAL aclexplode(system_object_acl.acl) grant_entry
               WHERE grant_entry.grantee = (SELECT oid FROM pg_roles WHERE rolname = :role)
            ) AS direct_system_object_acl
    """), {"role": role, "schema": schema}).mappings().one()
    if authority["trigger_control"]:
        raise ValueError("Runtime role must not control trigger execution settings")
    if current_session_role and not authority["trigger_execution_origin"]:
        raise ValueError("Runtime role must execute ordinary integrity triggers")
    if authority["database_create"] or authority["writable_schemas"]:
        raise ValueError("Runtime role must not create schemas or temporary/schema objects")
    if not authority["database_connect"]:
        raise ValueError("Runtime role needs database connection capability")
    if authority["other_database_connect"] or authority["other_database_temporary"]:
        raise ValueError(
            "Runtime role can access another database; an application-dedicated PostgreSQL cluster "
            "with PUBLIC CONNECT/TEMPORARY revoked outside the selected database is required"
        )
    if not authority["schema_usage"]:
        raise ValueError("Runtime role needs usage of the application schema")
    if authority["unexpected_schema_usage"]:
        raise ValueError("Runtime role must not use other non-system schemas")
    if not authority["exact_configured_defaults"]:
        raise ValueError("Runtime role must have only the pinned application search path default")
    if current_session_role and not authority["exact_search_path"]:
        raise ValueError("Runtime role search path must resolve only the application schema")
    if authority["sequence_access"] or authority["function_access"]:
        raise ValueError("Runtime role must not use non-system sequences or routines")
    if authority["direct_system_object_acl"]:
        raise ValueError(
            "Runtime role has forbidden direct ACL entries on system schemas, relations, columns or routines"
        )

    # One bounded catalogue query replaces thousands of remote scalar probes.
    rows = connection.execute(text("""
        SELECT namespace.nspname AS schema_name, relation.relname AS table_name,
          attribute.attname AS column_name,
          has_table_privilege(:role, relation.oid, 'SELECT') AS table_select,
          has_table_privilege(:role, relation.oid, 'INSERT') AS table_insert,
          has_table_privilege(:role, relation.oid, 'UPDATE') AS table_update,
          has_table_privilege(:role, relation.oid, 'DELETE') AS table_delete,
          has_table_privilege(:role, relation.oid, 'TRUNCATE') AS table_truncate,
          has_table_privilege(:role, relation.oid, 'REFERENCES') AS table_references,
          has_table_privilege(:role, relation.oid, 'TRIGGER') AS table_trigger,
          has_column_privilege(:role, relation.oid, attribute.attnum, 'SELECT') AS column_select,
          has_column_privilege(:role, relation.oid, attribute.attnum, 'INSERT') AS column_insert,
          has_column_privilege(:role, relation.oid, attribute.attnum, 'UPDATE') AS column_update,
          has_column_privilege(:role, relation.oid, attribute.attnum, 'REFERENCES') AS column_references
        FROM pg_class relation
        JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
        LEFT JOIN pg_attribute attribute ON attribute.attrelid = relation.oid
          AND attribute.attnum > 0 AND NOT attribute.attisdropped
        WHERE namespace.nspname !~ '^pg_' AND namespace.nspname <> 'information_schema'
          AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
        ORDER BY namespace.nspname, relation.relname, attribute.attnum
    """), {"role": role, "schema": schema}).mappings().all()
    observed_tables = {row["table_name"] for row in rows if row["schema_name"] == schema}
    expected_tables = set(Base.metadata.tables) | {"alembic_version"}
    if not expected_tables <= observed_tables:
        raise ValueError("Runtime role cannot verify every required canonical table")
    table_keys = {
        "SELECT": "table_select",
        "INSERT": "table_insert",
        "UPDATE": "table_update",
        "DELETE": "table_delete",
        "TRUNCATE": "table_truncate",
        "REFERENCES": "table_references",
        "TRIGGER": "table_trigger",
    }
    column_keys = {
        "SELECT": "column_select",
        "INSERT": "column_insert",
        "UPDATE": "column_update",
        "REFERENCES": "column_references",
    }
    for row in rows:
        table = row["table_name"]
        in_application_schema = row["schema_name"] == schema
        if in_application_schema and table == "alembic_version":
            expected = {"SELECT"}
            scoped = {}
        elif in_application_schema and table in Base.metadata.tables:
            expected = set(table_privileges(profile, table))
            scoped = WORKER_COLUMN_PRIVILEGES.get(table, {}) if profile == "worker" else {}
        else:
            expected = set()
            scoped = {}
        for privilege, key in table_keys.items():
            allowed = bool(row[key])
            if allowed != (privilege in expected):
                state = "missing required" if privilege in expected else "has forbidden"
                raise ValueError(f"Runtime {profile} role {state} canonical table privileges")
        if row["column_name"] is not None:
            for privilege, key in column_keys.items():
                expected_column = privilege in expected or row["column_name"] in scoped.get(privilege, ())
                allowed = bool(row[key])
                if allowed != expected_column:
                    state = "missing required" if expected_column else "has forbidden"
                    raise ValueError(f"Runtime {profile} role {state} canonical column privileges")


def provision_runtime_roles(
    connection: Connection, *, api_role: str, api_password: str, worker_role: str, worker_password: str,
    schema: str = "public",
) -> None:
    """Atomically provision distinct API and worker identities from the owner."""
    api_role, worker_role = _identifier(api_role), _identifier(worker_role)
    if api_role == worker_role:
        raise ValueError("API and worker database roles must differ")
    if api_password == worker_password:
        raise ValueError("API and worker database passwords must differ")
    grant_runtime_privileges(connection, role=api_role, password=api_password, schema=schema, profile="api")
    grant_runtime_privileges(connection, role=worker_role, password=worker_password, schema=schema, profile="worker")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--profile", choices=ROLE_PROFILES, default="api")
    args = parser.parse_args(argv)
    engine = None
    try:
        from scripts.deployment_preflight import guard_migration_connection, required_migration_target

        environment = os.environ.get("ENVIRONMENT", "production")
        expected_target = required_migration_target(environment=environment)
        engine = make_engine(
            os.environ["DATABASE_URL"], environment=environment,
        )
        with engine.begin() as connection:
            guard_migration_connection(connection, expected_target)
            if args.verify_only:
                verify_runtime_role(connection, schema=args.schema, profile=args.profile)
            else:
                prefix = "POSTGRES_API" if args.profile == "api" else "POSTGRES_WORKER"
                grant_runtime_privileges(connection, role=os.environ[f"{prefix}_USER"],
                                         password=os.environ[f"{prefix}_PASSWORD"], schema=args.schema,
                                         profile=args.profile)
        print("Runtime role privileges verified.")
        return 0
    except Exception as error:
        print(f"Runtime role operation stopped ({type(error).__name__}); no credentials displayed.")
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
