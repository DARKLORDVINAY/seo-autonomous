from __future__ import annotations

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.config.settings import get_settings


def make_engine(url: str, **kwargs) -> Engine:
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    options: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if url in ("sqlite://", "sqlite:///:memory:"):
            options["poolclass"] = StaticPool
    options.update(kwargs)
    engine = create_engine(url, **options)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _sqlite_integrity(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            # REPLACE deletes conflicting rows implicitly; those deletions must
            # fire the append-only audit triggers just like explicit DELETE.
            cursor.execute("PRAGMA recursive_triggers=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()
    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False, autoflush=True)


@lru_cache(maxsize=1)
def default_engine() -> Engine:
    return make_engine(get_settings().database_url)


@lru_cache(maxsize=1)
def default_session_factory() -> sessionmaker[Session]:
    return make_session_factory(default_engine())


def get_session() -> Generator[Session, None, None]:
    """No automatic commits: services explicitly own transaction boundaries."""
    with default_session_factory()() as session:
        try:
            yield session
        except BaseException:
            session.rollback()
            raise
