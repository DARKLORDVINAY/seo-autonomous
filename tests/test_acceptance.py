"""Assembled control plane: local synthetic data and CMS only."""
from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from backend.app.config.settings import Settings, get_settings
from backend.app.contracts import stable_hash
from backend.app.db import models as m
from backend.app.db.session import get_session, make_engine, make_session_factory
from backend.app.integrations.fixtures import FixtureCMS
from backend.app.main import app
from backend.app.services import control, measurement
from scripts.bootstrap import migrate

TOKENS = {"operator": "isolated-operator", "reviewer": "isolated-reviewer", "admin": "isolated-admin"}
DEFINITION = {"verified": True, "tracking_verified": True, "qualification_verified": True,
    "deduplication_verified": True, "qualified_events": ["qualified_enquiry"],
    "qualification_definition": "Owner confirms an eligible service enquiry",
    "deduplication_method": "One CRM-qualified event per unique enquiry",
    "value_method": "fixed_per_qualified_conversion", "currency": "GBP", "value_per_conversion": 75.0}
REVIEW = {"verdict": "PASS", "confidence": .95, "reasons": ["Reviewed exact fixture revision"],
    "factual_accuracy": True, "policy_compliance": True, "conversion_guard": True, "source_independence": True,
    "alternatives_considered": True, "tracking_quality": True,
    "alternative_explanations": ["Fixture data do not establish actual benefit"]}


@pytest.fixture
def plane(monkeypatch, tmp_path):
    database_url = f"sqlite:///{tmp_path / 'acceptance.sqlite3'}"
    migrate(database_url)
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    settings = Settings(_env_file=None, environment="test", api_token=TOKENS["operator"],
        approval_token=TOKENS["reviewer"], admin_token=TOKENS["admin"], openai_model="configured-test-model")
    with factory() as session:
        sid = control.create_site(session, name="Acceptance fixture", base_url="https://example.test", fixture=True).id
    cms = FixtureCMS()
    monkeypatch.setattr(control, "cms_for_site", lambda *args: cms)

    def sessions():
        with factory() as session:
            yield session

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_session] = sessions
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            def request(method, suffix, body=None, role="operator"):
                return client.request(method, f"/api/sites/{sid}/{suffix}", json=body,
                                      headers={"Authorization": "Bearer " + TOKENS[role]})
            yield client, request, factory, sid, cms
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        engine.dispose()


