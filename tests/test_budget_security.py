"""Independent durable-budget and control-loop tests; no live provider calls."""
from __future__ import annotations

import json
import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm.attributes import flag_modified

from backend.app.agents.runtime import AgentRuntime, AuditSinkError
from backend.app.config.settings import Settings
from backend.app.contracts import CMSPage, FindingPacket, ProviderUnavailable, VerificationPacket, utcnow
from backend.app.db import models as m
from backend.app.db.repositories.leases import acquire_lease
from backend.app.db.session import make_engine, make_session_factory
from backend.app.integrations.fixtures import FixtureCMS
from backend.app.scheduler.locking import site_lease_key
from backend.app.services import agent_audit, control, execution


class SimulatedLiveCMS(FixtureCMS):
    """Use real production gates with local, atomic state only."""
    is_fixture = False

    def __init__(self, pages):
        super().__init__(pages)
        self.writes = []

    def update_page(self, external_id, changes, *, expected_fingerprint):
        result = super().update_page(external_id, changes, expected_fingerprint=expected_fingerprint)
        self.writes.append(external_id)
        return result


@dataclass
class BudgetCase:
    factory: object
    session: object
    site: m.Site
    settings: Settings
    cms: SimulatedLiveCMS
    pages: list[m.Page]
    evidence_id: str

    def configure(self, **changes):
        self.site.config_json = {**self.site.config_json, **changes}
        # Deliberate corrupt-configuration tests must persist even a JSON type
        # change that Python considers equal (True == 1.0).
        flag_modified(self.site, "config_json")
        self.session.commit()

    def start(self, **changes):
        return {
            "id": str(uuid4()), "cycle_id": str(uuid4()), "role": "technical",
            "mode": "live", "model": self.settings.openai_model, "status": "started",
            "started_at": utcnow().isoformat(), "reserved_tokens": 10000,
            "reserved_model_calls": 1, "llm_attempted": False, "llm_executed": False,
            "contract": {}, **changes,
        }

    def recorder(self):
        return agent_audit.run_recorder(self.session, self.site.id, self.settings)

    def ready(self, index=0, *, approve=True):
        page = self.pages[index]
        before = CMSPage.model_validate(page.metadata_json["cms_snapshot"])
        experiment = m.Experiment(site_id=self.site.id, page_id=page.id,
                                  hypothesis="Accurate service titles may improve qualified enquiries.",
                                  primary_outcome="qualified_organic_conversion_value")
        self.session.add(experiment)
        self.session.commit()
        proposal = execution.propose_revision(self.session, site_id=self.site.id, page_id=page.id,
            kind="update_title", after=before.model_copy(update={"title": f"Accurate service title {index}"}),
            created_by="content-specialist", reason="Review exact supported service title against canonical evidence.",
            evidence_ids=[self.evidence_id], experiment_id=experiment.id)
        assert proposal["status"] == "local_draft_created", proposal
        revision_id = proposal["revision_id"]
        execution.record_verification(self.session, revision_id=revision_id, packet=VerificationPacket(
            verdict="PASS", verifier_id="human-reviewer", independent=True, confidence=.95,
            reasons=["Reviewed exact revision against the supported facts."], evidence_ids=[self.evidence_id],
            alternative_explanations=["Demand could explain the prior performance."],
            checks=dict.fromkeys(execution.REQUIRED_CHECKS, True), action_safe=True))
        if approve:
            execution.approve_revision(self.session, revision_id=revision_id,
                                      approved_by="human-reviewer", reason="Reviewed exact immutable revision.")
        return self.session.get(m.Revision, revision_id)


@pytest.fixture
def budget_case(monkeypatch):
    engine = make_engine("sqlite://")
    m.Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    settings = Settings(_env_file=None, environment="test", database_url="sqlite://",
                        agent_mode="openai", provider_mode="live", openai_model="explicit-test-model",
                        openai_api_key="explicit-test-key-never-network", production_enabled=True,
                        shadow_mode=False, autonomy_level=1, max_daily_actions=5, max_daily_cost_usd=.1)
    snapshots = [CMSPage(external_id=f"pages:{i + 1}", url=f"https://budget.example.org/service-{i}",
                         title=f"Service {i}", content="<p>Supported service description.</p>",
                         metadata={"atomic_compare_and_swap": True}) for i in range(3)]
    cms = SimulatedLiveCMS(snapshots)
    with factory() as session:
        site = control.create_site(session, name="Simulated live budget review", base_url="https://budget.example.org")
        site.production_enabled = True
        site.conversion_definition = {"verified": True}
        site.config_json = {**site.config_json, "max_daily_model_calls": 5, "max_daily_actions": 5,
                            "model_price_bound": {"model": settings.openai_model, "verified": True,
                                                  "usd_per_million_tokens": 1.0, "source": "local-test-price-bound"}}
        evidence_id = control.ingest_cms(session, site, snapshots, is_fixture=False)
        session.commit()
        pages = list(session.scalars(select(m.Page).where(m.Page.site_id == site.id).order_by(m.Page.url)))
        monkeypatch.setattr(control, "cms_for_site", lambda *args: cms)
        yield BudgetCase(factory, session, site, settings, cms, pages, evidence_id)
    engine.dispose()


