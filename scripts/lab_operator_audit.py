"""Record manual Test Lab release operations; this module cannot execute them.

Only the build operator uses this local CLI. It grants no executor authority,
exposes no MCP tool, and never changes the site's Level 1 / write-disabled state.
GitHub and deployment receipts are appended after their actual tools return.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.db import models as m
from backend.app.db.session import make_engine, make_session_factory
from backend.app.services.test_lab import register_lab
from scripts.bootstrap import migrate

ROOT = Path(__file__).resolve().parents[1]
REPOSITORIES = frozenset({"DARKLORDVINAY/seo-autonomous", "DARKLORDVINAY/seo-test-lab"})
OPERATIONS = frozenset({"create_file", "create_branch", "create_tree", "create_commit", "update_ref",
                        "create_pull_request", "update_pull_request", "merge_pull_request"})


def checked_site(session, site_id):
    site = session.get(m.Site, site_id)
    if (not site or site.base_url != "https://seo-test-lab.pages.dev"
            or site.config_json.get("target_kind") != "controlled_test_lab"
            or site.config_json.get("source_mode") != "live"
            or site.autonomy_level != 1 or site.production_enabled
            or site.config_json.get("earned_categories")
            or site.config_json.get("max_daily_actions") != 0):
        raise ValueError("Manual audit requires the registered, write-disabled public Test Lab")
    return site


def begin_change(session, request):
    site = checked_site(session, request["site_id"])
    operation, repository = request["operation"], request["repository"]
    if operation not in OPERATIONS or repository not in REPOSITORIES:
        raise ValueError("Only the two authorised Test Lab repositories and release operations are in scope")
    experiment = session.get(m.Experiment, request["experiment_id"])
    if not experiment or experiment.site_id != site.id:
        raise ValueError("A same-site release experiment is required")
    if not request.get("reason") or not request.get("rollback_procedure"):
        raise ValueError("Every manual change requires a reason and rollback procedure")
    if "before" not in request or "after" not in request:
        raise ValueError("Both intended before and after state must be recorded")
    key = request["idempotency_key"]
    if session.scalar(select(m.Action).where(m.Action.site_id == site.id, m.Action.idempotency_key == key)):
        raise ValueError("An existing operation must be reconciled; it cannot be redispatched by this recorder")
    may_deploy = operation in {"create_branch", "update_ref", "merge_pull_request"}
    action = m.Action(site_id=site.id, kind="manual_github_" + operation,
        risk="CRITICAL" if may_deploy else "LOW", actor="mission-governor-build-operator",
        reason=request["reason"], experiment_id=experiment.id, idempotency_key=key,
        payload_json={**request, "autonomous": False, "external_repository_write": True,
            "may_trigger_public_deployment": may_deploy, "autonomy_level": 1,
            "authorization": "User requested public Test Lab deployment and rollback through GitHub",
            "executor_production_enabled": False})
    session.add(action)
    session.flush()
    session.add(m.ActionEvent(site_id=site.id, action_id=action.id, event_type="manual_intent_recorded",
        details_json={"operation": operation, "repository": repository, "autonomous": False}))
    session.commit()
    return {"action_id": action.id, "risk": action.risk}


def record_outcome(session, request):
    site = checked_site(session, request["site_id"])
    action = session.get(m.Action, request["action_id"])
    if not action or action.site_id != site.id or not action.kind.startswith("manual_github_"):
        raise ValueError("Outcome must reference this site's manual release action")
    if request["status"] not in {"succeeded", "failed", "unknown", "public_verified", "public_verification_failed"}:
        raise ValueError("Unsupported observed outcome")
    session.add(m.ActionEvent(site_id=site.id, action_id=action.id, event_type="manual_" + request["status"],
        details_json={"autonomous": False, "production_write": action.payload_json["may_trigger_public_deployment"],
                      "receipt": request.get("receipt", {}), "verification_scope": request.get("verification_scope", "GitHub operation only; public serving is unverified")}))
    session.commit()
    return {"action_id": action.id, "recorded": request["status"]}


def main():
    request = json.load(sys.stdin)
    database_url = os.environ.get("LAB_DATABASE_URL") or f"sqlite:///{ROOT / 'artifacts/test-lab-public.sqlite3'}"
    migrate(database_url)
    engine = make_engine(database_url)
    try:
        with make_session_factory(engine)() as session:
            if request["command"] == "initialise":
                release = Path(request["release_dir"])
                manifest_hash = hashlib.sha256((release / "inventory.json").read_bytes()).hexdigest()
                site = register_lab(session, mode="public", base_url="https://seo-test-lab.pages.dev",
                    expected_manifest_sha256=manifest_hash, build_dir=release)
                experiment = m.Experiment(site_id=site.id, name="Public Test Lab release and restore integrity",
                    hypothesis="A reviewed Git release serves the attested public pages and a bounded change can be restored",
                    mechanism="User-authorised GitHub PR deployment, independent public readback and Git rollback",
                    primary_outcome="public_release_integrity", status="running",
                    secondary_outcomes_json=["public_rollback_integrity", "structural_shadow_benchmark"],
                    evaluation_windows_json=[0], analysis_json={"is_fixture": False, "test_only": True,
                        "qualifies_for_autonomy": False, "commercial_value": None})
                session.add(experiment)
                session.commit()
                result = {"site_id": site.id, "experiment_id": experiment.id, "manifest_sha256": manifest_hash}
            elif request["command"] == "begin":
                result = begin_change(session, request)
            elif request["command"] == "outcome":
                result = record_outcome(session, request)
            else:
                raise ValueError("Unsupported audit command")
            print(json.dumps(result))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
