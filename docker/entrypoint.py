"""Construct safely escaped database URLs without shell expansion or secret output."""
from __future__ import annotations

import os
import sys

from sqlalchemy.engine import URL

from backend.app.db.transport import validate_database_transport


def database_url_from_environment(*, environment: str | None = None) -> str:
    if os.environ.get("POSTGRES_HOST"):
        required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
        if any(not os.environ.get(name) for name in required):
            raise ValueError("PostgreSQL login configuration is incomplete")
        query = {"sslmode": os.environ["POSTGRES_SSLMODE"]} if os.environ.get("POSTGRES_SSLMODE") else {}
        url = URL.create("postgresql+psycopg", username=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"], host=os.environ["POSTGRES_HOST"],
            port=int(os.environ.get("POSTGRES_PORT", "5432")), database=os.environ["POSTGRES_DB"],
            query=query).render_as_string(hide_password=False)
    else:
        url = os.environ["DATABASE_URL"]
    return validate_database_transport(url, environment=environment)


def verify_database_role(profile: str = "api") -> None:
    """Fail before service exec unless runtime ACLs and schema match the image."""
    from backend.app.db.readiness import verify_database_readiness
    from backend.app.db.session import make_engine

    environment = os.environ.get("ENVIRONMENT", "development")
    engine = make_engine(os.environ["DATABASE_URL"], environment=environment)
    try:
        with engine.connect() as connection:
            verify_database_readiness(connection, environment=environment, profile=profile)
    finally:
        engine.dispose()


def migration_release_target(*, environment: str | None):
    """Validate mandatory production release/target pins before constructing the DSN."""
    # Keep this check before DATABASE_URL construction, engine creation, and
    # migrate()/grant calls. The verification overlay uses SEO_RELEASE_IMAGE as
    # both the Compose image selector and the value injected into this process.
    from scripts.deployment_preflight import required_migration_target

    return required_migration_target(environment=environment)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mode = args.pop(0) if args else "api"
    if mode not in {"api", "worker", "migrate", "bootstrap", "mcp", "preflight"}:
        raise ValueError("Unknown container service mode")
    if mode in {"api", "worker"}:
        # The executable role, not operator-controlled environment text, selects
        # whether bearer capabilities are valid for this process.
        os.environ["SERVICE_ROLE"] = mode
    elif mode != "mcp":
        os.environ["SERVICE_ROLE"] = "utility"
    database_environment = os.environ.get("ENVIRONMENT")
    if mode == "migrate" and database_environment is None:
        # This image's migration mode provisions production PostgreSQL roles;
        # it is never a generic local SQLite/Alembic command.
        database_environment = "production"
    migration_target = (
        migration_release_target(environment=database_environment)
        if mode == "migrate" else None
    )
    if mode != "mcp":
        os.environ["DATABASE_URL"] = database_url_from_environment(environment=database_environment)
    if mode == "migrate":
        from scripts.bootstrap import migrate
        from scripts.deployment_preflight import pin_migration_search_path
        from scripts.deployment_preflight import preflight_migration_target
        from scripts.grant_runtime import provision_runtime_roles

        if migration_target is not None:
            preflight_migration_target(
                os.environ["DATABASE_URL"], migration_target, environment=database_environment,
            )
        def provision(connection):
            # Defense in depth: keep the exact schema pin active for the grant
            # callback in the same serialized transaction as Alembic.
            pin_migration_search_path(connection)
            provision_runtime_roles(
                connection,
                api_role=os.environ["POSTGRES_API_USER"],
                api_password=os.environ["POSTGRES_API_PASSWORD"],
                worker_role=os.environ["POSTGRES_WORKER_USER"],
                worker_password=os.environ["POSTGRES_WORKER_PASSWORD"],
            )

        migrate(
            os.environ["DATABASE_URL"],
            environment=database_environment,
            expected_target=migration_target,
            post_migration=provision,
        )
        print("Migrations and restricted API/worker grants completed.")
        return 0
    if mode in {"api", "worker", "bootstrap", "preflight"}:
        verify_database_role("worker" if mode == "worker" else "api")
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
