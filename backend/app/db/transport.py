"""Fail-closed database transport policy evaluated before opening a connection."""
from __future__ import annotations

import os

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


LOCAL_POSTGRES_HOSTS = frozenset({"db", "localhost", "127.0.0.1", "::1"})
PRODUCTION_POSTGRES_DRIVERS = frozenset({"postgresql", "postgresql+psycopg"})
CONNECTION_OVERRIDE_KEYS = frozenset({
    "autocommit", "conninfo", "context", "cursor_factory", "database", "dbname", "gssencmode", "host",
    "hostaddr", "options", "password", "plugin", "port", "prepare_threshold", "requiressl", "row_factory",
    "service", "servicefile", "sslmode", "user",
})
PRODUCTION_CONNECTION_QUERY_KEYS = frozenset({"gssencmode", "sslmode"})
DATABASE_ENVIRONMENTS = frozenset({"development", "test", "production"})
FORBIDDEN_PRODUCTION_LIBPQ_ENVIRONMENT = frozenset({
    "PGCHANNELBINDING", "PGDATABASE", "PGGSSENCMODE", "PGHOST", "PGHOSTADDR", "PGOPTIONS", "PGPASSFILE",
    "PGPASSWORD", "PGPORT", "PGREQUIREAUTH", "PGREQUIRESSL", "PGSERVICE", "PGSERVICEFILE", "PGSSLCERT",
    "PGSSLCERTMODE", "PGSSLCRL", "PGSSLCRLDIR", "PGSSLKEY", "PGSSLMAXPROTOCOLVERSION",
    "PGSSLMINPROTOCOLVERSION", "PGSSLMODE",
    "PGSSLNEGOTIATION", "PGSSLPASSWORD", "PGSSLROOTCERT", "PGSSLSNI", "PGUSER",
})


def normalize_database_environment(environment: str | None = None) -> str:
    raw = environment if environment is not None else os.environ.get("ENVIRONMENT", "development")
    normalized = raw.strip().lower()
    if normalized not in DATABASE_ENVIRONMENTS:
        raise ValueError("Database environment must be development, test, or production")
    return normalized


def validate_database_transport(database_url: str, *, environment: str | None = None) -> str:
    """Return *database_url* only when its production transport is acceptable.

    ``create_engine`` is deliberately downstream of this function.  That keeps
    malformed or downgrade-prone remote production PostgreSQL URLs from ever
    reaching a driver, including owner migration and direct-launch paths.
    """
    try:
        parsed = make_url(database_url)
    except (ArgumentError, TypeError) as exc:
        raise ValueError("Database URL is malformed") from exc
    if normalize_database_environment(environment) != "production":
        return database_url
    if FORBIDDEN_PRODUCTION_LIBPQ_ENVIRONMENT.intersection(os.environ):
        raise ValueError("Production PostgreSQL rejects ambient connection overrides")
    if parsed.drivername not in PRODUCTION_POSTGRES_DRIVERS:
        raise ValueError("Production requires PostgreSQL through the supported psycopg driver")
    if not parsed.username or not parsed.database:
        raise ValueError("Production PostgreSQL requires explicit database username and database name")
    # libpq accepts connection-target fields in the query string and gives them
    # precedence over the URI authority.  Never let an apparently local host
    # redirect to an unverified remote target after this check.
    query_overrides = (CONNECTION_OVERRIDE_KEYS - {"gssencmode", "sslmode"}).intersection(parsed.query)
    if query_overrides:
        raise ValueError("Production database URL query may not override connection identity")
    if set(parsed.query) - PRODUCTION_CONNECTION_QUERY_KEYS:
        raise ValueError("Production database URL contains unsupported connection parameters")
    host = parsed.host
    if not host:
        raise ValueError("Production PostgreSQL requires an explicit database host")
    if host.lower() not in LOCAL_POSTGRES_HOSTS:
        sslmode = parsed.query.get("sslmode")
        if sslmode != "verify-full":
            raise ValueError("Remote production PostgreSQL requires sslmode=verify-full")
        # libpq otherwise prefers GSSAPI encryption when credentials are
        # available, regardless of sslmode.  Disable GSS transport so this
        # boundary's explicit TLS + hostname-verification contract is true.
        gssencmode = parsed.query.get("gssencmode")
        if gssencmode not in (None, "disable"):
            raise ValueError("Remote production PostgreSQL TLS requires gssencmode=disable")
        if gssencmode is None:
            parsed = parsed.update_query_dict({"gssencmode": "disable"})
            return parsed.render_as_string(hide_password=False)
    return database_url


def validate_database_engine_options(
    database_url: str, *, environment: str | None = None, options: dict | None = None,
) -> str:
    """Prevent engine kwargs from overriding a production URL after validation."""
    effective_environment = normalize_database_environment(environment)
    database_url = validate_database_transport(database_url, environment=effective_environment)
    if effective_environment != "production":
        return database_url
    # Caller-provided engine kwargs can replace the pool, DBAPI module, creator,
    # dialect plugins, isolation semantics, or raw connect arguments after URL
    # validation. Production applies only this module's reviewed internal
    # defaults; any requested override therefore fails closed.
    if options:
        raise ValueError("Production database engine options may not override the validated connection")
    return database_url