def _terminal(start, **changes):
    return {**start, "status": "completed", "completed_at": utcnow().isoformat(),
            "llm_attempted": True, "llm_executed": True, "input_tokens": 100, "output_tokens": 30,
            "cost_usd": None, **changes}


def _stored(case, run_id):
    with case.factory() as session:
        row = session.get(m.AgentRun, run_id)
        return deepcopy(row.result_json)


def test_start_and_terminal_replay_reserve_once_without_state_regression(budget_case):
    record, start = budget_case.recorder(), budget_case.start()
    terminal = _terminal(start, reserved_model_calls=0, reserved_cost_upper_bound_usd=0)
    record(start)
    record(start)
    record(terminal)
    record(terminal)
    record(start)  # Delayed duplicate acknowledgement cannot erase completion.
    stored = _stored(budget_case, start["id"])
    assert stored["reserved_model_calls"] == 1
    assert stored["reserved_cost_upper_bound_usd"] == pytest.approx(.01)
    assert stored["status"] == "completed"
    with budget_case.factory() as session:
        assert session.scalar(select(func.count()).select_from(m.AgentRun)) == 1
        actions = session.scalars(select(m.Action).where(m.Action.kind == "agent_run")).all()
        assert len(actions) == 2


def test_denied_start_replay_does_not_become_an_unreserved_acknowledgement(budget_case):
    budget_case.settings.max_daily_cost_usd = 0
    record, start = budget_case.recorder(), budget_case.start()
    for _ in range(2):
        with pytest.raises((ProviderUnavailable, ValueError)):
            record(start)
    stored = _stored(budget_case, start["id"])
    assert stored["status"] == "budget_blocked"
    assert stored["reserved_model_calls"] == 0


@pytest.mark.parametrize("change", [
    {"reserved_tokens": 400000},
    {"model": "unpriced-different-model"},
    {"started_at": (utcnow() - timedelta(days=1)).isoformat()},
])
def test_start_replay_cannot_change_the_paid_request_binding(budget_case, change):
    record, start = budget_case.recorder(), budget_case.start()
    record(start)
    with pytest.raises((ProviderUnavailable, ValueError)):
        record({**start, **change})
    stored = _stored(budget_case, start["id"])
    assert stored["reserved_tokens"] == start["reserved_tokens"]
    assert stored["model"] == start["model"]
    assert stored["started_at"] == start["started_at"]


def test_terminal_result_cannot_invent_a_reservation_or_refund_an_ambiguous_call(budget_case):
    record, start = budget_case.recorder(), budget_case.start()
    with pytest.raises(ValueError):
        record(_terminal(start))
    record(start)
    record(_terminal(start, status="error", llm_executed=False, reserved_model_calls=0,
                     reserved_cost_upper_bound_usd=0, error_type="TimeoutError"))
    stored = _stored(budget_case, start["id"])
    assert stored["reserved_model_calls"] == 1
    assert stored["reserved_cost_upper_bound_usd"] == pytest.approx(.01)
    budget_case.settings.max_daily_cost_usd = .01
    with pytest.raises(ProviderUnavailable):
        record(budget_case.start())


@pytest.mark.parametrize("bound", [
    {}, {"verified": False}, {"model": "wrong-model"}, {"source": ""},
    {"usd_per_million_tokens": 0}, {"usd_per_million_tokens": True},
    {"usd_per_million_tokens": float("inf")},
])
def test_unverified_or_invalid_price_bound_cannot_reserve(budget_case, bound):
    original = budget_case.site.config_json["model_price_bound"]
    budget_case.configure(model_price_bound={} if not bound else original | bound)
    start = budget_case.start()
    with pytest.raises(ProviderUnavailable):
        budget_case.recorder()(start)
    assert _stored(budget_case, start["id"])["reserved_model_calls"] == 0


