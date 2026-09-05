"""Register one shadow site or offline demo; run owner migrations only when selected."""
from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.engine import Connection

# Both direct script and module invocation import the same application package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.config.settings import Settings
from backend.app.db import models as m
from backend.app.db.readiness import verify_database_readiness
from backend.app.db.session import make_engine, make_session_factory
from backend.app.services import control
from scripts.deployment_preflight import (
    MigrationTarget,
    required_migration_target,
)

ROOT = Path(__file__).resolve().parents[1]


def migrate(
    database_url: str, *, environment: str | None = None,
    expected_target: MigrationTarget | None = None,
    post_migration: Callable[[Connection], None] | None = None,
) -> None:
    """Inject the actual connection so an ambient DATABASE_URL cannot retarget it."""
    expected_target = required_migration_target(
        environment=environment,
        explicit=expected_target,
    )
    engine = make_engine(database_url, environment=environment)
    try:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "backend/app/db/migrations"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            config.attributes["database_environment"] = environment
            config.attributes["migration_target"] = expected_target
            command.upgrade(config, "head")
            if post_migration is not None:
                post_migration(connection)
    finally:
        engine.dispose()


def bootstrap(
    settings: Settings, *, demo: bool = False, domain: str | None = None,
    name: str | None = None, migrate_only: bool = False,
) -> dict:
    if sum((bool(demo), bool(domain), bool(migrate_only))) != 1:
        raise ValueError("Select exactly one of --demo, --domain, or --migrate-only")
    if domain and (not name or not name.strip()):
        raise ValueError("A real domain requires an explicit --name")
    if demo and name:
        raise ValueError("The fixture has a fixed, clearly labelled demo name")
    base_url = "https://example.test" if demo else domain
    if domain:
        from backend.app.integrations.crawler.network import validate_url
        base_url = domain if "://" in domain else "https://" + domain
        parsed = urlsplit(base_url)
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Register a site origin, without a path, query, or fragment")
        base_url = validate_url(base_url, fixture=False).rstrip("/")
    if migrate_only or settings.environment != "production":
        migrate(settings.database_url, environment=settings.environment)
    if migrate_only:
        return {"status": "migrated"}
    engine = make_engine(settings.database_url, environment=settings.environment)
    try:
        with make_session_factory(engine)() as session:
            if settings.environment == "production":
                # Production registration uses the restricted API role after a
                # separate owner migration. Check this same connection before
                # any site access; never request owner pins or alter the schema.
                verify_database_readiness(session.connection(), environment="production", profile="api")
            site = session.scalar(select(m.Site).where(m.Site.base_url == base_url))
            created = site is None
            if site is None:
                site = control.create_site(session, name="Offline demo — example.test" if demo else name.strip(),
                                           base_url=base_url, fixture=demo)
            elif (site.config_json.get("source_mode") == "fixture") != demo:
                raise ValueError("An existing site's source mode cannot be changed by bootstrap")
            # Re-running registration never resets authority, strategy, names or observations.
            result = {"status": "created" if created else "existing", "site_id": site.id,
                      "base_url": site.base_url, "source_mode": site.config_json.get("source_mode"),
                      "production_write": False}
            if demo:
                # A demo never inherits live model/connector settings from an existing .env.
                fixture_settings = settings.model_copy(update={
                    "provider_mode": "fixture", "agent_mode": "fixture", "production_enabled": False,
                    "shadow_mode": True, "openai_api_key": None, "openai_model": None,
                })
                cycle = control.run_cycle(session, site.id, fixture_settings, idempotency_key="bootstrap:offline-demo:v1")
                result.update(job_id=cycle.get("job_id"), cycle_status=cycle.get("status"))
            return result
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--demo", action="store_true")
    selection.add_argument("--domain", help="A real site origin; registration does not crawl or publish")
    selection.add_argument("--migrate-only", action="store_true")
    parser.add_argument("--name", help="Required explicit name for --domain")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args(argv)
    if args.domain and (not args.name or not args.name.strip()):
        parser.error("--domain requires --name")
    try:
        if args.migrate_only:
            # An owner migration does not need API/approval capabilities.
            from dotenv import dotenv_values
            values = dotenv_values(args.env_file)
            url = os.environ.get("DATABASE_URL") or values.get("DATABASE_URL") or "sqlite:///./seo-autonomous.db"
            environment = os.environ.get("ENVIRONMENT") or values.get("ENVIRONMENT")
            migrate(url, environment=environment)
            result = {"status": "migrated"}
        else:
            result = bootstrap(Settings(_env_file=args.env_file), demo=args.demo, domain=args.domain, name=args.name)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__,
                          "detail": "Bootstrap stopped; configuration and credentials were not displayed."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
