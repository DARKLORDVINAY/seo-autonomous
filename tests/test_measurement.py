from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from backend.app.contracts import stable_hash
from backend.app.db import models as m
from backend.app.db.session import make_engine, make_session_factory
from backend.app.services.measurement import evaluate_due_experiments


BASE = date(2026, 6, 1)
DEPLOYED = datetime(2026, 6, 29, 12, tzinfo=timezone.utc)
NOW = DEPLOYED + timedelta(days=29)


@pytest.fixture
def session():
    engine = make_engine("sqlite://")
    m.Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        yield session
    engine.dispose()


def setup_experiment(session, *, after_value=80, controls=False, mapping=True, complete=True):
    site = m.Site(name="Measurement site", base_url="https://example.com", autonomy_level=1,
                  conversion_definition={"verified": mapping, "qualified_events": ["qualified_lead"], "value_method": "verified_invoice_value"},
                  config_json={"source_mode": "live", "trusted_outcome_adjudicators": ["human:reviewer"]})
    session.add(site)
    session.flush()
    page = m.Page(site_id=site.id, url=site.base_url + "/services")
    session.add(page)
    session.flush()
    control = None
    if controls:
        control = m.Page(site_id=site.id, url=site.base_url + "/control")
        session.add(control)
        session.flush()
    experiment = m.Experiment(site_id=site.id, page_id=page.id, name="Useful title", hypothesis="Qualified organic value will increase",
        mechanism="Improve accurate expectation-setting", baseline_start=BASE, baseline_end=BASE + timedelta(days=27),
        deployed_at=DEPLOYED, status="running", predicted_effect=0.15, predicted_confidence=0.8,
        control_pages_json=[control.id] if control else [], analysis_json={"agent_id": "content-strategist", "action_category": "update_title"})
    session.add(experiment)
    session.flush()
    for target in [page] + ([control] if control else []):
        for offset in range(28):
            for observed, amount in ((BASE + timedelta(days=offset), 100), (DEPLOYED.date() + timedelta(days=offset + 1), after_value)):
                session.add(m.GA4Daily(site_id=site.id, page_id=target.id, landing_page=target.url, date=observed,
                    sessions=50 if observed < DEPLOYED.date() else 100, qualified_conversions=4, conversion_value=amount,
                    channel="Organic Search", is_fixture=False))
    evidence = m.Evidence(site_id=site.id, source="verified:ga4-qualified-outcomes", source_type="ga4", confidence=1,
        owner="ingestion", content={"complete": complete, "quality_flags": [], "metadata": {
            "start": BASE.isoformat(), "end": (DEPLOYED.date() + timedelta(days=28)).isoformat(),
            "scope": "all_organic_landing_pages", "tracking_verified": True,
            "conversion_definition_hash": stable_hash(site.conversion_definition),
            "qualified_conversion_semantics_verified": True, "qualified_conversion_value_semantics_verified": True}})
    session.add(evidence)
    session.commit()
    return site, page, experiment, evidence


def count(session, model):
    return session.scalar(select(func.count(model.id)))


def adjudication(session, site, experiment, *, trusted=True, succeeded=False, snapshot_override=None, causal=False):
    primary = experiment.analysis_json["checkpoints"]["28"]
    evidence = m.Evidence(site_id=site.id, source="human-reviewed:case-001", source_type="experiment_adjudication", confidence=0.9,
        owner="human:reviewer" if trusted else "content-strategist",
        content={"independent": True, "experiment_id": experiment.id, "primary_outcome": experiment.primary_outcome,
                 "checkpoint_day": 28, "succeeded": succeeded,
                 "measurement_snapshot_hash": snapshot_override or primary["input_hash"],
                 "causal_confidence": 0.9 if causal else None, "rollback_safe_to_propose": causal})
    evidence.content_hash = stable_hash(evidence.content)
    session.add(evidence)
    session.flush()
    experiment.analysis_json = {**experiment.analysis_json, "adjudication_evidence_id": evidence.id}
    session.commit()
    return evidence


def test_due_checkpoints_are_persisted_once_and_every_mutation_is_audited(session):
    site, page, experiment, evidence = setup_experiment(session)
    output = evaluate_due_experiments(session, site.id, now=NOW)
    assert [result["checkpoint_day"] for result in output["checkpoints"]] == [7, 14, 28]
    assert experiment.verdict == "regression_signal"
    assert count(session, m.ExperimentMetric) == count(session, m.Action) == count(session, m.ActionEvent) == 3
    assert count(session, m.DecisionLog) == 3
    assert count(session, m.FailureCase) == 1
    assert count(session, m.RollbackEvent) == 0  # A temporal association is not adequate rollback evidence.
    assert count(session, m.CalibrationRecord) == 0  # The analyst cannot grade its own prediction.
    repeat = evaluate_due_experiments(session, site.id, now=NOW)
    assert repeat["checkpoints"] == [] and count(session, m.ExperimentMetric) == 3
    assert output["production_mutations"] == 0
    assert site.autonomy_level == 1


def test_current_partial_day_is_not_evaluated(session):
    site, _, _, _ = setup_experiment(session)
    output = evaluate_due_experiments(session, site.id, now=DEPLOYED + timedelta(days=28))
    assert [result["checkpoint_day"] for result in output["checkpoints"]] == [7, 14]


