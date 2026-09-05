"""Prove a local static-release Git rollback without enabling website writes.

This is an artifact-only drill. It cannot deploy, contact a remote, or count as a
successful public Cloudflare rollback. All records are explicitly fixture-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.contracts import utcnow
from backend.app.db import models as m
from backend.app.db.session import make_engine, make_session_factory


def _git(root: Path, *arguments: str) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="Spiral Max Test Automation", GIT_AUTHOR_EMAIL="test-lab@example.invalid",
        GIT_COMMITTER_NAME="Spiral Max Test Automation", GIT_COMMITTER_EMAIL="test-lab@example.invalid")
    result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false",
        *arguments], cwd=root, env=environment, check=True, capture_output=True, text=True, timeout=15)
    return result.stdout.strip()


def run_drill(session, site_id: str, release_dir: Path, work_dir: Path,
              page_path: str = "/guides/descriptive-titles/") -> dict:
    site = session.get(m.Site, site_id)
    if not site or site.config_json.get("source_mode") != "fixture" or site.production_enabled or site.autonomy_level != 1:
        raise ValueError("Artifact drill requires an existing Level 1, write-disabled fixture site")
    if not re.fullmatch(r"/(?:[a-z0-9]+(?:-[a-z0-9]+)*/)+", page_path):
        raise ValueError("Drill page must be a bounded directory route")
    release_dir, work_dir = Path(release_dir).resolve(), Path(work_dir).absolute()
    if work_dir.exists() or work_dir.is_symlink():
        raise ValueError("Choose a fresh drill directory; existing work is never replaced")
    # A symlink in a parent must not disguise a destination inside the source.
    work_dir = work_dir.resolve()
    if release_dir == work_dir or release_dir in work_dir.parents:
        raise ValueError("The drill directory must be outside the release being copied")
    files = list(release_dir.rglob("*"))
    if any(p.is_symlink() or any(part.startswith(".") for part in p.relative_to(release_dir).parts) for p in files):
        raise ValueError("Only ordinary public release assets may enter the drill")
    public_files = [p for p in files if p.is_file()]
    if len(public_files) > 200 or sum(p.stat().st_size for p in public_files) > 10_000_000:
        raise ValueError("Static release exceeds the bounded drill budget")
    if any(p.name in {"ground_truth.json", "pages.json"} for p in public_files):
        raise ValueError("Source/benchmark manifests are not public release assets")
    manifest = json.loads((release_dir / "inventory.json").read_text())
    entry = next((p for p in manifest["pages"] if p["path"] == page_path), None)
    if entry is None:
        raise ValueError("Requested drill page is not in the release inventory")
    relative_page = page_path.lstrip("/") + "index.html"
    before = (release_dir / relative_page).read_bytes()
    if hashlib.sha256(before).hexdigest() != entry["content_sha256"]:
        raise ValueError("The copied release must match its attested inventory")
    text = before.decode("utf-8")
    matches = list(re.finditer(r"<title>([^<]*)</title>", text))
    if len(matches) != 1:
        raise ValueError("Drill needs one plain-text title")
    match = matches[0]
    changed = (text[:match.end(1)] + " — rollback drill" + text[match.end(1):]).encode("utf-8")
    experiment = m.Experiment(site_id=site.id, name="Static artifact rollback drill", hypothesis="Git revert restores every tracked public asset byte",
        mechanism="Isolated release copy, one title edit plus inventory hash, whole-tree comparison",
        primary_outcome="artifact_tree_restored", secondary_outcomes_json=[], status="running",
        analysis_json={"is_fixture": True, "scope": "isolated_local_git_copy", "qualifies_for_autonomy": False})
    session.add(experiment)
    session.flush()
    reason = "User-authorised sandbox rollback drill; no website or remote Git writes"
    action = m.Action(site_id=site.id, kind="test_lab_artifact_change", risk="LOW", actor="test-lab-operator",
        reason=reason, experiment_id=experiment.id, idempotency_key="artifact-drill:" + str(uuid4()),
        payload_json={"is_fixture": True, "production_write": False, "page_path": page_path,
            "before": text, "after": changed.decode(), "before_sha256": hashlib.sha256(before).hexdigest(),
            "after_sha256": hashlib.sha256(changed).hexdigest(), "rollback_procedure": "git revert the isolated change commit; compare complete Git trees"})
    session.add(action)
    session.flush()
    session.add(m.ActionEvent(site_id=site.id, action_id=action.id, event_type="requested", details_json={"scope": "artifact_only", "production_write": False}))
    session.commit()  # Record intent before touching the isolated release copy.
    reverse_id: str | None = None
    reverse_requested = False
    rollback_stage: str | None = None
    try:
        shutil.copytree(release_dir, work_dir)
        _git(work_dir, "init", "--initial-branch=lab-drill")
        _git(work_dir, "add", "--all")
        _git(work_dir, "commit", "-m", "Record original static test-lab release")
        baseline_commit = _git(work_dir, "rev-parse", "HEAD")
        baseline_tree = _git(work_dir, "rev-parse", "HEAD^{tree}")
        (work_dir / relative_page).write_bytes(changed)
        entry["content_sha256"] = hashlib.sha256(changed).hexdigest()
        (work_dir / "inventory.json").write_text(json.dumps(manifest, indent=2) + "\n")
        _git(work_dir, "add", "--all")
        _git(work_dir, "commit", "-m", "Demonstrate one reversible sandbox title edit")
        changed_commit = _git(work_dir, "rev-parse", "HEAD")
        changed_tree = _git(work_dir, "rev-parse", "HEAD^{tree}")
        if baseline_tree == changed_tree:
            raise RuntimeError("Drill did not produce a real changed tree")
        session.add(m.ActionEvent(site_id=site.id, action_id=action.id, event_type="succeeded",
            details_json={"scope": "artifact_only", "production_write": False, "commit": changed_commit, "tree": changed_tree}))
        reverse = m.Action(site_id=site.id, kind="test_lab_artifact_rollback", risk="LOW", actor="test-lab-operator",
            reason=reason, experiment_id=experiment.id, idempotency_key="artifact-rollback:" + str(uuid4()),
            payload_json={"is_fixture": True, "production_write": False, "original_action_id": action.id,
                "baseline_commit": baseline_commit, "change_commit": changed_commit, "expected_tree": baseline_tree})
        session.add(reverse)
        session.flush()
        reverse_id = reverse.id
        session.add(m.ActionEvent(site_id=site.id, action_id=reverse.id, event_type="requested", details_json={"scope": "artifact_only", "production_write": False}))
        session.commit()
        reverse_requested = True
        rollback_stage = "git_revert"
        _git(work_dir, "revert", "--no-edit", changed_commit)
        rollback_stage = "restoration_verification"
        restored_commit = _git(work_dir, "rev-parse", "HEAD")
        restored_tree = _git(work_dir, "rev-parse", "HEAD^{tree}")
        if restored_tree != baseline_tree or (work_dir / relative_page).read_bytes() != before or _git(work_dir, "status", "--porcelain"):
            raise RuntimeError("Rollback did not restore the complete original release")
        report = {"status": "passed", "scope": "isolated_local_git_copy", "is_fixture": True,
            "site_id": site.id, "experiment_id": experiment.id, "action_id": action.id, "rollback_action_id": reverse.id,
            "page_path": page_path, "baseline_commit": baseline_commit, "changed_commit": changed_commit,
            "restored_commit": restored_commit, "baseline_tree": baseline_tree, "changed_tree": changed_tree,
            "restored_tree": restored_tree, "all_tracked_bytes_restored": True, "checked_at": utcnow().isoformat(),
            "production_mutations": 0, "public_deployment_rollback": "not_verified", "qualifies_for_autonomy": False}
        session.add(m.ActionEvent(site_id=site.id, action_id=reverse.id, event_type="succeeded", details_json=report))
        session.add(m.RollbackEvent(site_id=site.id, action_id=action.id, rollback_action_id=reverse.id,
            reason=reason, actor="test-lab-operator", status="artifact_restored", details_json=report))
        experiment.status, experiment.verdict = "completed", "artifact_integrity_passed"
        experiment.analysis_json = {**experiment.analysis_json, "result": report}
        session.commit()
        return report
    except Exception as error:
        session.rollback()
        failure = {"scope": "artifact_only", "error_type": type(error).__name__,
            "production_write": False, "stage": rollback_stage or "change_preparation"}
        if reverse_requested and reverse_id is not None:
            # Once the inverse has a durable request, its own lifecycle must end
            # on that action. The successful original change remains immutable
            # history rather than being mislabeled as the rollback failure.
            reverse = session.get(m.Action, reverse_id)
            if reverse is None:  # A committed request disappearing is storage corruption.
                raise RuntimeError("Durable rollback action could not be recovered") from error
            session.add(m.ActionEvent(site_id=site.id, action_id=reverse.id, event_type="failed",
                details_json=failure))
            session.add(m.RollbackEvent(site_id=site.id, action_id=action.id,
                rollback_action_id=reverse.id, reason=reverse.reason, actor=reverse.actor,
                status="failed", details_json=failure))
        else:
            session.add(m.ActionEvent(site_id=site.id, action_id=action.id, event_type="drill_failed",
                details_json=failure))
        experiment.status, experiment.verdict = "failed", "inconclusive"
        session.commit()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True, help="Existing local fixture canonical database")
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    engine = make_engine(args.database_url)
    try:
        with make_session_factory(engine)() as session:
            result = run_drill(session, args.site_id, args.release_dir, args.work_dir)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"status": result["status"], "report": str(args.report), "public_rollback_verified": False}))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
