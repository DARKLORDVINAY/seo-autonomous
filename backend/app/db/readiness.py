"""Schema readiness is different from a responding database or live process."""
from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text


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
