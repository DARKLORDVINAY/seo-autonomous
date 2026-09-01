"""Provision the dedicated PostgreSQL runtime role after owner-run migrations."""
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


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
        raise ValueError("Role and schema names must be simple PostgreSQL identifiers")
    return value


def _role_state(connection: Connection, role: str):
    return connection.execute(text("""
        SELECT oid, rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit
        FROM pg_roles WHERE rolname = :role
    """), {"role": role}).mappings().one_or_none()


def _reject_role_authority(connection: Connection, role: str) -> None:
    state = _role_state(connection, role)
    if state is None:
        raise ValueError("Runtime role does not exist")
    if any(state[key] for key in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls", "rolinherit")):
        raise ValueError("Runtime role has administrative or inherited authority")
    membership = connection.scalar(text("SELECT count(*) FROM pg_auth_members WHERE member = :oid"), {"oid": state["oid"]})
    if membership:
        raise ValueError("Runtime role must not have other role memberships")
    owned = connection.scalar(text("""
        SELECT (SELECT count(*) FROM pg_class WHERE relowner = :oid)
             + (SELECT count(*) FROM pg_namespace WHERE nspowner = :oid)
             + (SELECT count(*) FROM pg_proc WHERE proowner = :oid)
             + (SELECT count(*) FROM pg_database WHERE datdba = :oid)
    """), {"oid": state["oid"]})
    if owned:
        raise ValueError("Runtime role must not own database, schema, table, sequence or function objects")


def grant_runtime_privileges(
    connection: Connection, *, role: str, password: str, schema: str = "public",
) -> None:
    """Caller owns the transaction; no automatic future-table mutation privileges.

    This is for an application-dedicated database. PUBLIC grants are removed from
    its schema/database so a runtime role cannot recover rights through PUBLIC.
    Re-run after each migration to grant only the current canonical table set.
    """
    if connection.dialect.name != "postgresql":
        raise ValueError("Runtime role grants require PostgreSQL")
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
        _reject_role_authority(connection, role)
    cursor = connection.connection.driver_connection.cursor()
    try:
        if state is None:
            cursor.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                                   "NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {}").format(
                                       sql.Identifier(role), sql.Literal(password)))
        else:
            cursor.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
        # Connection and schema usage are necessary; schema/database creation and
        # temporary objects are deliberately absent from the runtime capability.
        cursor.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database)))
        cursor.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(sql.Identifier(database), sql.Identifier(role)))
        cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), sql.Identifier(role)))
        cursor.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
        cursor.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(sql.Identifier(schema), sql.Identifier(role)))
        cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
        for recipient in (sql.SQL("PUBLIC"), sql.Identifier(role)):
            cursor.execute(sql.SQL("REVOKE SET, ALTER SYSTEM ON PARAMETER session_replication_role FROM {}").format(recipient))
        for kind in ("TABLES", "SEQUENCES", "FUNCTIONS"):
            for recipient in (sql.SQL("PUBLIC"), sql.Identifier(role)):
                cursor.execute(sql.SQL("REVOKE ALL ON ALL {} IN SCHEMA {} FROM {}").format(
                    sql.SQL(kind), sql.Identifier(schema), recipient))
        cursor.execute(sql.SQL("ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"))
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
            privileges = "SELECT, INSERT" if table in APPEND_ONLY_TABLES else "SELECT, INSERT, UPDATE, DELETE"
            cursor.execute(sql.SQL("GRANT {} ON TABLE {}.{} TO {}").format(
                sql.SQL(privileges), sql.Identifier(schema), sql.Identifier(table), sql.Identifier(role)))
        # Migration version is readable for diagnostics but never writable by the app.
        cursor.execute(sql.SQL("GRANT SELECT ON TABLE {}.alembic_version TO {}").format(
            sql.Identifier(schema), sql.Identifier(role)))
    finally:
        cursor.close()
    verify_runtime_role(connection, role=role, schema=schema)


def verify_runtime_role(connection: Connection, *, role: str | None = None, schema: str = "public") -> None:
    """Fail startup if a supposedly restricted PostgreSQL login can control schema/audit."""
    if connection.dialect.name != "postgresql":
        raise ValueError("Runtime role verification requires PostgreSQL")
    role = role or connection.scalar(text("SELECT current_user"))
    _identifier(role)
    _identifier(schema)
    _reject_role_authority(connection, role)
    trigger_control = connection.scalar(text("""
        SELECT has_parameter_privilege(:role, 'session_replication_role', 'SET')
            OR has_parameter_privilege(:role, 'session_replication_role', 'ALTER SYSTEM')
    """), {"role": role})
    if trigger_control:
        raise ValueError("Runtime role must not control trigger execution settings")
    bad_database = connection.scalar(text("""
        SELECT has_database_privilege(:role, current_database(), 'CREATE')
            OR has_database_privilege(:role, current_database(), 'TEMPORARY')
    """), {"role": role})
    # Any writable non-system schema permits shadowing/malicious object creation.
    bad_schema = connection.scalar(text("""
        SELECT count(*) FROM pg_namespace
        WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'
          AND has_schema_privilege(:role, oid, 'CREATE')
    """), {"role": role})
    if bad_database or bad_schema:
        raise ValueError("Runtime role must not create schemas or temporary/schema objects")
    if not connection.scalar(text("SELECT has_schema_privilege(:role, :schema, 'USAGE')"), {"role": role, "schema": schema}):
        raise ValueError("Runtime role needs usage of the application schema")
    for table in Base.metadata.tables:
        qualified = f'"{schema}"."{table}"'
        permitted = ("SELECT", "INSERT") if table in APPEND_ONLY_TABLES else ("SELECT", "INSERT", "UPDATE", "DELETE")
        forbidden = ("TRUNCATE", "REFERENCES", "TRIGGER") + (("UPDATE", "DELETE") if table in APPEND_ONLY_TABLES else ())
        for privilege in permitted:
            allowed = connection.scalar(text("SELECT has_table_privilege(:role, :table, :privilege)"),
                                        {"role": role, "table": qualified, "privilege": privilege})
            if not allowed:
                raise ValueError("Runtime role is missing required canonical table privileges")
        for privilege in forbidden:
            check = "has_any_column_privilege" if privilege in {"UPDATE", "REFERENCES"} else "has_table_privilege"
            allowed = connection.scalar(text(f"SELECT {check}(:role, :table, :privilege)"),
                                        {"role": role, "table": qualified, "privilege": privilege})
            if allowed:
                raise ValueError("Runtime role has forbidden canonical table privileges")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--schema", default="public")
    args = parser.parse_args(argv)
    engine = None
    try:
        engine = make_engine(os.environ["DATABASE_URL"])
        with engine.begin() as connection:
            if args.verify_only:
                verify_runtime_role(connection, schema=args.schema)
            else:
                grant_runtime_privileges(connection, role=os.environ["POSTGRES_APP_USER"],
                                         password=os.environ["POSTGRES_APP_PASSWORD"], schema=args.schema)
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