@pytest.mark.parametrize("barrier", ["cost", "calls", "suspended", "audit_storage"])
@pytest.mark.asyncio
async def test_durable_budget_rejection_happens_before_sdk_spend(budget_case, monkeypatch, barrier):
    import agents
    import openai

    if barrier == "cost":
        budget_case.settings.max_daily_cost_usd = 0
    elif barrier == "calls":
        budget_case.configure(max_daily_model_calls=0)
    elif barrier == "suspended":
        budget_case.configure(automation_suspended=True)
    else:
        def broken_commit():
            raise RuntimeError("simulated-durable-storage-failure")
        monkeypatch.setattr(budget_case.session, "commit", broken_commit)
    spent = []

    class NoSpendClient:
        def __init__(self, **kwargs):
            spent.append("client-opened")
            raise AssertionError("SDK client opened without a durable budget acknowledgement")

    async def forbidden_model(*args, **kwargs):
        spent.append("model-called")
        raise AssertionError("Model invoked despite budget rejection")

    monkeypatch.setattr(openai, "AsyncOpenAI", NoSpendClient)
    monkeypatch.setattr(agents.Runner, "run", forbidden_model)
    options = agent_audit.runtime_options(budget_case.session, budget_case.site, budget_case.settings)
    runtime = AgentRuntime(**options)
    with pytest.raises(AuditSinkError):
        await runtime._invoke("technical", {"problem": {}, "evidence": [{"id": budget_case.evidence_id}]}, FindingPacket)
    assert spent == []
    assert runtime.model_calls == 0


@pytest.mark.asyncio
async def test_explicit_settings_and_committed_cost_survive_sdk_error_without_secret_logs(budget_case, monkeypatch, caplog):
    import agents
    import openai

    key = budget_case.settings.openai_api_key.get_secret_value()
    monkeypatch.setenv("OPENAI_MODEL", "wrong-environment-model")
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-environment-key")
    client_keys, calls = [], []

    class LocalClient:
        def __init__(self, *, api_key, max_retries, timeout):
            assert max_retries == 0
            assert timeout > 0
            client_keys.append(api_key)
            with budget_case.factory() as session:
                row = session.scalar(select(m.AgentRun).where(m.AgentRun.site_id == budget_case.site.id))
                assert row.status == "started"
                assert row.result_json["reserved_model_calls"] == 1
                assert row.result_json["reserved_cost_upper_bound_usd"] > 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    async def local_model(agent, input, **kwargs):
        assert agent.model == budget_case.settings.openai_model
        assert key not in input + agent.instructions
        calls.append(agent.name)
        raise ValueError(f"Upstream response accidentally included {key}")

    monkeypatch.setattr(openai, "AsyncOpenAI", LocalClient)
    monkeypatch.setattr(agents.Runner, "run", local_model)
    options = agent_audit.runtime_options(budget_case.session, budget_case.site, budget_case.settings)
    runtime = AgentRuntime(**options)
    with caplog.at_level(logging.INFO, logger="backend.app.agents.runtime"):
        result = await runtime._invoke("technical", {"problem": {}, "evidence": [{"id": budget_case.evidence_id}]}, FindingPacket)
    assert result is None
    assert calls == ["technical"] and client_keys == [key]
    stored = _stored(budget_case, runtime.runs[0]["id"])
    assert stored["status"] == "error" and stored["reserved_model_calls"] == 1
    assert stored["reserved_cost_upper_bound_usd"] > 0
    log_packets = [getattr(row, "agent_run", {}) for row in caplog.records]
    with budget_case.factory() as session:
        events = [row.details_json for row in session.scalars(select(m.ActionEvent))]
    assert key not in json.dumps([runtime.runs, stored, log_packets, events])
    assert key not in caplog.text


@pytest.mark.parametrize("gate", ["global_disabled", "shadow", "site_disabled", "fixture", "global_zero", "site_zero", "suspended"])
def test_execution_queue_does_not_open_cms_when_gate_is_closed(budget_case, monkeypatch, gate):
    budget_case.ready()
    if gate == "global_disabled":
        budget_case.settings.production_enabled = False
    elif gate == "shadow":
        budget_case.settings.shadow_mode = True
    elif gate == "site_disabled":
        budget_case.site.production_enabled = False
        budget_case.session.commit()
    elif gate == "fixture":
        budget_case.configure(source_mode="fixture")
    elif gate == "global_zero":
        budget_case.settings.max_daily_actions = 0
    elif gate == "site_zero":
        budget_case.configure(max_daily_actions=0)
    else:
        budget_case.configure(automation_suspended=True)

    def forbidden(*args):
        raise AssertionError("Closed queue gate still opened the CMS")

    monkeypatch.setattr(control, "cms_for_site", forbidden)
    result = control.execute_eligible_revisions(budget_case.session, budget_case.site, budget_case.settings)
    assert result["executed"] == []
    assert budget_case.cms.writes == []


