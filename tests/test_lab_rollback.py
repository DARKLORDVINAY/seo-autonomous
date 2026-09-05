"""An isolated Git restore drill must not change the registered website authority."""
import json

import pytest
from sqlalchemy import select

from backend.app.db import models as m
from backend.app.db.session import make_engine, make_session_factory
from backend.app.services.control import create_site
from scripts import lab_rollback_drill as rollback_module
from scripts.lab_rollback_drill import run_drill
from test_lab.build import build_site


def test_artifact_drill_restores_all_bytes_with_immutable_canonical_events(tmp_path):
    release = tmp_path / "release"
    build_site("https://example.test", release, fixture=True)
    original = {str(p.relative_to(release)): p.read_bytes() for p in release.rglob("*") if p.is_file()}
    engine = make_engine("sqlite://")
    m.Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        site = create_site(session, name="Explicit local drill fixture", base_url="https://example.test", fixture=True)
        result = run_drill(session, site.id, release, tmp_path / "drill")
        assert result["baseline_tree"] == result["restored_tree"] != result["changed_tree"]
        assert result["public_deployment_rollback"] == "not_verified" and result["qualifies_for_autonomy"] is False
        assert not site.production_enabled and site.autonomy_level == 1
        assert len(list(session.scalars(select(m.RollbackEvent)))) == 1
        actions = list(session.scalars(select(m.Action).where(m.Action.experiment_id == result["experiment_id"])))
        assert len(actions) == 2 and all(a.payload_json["production_write"] is False for a in actions)
        assert {str(p.relative_to(release)): p.read_bytes() for p in release.rglob("*") if p.is_file()} == original
        assert json.loads((tmp_path / "drill/inventory.json").read_text()) == json.loads((release / "inventory.json").read_text())
        with pytest.raises(ValueError, match="fresh drill directory"):
            run_drill(session, site.id, release, tmp_path / "drill")
    engine.dispose()


def test_artifact_drill_cannot_operate_on_live_site(tmp_path):
    engine = make_engine("sqlite://")
    m.Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        site = create_site(session, name="Read-only live target", base_url="https://example.com")
        with pytest.raises(ValueError, match="fixture site"):
            run_drill(session, site.id, tmp_path / "unused", tmp_path / "unused-copy")
        assert not (tmp_path / "unused-copy").exists()
        assert list(session.scalars(select(m.RollbackEvent))) == []
    engine.dispose()


@pytest.mark.parametrize(
    ("failure_mode", "expected_stage"),
    [("revert", "git_revert"), ("verification", "restoration_verification")],
)
def test_artifact_drill_records_terminal_failure_on_requested_reverse(
    tmp_path, monkeypatch, failure_mode, expected_stage,
):
    release = tmp_path / "release"
    build_site("https://example.test", release, fixture=True)
    actual_git = rollback_module._git

    def fault_injected_git(root, *arguments):
        if failure_mode == "revert" and arguments[:1] == ("revert",):
            raise RuntimeError("injected git revert failure")
        if failure_mode == "verification" and arguments == ("status", "--porcelain"):
            return "injected-unrestored-file"
        return actual_git(root, *arguments)

    monkeypatch.setattr(rollback_module, "_git", fault_injected_git)
    engine = make_engine("sqlite://")
    m.Base.metadata.create_all(engine)
    try:
        with make_session_factory(engine)() as session:
            site = create_site(session, name="Rollback failure fixture", base_url="https://example.test", fixture=True)
            with pytest.raises(RuntimeError):
                run_drill(session, site.id, release, tmp_path / "drill")

            actions = list(session.scalars(select(m.Action).where(
                m.Action.kind.in_(["test_lab_artifact_change", "test_lab_artifact_rollback"]),
            ).order_by(m.Action.created_at, m.Action.id)))
            assert [action.kind for action in actions] == [
                "test_lab_artifact_change", "test_lab_artifact_rollback",
            ]
            original, reverse = actions
            original_events = list(session.scalars(select(m.ActionEvent).where(
                m.ActionEvent.action_id == original.id,
            ).order_by(m.ActionEvent.created_at, m.ActionEvent.id)))
            reverse_events = list(session.scalars(select(m.ActionEvent).where(
                m.ActionEvent.action_id == reverse.id,
            ).order_by(m.ActionEvent.created_at, m.ActionEvent.id)))

            # The original change really completed. A later inverse failure must
            # not rewrite or append a contradictory failure to that history.
            assert [event.event_type for event in original_events] == ["requested", "succeeded"]
            assert [event.event_type for event in reverse_events] == ["requested", "failed"]
            assert reverse_events[-1].details_json == {
                "scope": "artifact_only", "error_type": "RuntimeError",
                "production_write": False, "stage": expected_stage,
            }

            rollbacks = list(session.scalars(select(m.RollbackEvent)))
            assert len(rollbacks) == 1
            assert rollbacks[0].action_id == original.id
            assert rollbacks[0].rollback_action_id == reverse.id
            assert rollbacks[0].status == "failed"
            assert rollbacks[0].details_json["stage"] == expected_stage
            experiment = session.get(m.Experiment, original.experiment_id)
            assert (experiment.status, experiment.verdict) == ("failed", "inconclusive")
    finally:
        engine.dispose()
