"""Read-only preflight for the locked verification deployment package."""
from __future__ import annotations

import json
import os
import re

from sqlalchemy import select

from backend.app.config.settings import Settings
from backend.app.db import models as m
from backend.app.db.readiness import verify_schema_revision
from backend.app.db.session import make_engine
from scripts.grant_runtime import verify_runtime_role


def checked_image(value: str) -> str:
    if not re.fullmatch(r"(?:[a-zA-Z0-9][a-zA-Z0-9._:/-]*@)?sha256:[0-9a-f]{64}", value):
        raise ValueError("An immutable image digest is required; mutable tags are not release pins")
    return value


def preflight(settings: Settings, image: str) -> dict:
    if not settings.verification_only or settings.environment != "production":
        raise ValueError("Preflight requires the production verification-only package")
    checked_image(image)
    engine = make_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            verify_runtime_role(connection)
            heads = verify_schema_revision(connection)
            sites = connection.execute(select(m.Site.autonomy_level, m.Site.production_enabled, m.Site.config_json)).all()
            for level, enabled, config in sites:
                if level != 1 or enabled or config.get("earned_categories"):
                    raise ValueError("Canonical site authority is incompatible with verification-only deployment")
    finally:
        engine.dispose()
    return {"status": "verified", "schema_heads": heads, "image": image,
            "site_count": len(sites), "autonomy_level": 1, "production_enabled": False,
            "production_write_budget": 0, "paid_api_budget_usd": 0,
            "provider_mode": "fixture", "worker_started": False,
            "limits": ["not a durable-host or backup attestation", "provider access remains disconnected"]}


def main() -> int:
    try:
        report = preflight(Settings(), os.environ.get("SEO_RELEASE_IMAGE", ""))
    except Exception as exc:
        # SQL/provider exception messages can include connection details.
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