@pytest.mark.parametrize("decision", ["expired", "REJECT", "REVOKE"])
def test_queue_rechecks_latest_human_authority_even_when_category_is_earned(budget_case, decision):
    revision = budget_case.ready()
    if decision != "expired":
        budget_case.site.autonomy_level = budget_case.settings.autonomy_level = 2
        budget_case.configure(earned_categories=["update_title"])
    budget_case.session.add(m.Approval(site_id=budget_case.site.id, revision_id=revision.id,
        revision_hash=revision.revision_hash, approved_by="human-reviewer",
        decision="APPROVE" if decision == "expired" else decision, reason="Latest human review decision.",
        expires_at=utcnow() - timedelta(seconds=1)))
    budget_case.session.commit()
    result = control.execute_eligible_revisions(budget_case.session, budget_case.site, budget_case.settings)
    assert not any(row["status"] == "succeeded" for row in result.get("results", []))
    assert budget_case.cms.writes == []


def test_repeated_queue_uses_completed_events_to_skip_successful_revision(budget_case, monkeypatch):
    budget_case.ready()
    first = control.execute_eligible_revisions(budget_case.session, budget_case.site, budget_case.settings)
    assert first["results"][0]["status"] == "succeeded", first

    def forbidden(*args):
        raise AssertionError("Completed revision was selected for another CMS queue pass")

    monkeypatch.setattr(control, "cms_for_site", forbidden)
    repeated = control.execute_eligible_revisions(budget_case.session, budget_case.site, budget_case.settings)
    assert repeated["results"] == []
    assert len(budget_case.cms.writes) == 1


def test_global_daily_action_cap_is_not_reset_by_a_new_cycle(budget_case):
    budget_case.settings.max_daily_actions = 1
    budget_case.ready(0)
    first = control.execute_eligible_revisions(budget_case.session, budget_case.site, budget_case.settings)
    assert first["results"][0]["status"] == "succeeded", first
    budget_case.ready(1)  # A newer eligible revision must not reset today's global cap.
    second = control.execute_eligible_revisions(budget_case.session, budget_case.site, budget_case.settings)
    assert not any(row["status"] == "succeeded" for row in second.get("results", [])), second
    assert len(budget_case.cms.writes) == 1


def test_cycle_idempotency_and_busy_lease_do_not_repeat_cost_or_mutation(budget_case, monkeypatch):
    calls = []
    for operation in ("ingest_site", "analyze_site", "run_specialists", "execute_eligible_revisions", "evaluate_due_experiments"):
        def local_operation(*args, _operation=operation):
            calls.append(_operation)
            return {"local_operation": _operation}
        monkeypatch.setattr(control, operation, local_operation)
    first = control.run_cycle(budget_case.session, budget_case.site.id, budget_case.settings, idempotency_key="one-cycle")
    replay = control.run_cycle(budget_case.session, budget_case.site.id, budget_case.settings, idempotency_key="one-cycle")
    assert first["status"] == replay["status"] == "completed"
    assert first["job_id"] == replay["job_id"] and replay["idempotent_replay"]
    assert len(calls) == 5
    with budget_case.factory() as session:
        lease = acquire_lease(session, site_lease_key(budget_case.site.id), "competitor", site_id=budget_case.site.id)
        session.commit()
        assert lease is not None
    busy = control.run_cycle(budget_case.session, budget_case.site.id, budget_case.settings, idempotency_key="other-cycle")
    assert busy["status"] == "busy"
    assert len(calls) == 5
    assert budget_case.session.scalar(select(func.count()).select_from(m.JobRun)) == 1


def test_failed_cycle_replay_retains_reservation_and_redacts_failure(budget_case, monkeypatch):
    secret, calls = "cycle-secret-never-log-this-value", []
    monkeypatch.setattr(control, "ingest_site", lambda *args: {"status": "local"})
    monkeypatch.setattr(control, "analyze_site", lambda *args: {"status": "local"})
    start = budget_case.start()

    def fail_after_reserving(*args):
        calls.append("specialists")
        budget_case.recorder()(start)
        raise RuntimeError(secret)

    monkeypatch.setattr(control, "run_specialists", fail_after_reserving)
    with pytest.raises(RuntimeError):
        control.run_cycle(budget_case.session, budget_case.site.id, budget_case.settings, idempotency_key="failed-cycle")
    replay = control.run_cycle(budget_case.session, budget_case.site.id, budget_case.settings, idempotency_key="failed-cycle")
    assert replay["status"] == "failed" and replay["idempotent_replay"]
    assert calls == ["specialists"]
    assert _stored(budget_case, start["id"])["reserved_model_calls"] == 1
    with budget_case.factory() as session:
        job = session.scalar(select(m.JobRun))
        failure = session.scalar(select(m.FailureCase))
        assert job.error == "RuntimeError"
        assert secret not in json.dumps([job.result_json, failure.actual, failure.details_json])
    assert secret not in json.dumps(replay)