def test_shared_decline_with_distinct_control_is_not_labelled_action_harm(session):
    site, _, experiment, _ = setup_experiment(session, controls=True)
    evaluate_due_experiments(session, site.id, now=NOW)
    primary = experiment.analysis_json["checkpoints"]["28"]
    assert primary["verdict"] == "no_material_change"
    assert primary["difference_in_differences_per_day"] == 0
    assert primary["provenance"]["baseline"]["page_ids"] != primary["provenance"]["control_baseline"]["page_ids"]


def test_self_control_is_rejected_instead_of_fabricating_adjustment(session):
    site, page, experiment, _ = setup_experiment(session)
    experiment.control_pages_json = [page.id]
    session.commit()
    output = evaluate_due_experiments(session, site.id, now=NOW)
    assert all(item["verdict"] == "inconclusive" for item in output["checkpoints"])
    assert "distinct" in output["checkpoints"][-1]["reasons"][0]


@pytest.mark.parametrize("mapping,complete", [(False, True), (True, False)])
def test_unverified_conversion_or_incomplete_extraction_abstains(session, mapping, complete):
    site, _, experiment, _ = setup_experiment(session, mapping=mapping, complete=complete)
    output = evaluate_due_experiments(session, site.id, now=NOW)
    assert all(item["verdict"] == "inconclusive" for item in output["checkpoints"])
    assert count(session, m.CalibrationRecord) == count(session, m.RollbackEvent) == 0
    assert experiment.verdict == "inconclusive"


def test_late_complete_evidence_supersedes_abstention_without_overwriting_history(session):
    site, _, experiment, evidence = setup_experiment(session, complete=False)
    first = evaluate_due_experiments(session, site.id, now=NOW)
    assert first["checkpoints"][-1]["verdict"] == "inconclusive"
    corrected = m.Evidence(site_id=site.id, source="verified:corrected-full-import", source_type="ga4", confidence=1,
        observed_at=evidence.observed_at + timedelta(seconds=1), owner="ingestion", content={**evidence.content, "complete": True})
    session.add(corrected)
    session.commit()
    next_result = evaluate_due_experiments(session, site.id, now=NOW)
    assert next_result["checkpoints"][-1]["verdict"] == "regression_signal"
    assert count(session, m.ExperimentMetric) == 6
    assert experiment.analysis_json["checkpoints"]["28"]["input_hash"] != first["checkpoints"][-1]["input_hash"]


def test_retrospective_confidence_cannot_earn_calibration_even_with_reviewed_outcome(session):
    site, _, experiment, _ = setup_experiment(session)
    evaluate_due_experiments(session, site.id, now=NOW)
    adjudication(session, site, experiment, trusted=False)
    assert evaluate_due_experiments(session, site.id, now=NOW)["calibration_records"] == []
    adjudication(session, site, experiment, snapshot_override="wrong-observation")
    assert evaluate_due_experiments(session, site.id, now=NOW)["calibration_records"] == []
    adjudication(session, site, experiment)
    report = evaluate_due_experiments(session, site.id, now=NOW)
    # This legacy experiment was inserted after deployment without a recorded
    # executor prediction. Even a valid reviewer cannot create that provenance.
    assert report["calibration_records"] == []
    assert report["calibration_exclusions"][0]["status"] == "UNKNOWN"
    assert report["calibration"]["groups"] == []
    assert evaluate_due_experiments(session, site.id, now=NOW)["calibration_records"] == []
    assert count(session, m.CalibrationRecord) == 0


def test_adequately_reviewed_regression_only_proposes_preserved_revision_rollback(session):
    site, page, experiment, _ = setup_experiment(session)
    executed = m.Action(site_id=site.id, experiment_id=experiment.id, kind="update_title", risk="MEDIUM", actor="executor",
                        reason="Previously approved change", idempotency_key="prior-change")
    session.add(executed)
    session.flush()
    session.add_all([m.ActionEvent(site_id=site.id, action_id=executed.id, event_type="succeeded"),
                     m.PageVersion(site_id=site.id, page_id=page.id, action_id=executed.id, version_number=1,
                                   content_hash="before", content_json={"title": "Preserved old title"})])
    session.commit()
    evaluate_due_experiments(session, site.id, now=NOW)
    adjudication(session, site, experiment, causal=True)
    report = evaluate_due_experiments(session, site.id, now=NOW)
    proposal = session.scalar(select(m.RollbackEvent))
    assert proposal.status == "proposed" and proposal.action_id == executed.id
    assert proposal.rollback_action_id is None
    assert proposal.details_json["automatic_execution"] is False
    assert report["rollback_execution"] is False


def test_other_site_evidence_cannot_establish_completeness(session):
    site, _, _, evidence = setup_experiment(session, complete=False)
    other = m.Site(name="Other", base_url="https://elsewhere.com")
    session.add(other)
    session.flush()
    session.add(m.Evidence(site_id=other.id, source="complete-unrelated-site", source_type="ga4", confidence=1,
                           content={**evidence.content, "complete": True}, observed_at=evidence.observed_at + timedelta(seconds=1)))
    session.commit()
    report = evaluate_due_experiments(session, site.id, now=NOW)
    assert report["checkpoints"][-1]["verdict"] == "inconclusive"