def test_shadow_cycle_draft_approval_execute_and_fresh_rollback(plane):
    client, request, factory, sid, cms = plane
    cycle = request("POST", "cycle", {"idempotency_key": "acceptance-cycle"})
    assert cycle.status_code == 200
    result = cycle.json()
    assert result["status"] == "completed"
    assert result["result"]["diagnosis"]["opportunities"]
    assert result["result"]["specialists"]["decision"] == "NO-ACTION"
    assert result["result"]["execution"]["status"] == "shadow"
    replay = request("POST", "cycle", {"idempotency_key": "acceptance-cycle"}).json()
    assert replay["idempotent_replay"] and replay["job_id"] == result["job_id"]
    state = request("GET", "state").json()
    assert state["metrics"]["qualified_organic_conversion_value"] is None
    assert state["metrics"]["organic_sessions"] > 0
    page = next(p for p in request("GET", "pages").json()["items"] if p["external_id"])
    evidence = request("GET", "strategy").json()["evidence"]
    cms_evidence = next(e["id"] for e in evidence if e["source_type"] == "cms")
    before = cms.get_page(page["external_id"])
    draft = request("POST", "drafts/metadata", {"page_id": page["id"], "title": before.title + " services",
        "reason": "Test exact title revision through all assembled gates", "evidence_ids": [cms_evidence]}).json()
    rid = draft["revision_id"]
    revision = next(r for r in request("GET", "revisions").json()["items"] if r["id"] == rid)
    forecast = {"probability_of_success": .65, "success_criterion": "Positive qualified-value change at the primary checkpoint",
                "uncertainty": ["Synthetic forecast; no live calibration value"]}
    forecast_path = f"experiments/{revision['experiment_id']}/forecast"
    assert request("POST", forecast_path, forecast, "reviewer").status_code == 403
    assert request("POST", forecast_path, forecast).status_code == 200
    blocked = request("POST", f"revisions/{rid}/execute", {"idempotency_key": "preapproval-block"}).json()
    assert blocked["status"] == "blocked"
    assert cms.get_page(page["external_id"]).fingerprint == before.fingerprint
    assert request("POST", f"revisions/{rid}/human-review", REVIEW, "reviewer").status_code == 200
    assert request("POST", f"revisions/{rid}/approve", {"reason": "Approve exact reviewed fixture title"}, "reviewer").status_code == 200
    executed = request("POST", f"revisions/{rid}/execute", {"idempotency_key": "approved-execution"}).json()
    assert executed["status"] == "succeeded"
    assert request("POST", forecast_path, forecast).status_code == 409
    assert cms.get_page(page["external_id"]).fingerprint != before.fingerprint
    rollback = request("POST", f"actions/{executed['action_id']}/rollback", {"idempotency_key": "fresh-rollback"}).json()
    assert rollback["status"] == "rollback_proposed"
    reverse = rollback["revision_id"]
    assert request("POST", f"revisions/{reverse}/human-review", REVIEW, "reviewer").status_code == 200
    assert request("POST", f"revisions/{reverse}/approve", {"reason": "Approve independently reviewed exact inverse"}, "reviewer").status_code == 200
    restored = request("POST", f"actions/{executed['action_id']}/rollback", {"idempotency_key": "fresh-rollback"}).json()
    assert restored["status"] == "succeeded"
    assert cms.get_page(page["external_id"]).fingerprint == before.fingerprint
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(m.PageVersion)) >= 3
        assert session.scalar(select(func.count()).select_from(m.ActionEvent).where(m.ActionEvent.event_type == "succeeded")) == 2
        assert all(run.mode == "fixture" and run.cost_usd == 0 for run in session.scalars(select(m.AgentRun)))
    for path in ("/", "/assets/style.css", "/assets/app.js", "/readyz"):
        assert client.get(path).status_code == 200
    assert client.get("/", headers={"host": "untrusted.example.org"}).status_code == 400


@pytest.mark.parametrize("route,body", [
    ("conversion-definition", DEFINITION),
    ("model-price-bound", {"model": "configured-test-model", "usd_per_million_tokens": 10.0,
                            "verified": True, "source": "https://openai.com/api/pricing/"}),
    ("brand-facts", {"brand_name": "Example business", "services": ["Window cleaning"], "service_areas": [],
                     "source": "Owner-supplied service catalogue", "reason": "Record the actual offered services"}),
])
def test_business_configuration_is_admin_only_scoped_and_audited(plane, route, body):
    _, request, factory, sid, _ = plane
    method = "POST" if route == "brand-facts" else "PUT"
    for role in ("operator", "reviewer"):
        assert request(method, route, body, role).status_code == 403
    assert request(method, route, {**body, "production_enabled": True}, "admin").status_code == 422
    response = request(method, route, body, "admin")
    assert response.status_code in (200, 201), response.text
    receipt = response.json()
    assert receipt["actor"] == "site-administrator"
    with factory() as session:
        evidence = session.get(m.Evidence, receipt["evidence_id"])
        assert evidence.site_id == sid and evidence.content_hash == stable_hash(evidence.content)
        assert session.get(m.Action, receipt["action_id"])
        site = session.get(m.Site, sid)
        assert site.autonomy_level == 1 and not site.production_enabled


@pytest.mark.parametrize("change", [
    {"verified": "true"}, {"qualification_verified": 1}, {"qualified_events": []},
    {"deduplication_method": ""}, {"currency": "gbp"}, {"value_per_conversion": -1},
    {"value_method": "event_value"},
])
def test_invalid_business_attestation_does_not_change_state(plane, change):
    _, request, factory, sid, _ = plane
    assert request("PUT", "conversion-definition", DEFINITION | change, "admin").status_code == 422
    with factory() as session:
        assert session.get(m.Site, sid).conversion_definition["verified"] is False
        assert not session.scalar(select(m.Evidence).where(m.Evidence.source_type == "conversion_definition"))


