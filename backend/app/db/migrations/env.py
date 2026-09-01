from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from backend.app.db.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def _url():
    url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def run_migrations_offline():
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    injected = config.attributes.get("connection")
    if injected is not None:
        if injected.dialect.name == "sqlite":
            caller_has_transaction = injected.in_transaction()
            injected.exec_driver_sql("PRAGMA recursive_triggers=ON")
            if not caller_has_transaction:
                injected.commit()
        context.configure(connection=injected, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _url()
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("PRAGMA recursive_triggers=ON")
            connection.commit()
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
