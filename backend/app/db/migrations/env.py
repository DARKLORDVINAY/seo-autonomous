from logging.config import fileConfig
import os

from alembic import context

from backend.app.db.models import Base
from backend.app.db.session import make_engine
from backend.app.db.transport import validate_database_transport
from scripts.deployment_preflight import guard_migration_connection, required_migration_target

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def _url():
    url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return validate_database_transport(url)


def _environment():
    return config.attributes.get("database_environment", os.environ.get("ENVIRONMENT"))


def _migration_target():
    return required_migration_target(
        environment=_environment(),
        explicit=config.attributes.get("migration_target"),
    )


def run_migrations_offline():
    migration_url = _url()
    if (_environment() or "development").strip().lower() == "production":
        # Static SQL cannot observe or bind a live database/system/schema
        # identity, so it is never an authorized production migration path.
        _migration_target()
        raise ValueError("Offline production migrations cannot verify the live target")
    context.configure(url=migration_url, target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        if context.get_context().dialect.name == "postgresql":
            context.execute("SET search_path TO public")
        context.run_migrations()


def run_migrations_online():
    expected_target = _migration_target()
    injected = config.attributes.get("connection")
    if injected is not None:
        guard_migration_connection(injected, expected_target)
        if injected.dialect.name == "sqlite":
            caller_has_transaction = injected.in_transaction()
            injected.exec_driver_sql("PRAGMA recursive_triggers=ON")
            if not caller_has_transaction:
                injected.commit()
        context.configure(connection=injected, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = make_engine(_url(), environment=_environment())
    with engine.begin() as connection:
        guard_migration_connection(connection, expected_target)
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("PRAGMA recursive_triggers=ON")
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
