"""Schema readiness is different from a responding database or live process."""
from functools import lru_cache
from pathlib import Path
import threading
from time import monotonic
from weakref import WeakKeyDictionary

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from backend.app.db.transport import normalize_database_environment


_PRIVILEGE_CACHE_LOCK = threading.Lock()
_PRIVILEGE_CACHE: WeakKeyDictionary = WeakKeyDictionary()


@lru_cache(maxsize=1)
def expected_heads() -> frozenset[str]:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
    return frozenset(ScriptDirectory.from_config(config).get_heads())


def verify_schema_revision(connection) -> tuple[str, ...]:
    actual = frozenset(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
    expected = expected_heads()
    if not expected or actual != expected:
        raise ValueError("Database schema revision differs from this release")
    return tuple(sorted(actual))


def clear_privilege_readiness_cache() -> None:
    """Clear process-local privilege results after a controlled reconfiguration."""
    with _PRIVILEGE_CACHE_LOCK:
        _PRIVILEGE_CACHE.clear()


def verify_database_readiness(
    connection, *, environment: str, profile: str = "api", privilege_cache_seconds: int = 0,
) -> tuple[str, ...]:
    """Verify release schema and, in production, the restricted runtime role.

    The schema query itself proves database reachability, so callers should not
    add a redundant ``SELECT 1``.  The import is lazy to keep migration tooling
    independent of the privilege-provisioning script until runtime readiness.
    """
    heads = verify_schema_revision(connection)
    if normalize_database_environment(environment) == "production":
        from scripts.grant_runtime import verify_runtime_role
        if not 0 <= privilege_cache_seconds <= 300:
            raise ValueError("Privilege readiness cache must be between 0 and 300 seconds")
        engine = getattr(connection, "engine", None)
        if not privilege_cache_seconds or engine is None:
            verify_runtime_role(connection, profile=profile)
            return heads
        now = monotonic()
        with _PRIVILEGE_CACHE_LOCK:
            cached = _PRIVILEGE_CACHE.get(engine, {}).get(profile)
            if cached and cached[0] > now:
                if cached[1]:
                    return heads
                raise ValueError("Cached runtime privilege policy failure")
            try:
                verify_runtime_role(connection, profile=profile)
            except Exception:
                by_profile = _PRIVILEGE_CACHE.setdefault(engine, {})
                by_profile[profile] = (now + min(5, privilege_cache_seconds), False)
                raise
            by_profile = _PRIVILEGE_CACHE.setdefault(engine, {})
            by_profile[profile] = (now + privilege_cache_seconds, True)
    return heads
