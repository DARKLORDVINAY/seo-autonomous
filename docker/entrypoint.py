"""Construct safely escaped database URLs without shell expansion or secret output."""
from __future__ import annotations

import os
import sys

from sqlalchemy.engine import URL


def database_url_from_environment() -> str:
    if os.environ.get("POSTGRES_HOST"):
        required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
        if any(not os.environ.get(name) for name in required):
            raise ValueError("PostgreSQL login configuration is incomplete")
        return URL.create("postgresql+psycopg", username=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"], host=os.environ["POSTGRES_HOST"],
            port=int(os.environ.get("POSTGRES_PORT", "5432")), database=os.environ["POSTGRES_DB"]).render_as_string(hide_password=False)
    return os.environ["DATABASE_URL"]


def verify_database_role() -> None:
    from backend.app.db.session import make_engine
    from scripts.grant_runtime import verify_runtime_role

    engine = make_engine(os.environ["DATABASE_URL"])
    try:
        with engine.connect() as connection:
            verify_runtime_role(connection)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mode = args.pop(0) if args else "api"
    if mode not in {"api", "worker", "migrate", "bootstrap", "mcp", "preflight"}:
        raise ValueError("Unknown container service mode")
    if mode != "mcp":
        os.environ["DATABASE_URL"] = database_url_from_environment()
    if mode == "migrate":
        from scripts.bootstrap import migrate
        from scripts.grant_runtime import grant_runtime_privileges
        from backend.app.db.session import make_engine

        if os.environ["POSTGRES_USER"] == os.environ["POSTGRES_APP_USER"]:
            raise ValueError("Migration and runtime logins must differ")
        if os.environ["POSTGRES_PASSWORD"] == os.environ["POSTGRES_APP_PASSWORD"]:
            raise ValueError("Migration and runtime passwords must differ")
        migrate(os.environ["DATABASE_URL"])
        engine = make_engine(os.environ["DATABASE_URL"])
        try:
            with engine.begin() as connection:
                grant_runtime_privileges(connection, role=os.environ["POSTGRES_APP_USER"],
                                         password=os.environ["POSTGRES_APP_PASSWORD"])
        finally:
            engine.dispose()
        print("Migrations and restricted runtime grants completed.")
        return 0
    if mode in {"api", "bootstrap", "preflight"}:
        verify_database_role()
    commands = {
        "api": [sys.executable, "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"],
        "worker": [sys.executable, "-m", "backend.app.scheduler"],
        "bootstrap": [sys.executable, "-m", "scripts.bootstrap"],
        "preflight": [sys.executable, "-m", "scripts.deployment_preflight"],
        "mcp": [sys.executable, "-m", "seo_mcp.server", "--transport", "streamable-http"],
    }
    command = commands[mode] + args
    os.execv(command[0], command)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Container startup stopped ({type(error).__name__}); inspect configured roles and required settings.")
        raise SystemExit(1)
