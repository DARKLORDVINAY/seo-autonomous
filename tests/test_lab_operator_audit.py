"""Manual deployment audit cannot grant authority or silently re-dispatch."""
import pytest
from sqlalchemy import select

from backend.app.db import models as m
from backend.app.db.session import make_engine, make_session_factory
from backend.app.services.test_lab import register_lab
from scripts.lab_operator_audit import begin_change, record_outcome


@pytest.fixture
def state():
    engine = make_engine("sqlite://")
    m.Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        site = register_lab(session, mode="public", base_url="https://seo-test-lab.pages.dev", expected_manifest_sha256="a" * 64)
        experiment = m.Experiment(site_id=site.id, hypothesis="Public release integrity")
        session.add(experiment)
        session.commit()
        yield session, site, experiment
    engine.dispose()


def request_for(site, experiment):
    return {"site_id": site.id, "experiment_id": experiment.id, "operation": "merge_pull_request",
        "repository": "DARKLORDVINAY/seo-test-lab", "idempotency_key": "manual-test-1",
        "reason": "User-authorised sandbox deploy", "before": {"commit": "a" * 40},
        "after": {"commit": "b" * 40}, "rollback_procedure": "Reviewed Git revert and public byte verification"}


def test_manual_deployment_intent_is_audited_without_enabling_execution(state):
    session, site, experiment = state
    request = request_for(site, experiment)
    result = begin_change(session, request)
    assert result["risk"] == "CRITICAL"
    assert site.autonomy_level == 1 and not site.production_enabled
    assert not site.config_json["earned_categories"]
    action = session.get(m.Action, result["action_id"])
    assert action.payload_json["before"] == request["before"]
    assert action.payload_json["after"] == request["after"]
    assert action.payload_json["autonomous"] is False
    with pytest.raises(ValueError, match="reconciled"):
        begin_change(session, request)
    record_outcome(session, {"site_id": site.id, "action_id": action.id, "status": "succeeded", "receipt": {"merged": True}})
    events = list(session.scalars(select(m.ActionEvent).where(m.ActionEvent.action_id == action.id)))
    assert {event.event_type for event in events} == {"manual_intent_recorded", "manual_succeeded"}
    assert next(event for event in events if event.event_type == "manual_succeeded").details_json["production_write"] is True


@pytest.mark.parametrize("change", [{"repository": "someone/commercial-site"}, {"operation": "execute_shell"}])
def test_manual_recorder_rejects_unrelated_capabilities(state, change):
    session, site, experiment = state
    with pytest.raises(ValueError, match="scope"):
        begin_change(session, {**request_for(site, experiment), **change})


def test_manual_recorder_refuses_graduated_site(state):
    session, site, experiment = state
    site.autonomy_level = 2
    with pytest.raises(ValueError, match="write-disabled"):
        begin_change(session, request_for(site, experiment))