def test_pause_is_downward_only_and_audited(plane):
    _, request, factory, sid, _ = plane
    body = {"reason": "Pause automatic activity pending investigation"}
    assert request("POST", "pause", body).status_code == 403
    assert request("POST", "pause", body, "admin").json()["status"] == "paused"
    with factory() as session:
        site = session.get(m.Site, sid)
        assert site.config_json["automation_suspended"] and not site.production_enabled
        assert session.scalar(select(m.Action).where(m.Action.kind == "pause_automation"))


def test_poor_calibration_revokes_categories_without_automatic_promotion(plane):
    _, _, factory, sid, _ = plane
    with factory() as session:
        site = session.get(m.Site, sid)
        site.config_json = {**site.config_json, "earned_categories": ["update_title", "add_internal_link"]}
        for index in range(20):
            experiment = m.Experiment(site_id=sid, name=f"Synthetic calibration {index}", hypothesis="Prespecified outcome",
                status="closed", baseline_start=m.utcnow().date() - timedelta(days=56))
            session.add(experiment)
            session.flush()
            # Trusted internal test input; no API lets a caller insert calibration rows.
            session.add(m.CalibrationRecord(site_id=sid, experiment_id=experiment.id, agent_name="content",
                action_category="update_title", predicted_confidence=.99, succeeded=False, evaluable=True,
                outcome_json={"independent": True, "is_primary_outcome": True, "adjudication_source": str(uuid4())}))
        session.commit()
        result = measurement.evaluate_due_experiments(session, sid)
        assert result["revoked_categories"] == ["update_title"]
        assert site.config_json["earned_categories"] == ["add_internal_link"]
        assert site.autonomy_level == 1
        assert measurement.evaluate_due_experiments(session, sid)["revoked_categories"] == []
        assert session.scalar(select(func.count()).select_from(m.Action).where(m.Action.kind == "reduce_autonomy")) == 1

@pytest.mark.asyncio
async def test_real_mcp_stdio_protocol_reads_canonical_api(plane):
    import asyncio
    import json
    import os
    from pathlib import Path
    import socket
    import sys
    import threading

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import uvicorn

    _, request, _, sid, _ = plane
    assert request("POST", "cycle", {"idempotency_key": "mcp-transport-fixture"}).status_code == 200
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(.02)
        assert server.started
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
               "API_TOKEN": TOKENS["operator"], "SEO_API_BASE_URL": f"http://127.0.0.1:{port}"}
        params = StdioServerParameters(command=sys.executable, args=["-m", "seo_mcp.server"], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                info = await client.initialize()
                tools = await client.list_tools()
                assert info.protocolVersion
                assert len(tools.tools) >= 30
                names = {tool.name for tool in tools.tools}
                assert "execute_arbitrary_sql" not in names and "approve_revision" not in names
                health = await client.call_tool("health", {})
                assert not health.isError, health
                response = await client.call_tool("get_site_state", {"site_id": sid})
                assert not response.isError, response
                data = json.loads(next(c.text for c in response.content if c.type == "text"))
                assert data["source_mode"] == "fixture" and not data["site"]["production_enabled"]
                assert data["metrics"]["qualified_organic_conversion_value"] is None
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        sock.close()

def test_outcome_review_requires_reviewer_and_immutable_measurement(plane):
    _, request, factory, sid, _ = plane
    with factory() as session:
        experiment = m.Experiment(site_id=sid, hypothesis="Prespecified qualified outcome", status="proposed")
        session.add(experiment)
        session.commit()
        eid = experiment.id
    body = {"measurement_action_id": str(uuid4()), "measurement_snapshot_hash": "a" * 64,
            "succeeded": True, "reason": "An independent causal assessment is required here",
            "alternative_explanations": ["Tracking errors remain possible"], "causal_confidence": .8}
    path = f"experiments/{eid}/adjudicate"
    assert request("POST", path, body).status_code == 403
    assert request("POST", path, body, "reviewer").status_code == 409
    assert request("POST", path, {**body, "independent": True}, "reviewer").status_code == 422
    assert request("POST", path, {**body, "succeeded": "true"}, "reviewer").status_code == 422
    with factory() as session:
        assert not session.scalar(select(m.CalibrationRecord))
        assert not session.scalar(select(m.Evidence).where(m.Evidence.source_type == "experiment_adjudication"))
