"""Synthetic records and an in-memory CMS only; no provider/network calls.

The live-mode test double exercises provenance gates, not real SEO efficacy.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError

from backend.app.contracts import CMSPage, ConcurrencyConflict, VerificationPacket, stable_hash, utcnow
from backend.app.db import models as m
from backend.app.db.session import make_engine, make_session_factory
from backend.app.services.execution import (
    PREDICTION_SEMANTICS,
    REQUIRED_CHECKS,
    approve_revision,
    execute_revision,
    propose_revision,
    reconcile_action,
    record_verification,
)
from backend.app.services.measurement import evaluate_due_experiments


class MemoryCMS:
    def __init__(self, page, *, fixture=False):
        self.page = page.model_copy(deep=True)
        self.is_fixture = fixture
        self.before_write = None
        self.after_write_error = False
        self.writes = 0

    def get_page(self, external_id):
        return self.page.model_copy(deep=True)

    def update_page(self, external_id, changes, *, expected_fingerprint):
        if self.before_write:
            self.before_write()
        if self.page.fingerprint != expected_fingerprint:
            raise ConcurrencyConflict("Concurrent edit")
        self.writes += 1
        self.page = self.page.model_copy(update=changes, deep=True)
        if self.after_write_error:
            raise TimeoutError("Synthetic ambiguous result")
        return self.page.model_copy(deep=True)


@dataclass
class Harness:
    session: object
    factory: object
    site: m.Site
    page: m.Page
    experiment: m.Experiment
    cms: MemoryCMS
    revision_id: str

    def execute(self, key="prediction-test"):
        return execute_revision(self.session, self.cms, revision_id=self.revision_id,
                                actor="executor", idempotency_key=key, production_enabled=True)

    def prediction(self):
        return self.session.scalar(select(m.Evidence).where(m.Evidence.source_type == "experiment_prediction"))

    def measure(self):
        return evaluate_due_experiments(self.session, self.site.id, now=self.experiment.deployed_at + timedelta(days=29))

    def add_outcomes(self):
        deployed = self.experiment.deployed_at.date()
        for offset in range(28):
            for day, amount in ((self.experiment.baseline_start + timedelta(days=offset), 100),
                                (deployed + timedelta(days=offset + 1), 80)):
                self.session.add(m.GA4Daily(site_id=self.site.id, page_id=self.page.id,
                    landing_page=self.page.url, date=day, sessions=100, qualified_conversions=4,
                    conversion_value=amount, channel="Organic Search", is_fixture=self.cms.is_fixture))
        content = {"complete": True, "quality_flags": [], "metadata": {
            "start": self.experiment.baseline_start.isoformat(), "end": (deployed + timedelta(days=28)).isoformat(),
            "scope": "all_organic_landing_pages", "tracking_verified": True,
            "conversion_definition_hash": stable_hash(self.site.conversion_definition),
            "qualified_conversion_semantics_verified": True, "qualified_conversion_value_semantics_verified": True}}
        self.session.add(m.Evidence(site_id=self.site.id, source="test:qualified-outcomes", source_type="ga4",
            content=content, content_hash=stable_hash(content), owner="ingestion", confidence=1,
            is_fixture=self.cms.is_fixture))
        self.session.commit()

    def adjudicate(self, *, owner="human-reviewer", source_type="experiment_adjudication", invalid_hash=False, **overrides):
        primary = self.experiment.analysis_json["checkpoints"]["28"]
        forecast = self.prediction()
        content = {"independent": True, "experiment_id": self.experiment.id,
            "primary_outcome": self.experiment.primary_outcome, "checkpoint_day": 28,
            "succeeded": False, "measurement_snapshot_hash": primary["input_hash"],
            "prediction_evidence_id": forecast.id, "prediction_hash": forecast.content_hash, **overrides}
        evidence = m.Evidence(site_id=self.site.id, source="test:independent-outcome-review", source_type=source_type,
            content=content, content_hash="invalid" if invalid_hash else stable_hash(content), owner=owner,
            confidence=0.9, is_fixture=self.cms.is_fixture)
        self.session.add(evidence)
        self.session.flush()
        self.experiment.analysis_json = {**self.experiment.analysis_json, "adjudication_evidence_id": evidence.id}
        self.session.commit()
        return evidence


@pytest.fixture
def harness(tmp_path):
    engine = make_engine("sqlite:///" + str(tmp_path / "prediction.db"))
    m.Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        site = m.Site(name="Synthetic provenance test", base_url="https://example.test", autonomy_level=1,
            production_enabled=True, conversion_definition={"verified": True, "qualified_events": ["qualified_lead"],
                "value_method": "verified_invoice_value"},
            config_json={"source_mode": "live", "trusted_verifier_ids": ["independent-verifier"],
                "trusted_evidence_owners": ["site-administrator"],
                "trusted_outcome_adjudicators": ["human-reviewer", "content-agent"], "max_daily_actions": 10})
        session.add(site)
        session.flush()
        snapshot = CMSPage(external_id="pages:1", url="https://example.test/windows", title="Windows",
                           content="<p>Window cleaning services.</p>", metadata={"atomic_compare_and_swap": True})
        page = m.Page(site_id=site.id, url=snapshot.url, external_id=snapshot.external_id, title=snapshot.title,
                      content_html=snapshot.content, metadata_json={"cms_snapshot": snapshot.model_dump(mode="json")})
        content = {"business": "Window cleaning services"}
        observation = m.Evidence(site_id=site.id, source="test:confirmed-business", source_type="business", content=content,
            content_hash=stable_hash(content), owner="site-administrator", confidence=1)
        session.add_all([page, observation])
        session.flush()
        today = utcnow().date()
        experiment = m.Experiment(site_id=site.id, page_id=page.id, hypothesis="Accurate titles improve qualified organic value",
            mechanism="Clarify page expectations", baseline_start=today - timedelta(days=28), baseline_end=today - timedelta(days=1),
            predicted_effect=0.15, predicted_confidence=0.8,
            analysis_json={"agent_id": "content-agent", "action_category": "update_title",
                "prediction_semantics": PREDICTION_SEMANTICS,
                "success_criterion": "Positive incremental qualified organic value attributable to this change at day 28"})
        session.add(experiment)
        session.commit()
        proposal = propose_revision(session, site_id=site.id, page_id=page.id, kind="update_title",
            after=snapshot.model_copy(update={"title": "Window cleaning services"}), created_by="content-agent",
            reason="Clarify an evidenced service", evidence_ids=[observation.id], experiment_id=experiment.id)
        assert proposal["status"] == "local_draft_created", proposal
        revision_id = proposal["revision_id"]
        record_verification(session, revision_id=revision_id, packet=VerificationPacket(
            verdict="PASS", verifier_id="independent-verifier", independent=True, confidence=0.9, action_safe=True,
            reasons=["Accurate wording and bounded edit"], evidence_ids=[observation.id],
            checks=dict.fromkeys(REQUIRED_CHECKS, True), alternative_explanations=["Demand variation"] ))
        approve_revision(session, revision_id=revision_id, approved_by="human-reviewer", reason="Reviewed exact revision")
        yield Harness(session, factory, site, page, experiment, MemoryCMS(snapshot), revision_id)
    engine.dispose()


def prepare_outcome(harness):
    result = harness.execute()
    assert result["status"] == "succeeded", result
    harness.add_outcomes()
    report = harness.measure()
    assert report["checkpoints"][-1]["verdict"] == "regression_signal", report
    return result


def test_prediction_and_dispatch_are_durable_before_first_cms_write(harness):
    seen = []

    def inspect_committed_database():
        # A separate connection cannot see uncommitted ORM state in the writer.
        with harness.factory() as observer:
            prediction = observer.scalar(select(m.Evidence).where(m.Evidence.source_type == "experiment_prediction"))
            dispatch = observer.scalar(select(m.ActionEvent).where(m.ActionEvent.event_type == "dispatching"))
            assert prediction.content_hash == stable_hash(prediction.content)
            assert dispatch.details_json["prediction_evidence_id"] == prediction.id
            assert observer.scalar(select(func.count(m.ExecutionLease.id))) == 1
            assert prediction.content["probability_of_success"] == 0.8
            assert prediction.content["specification"]["primary_outcome"] == "qualified_organic_conversion_value"
            seen.append(prediction.id)

    harness.cms.before_write = inspect_committed_database
    result = harness.execute()
    assert result["status"] == "succeeded", result
    prediction = harness.prediction()
    assert seen == [prediction.id]
    assert prediction.created_at < harness.experiment.deployed_at
    assert harness.execute()["idempotent_replay"] is True
    assert harness.cms.writes == 1
    assert harness.session.scalar(select(func.count(m.Evidence.id)).where(m.Evidence.source_type == "experiment_prediction")) == 1


def test_calibration_uses_frozen_probability_and_identity_after_mutable_edits(harness):
    prepare_outcome(harness)
    prediction = harness.prediction()
    original_hash = harness.experiment.analysis_json["checkpoints"]["28"]["input_hash"]
    harness.experiment.predicted_confidence = 0.05
    harness.experiment.predicted_effect = 9.0
    harness.experiment.analysis_json = {**harness.experiment.analysis_json,
        "agent_id": "different-agent", "action_category": "publish_page", "success_criterion": "More clicks"}
    harness.session.commit()
    assert harness.measure()["checkpoints"] == []
    assert harness.experiment.analysis_json["checkpoints"]["28"]["input_hash"] == original_hash
    harness.adjudicate()
    report = harness.measure()
    assert len(report["calibration_records"]) == 1, report
    record = harness.session.scalar(select(m.CalibrationRecord))
    assert record.evaluable is True and record.succeeded is False
    assert record.predicted_confidence == 0.8 and record.brier_score == pytest.approx(0.64)
    assert record.agent_name == "content-agent" and record.action_category == "update_title"
    assert record.outcome_json["prediction_evidence_id"] == prediction.id
    assert record.outcome_json["success_criterion"] == prediction.content["success_criterion"]
    assert harness.measure()["calibration_records"] == []
    assert harness.session.scalar(select(func.count(m.CalibrationRecord.id))) == 1


@pytest.mark.parametrize("review", [
    {"owner": "untrusted-page"}, {"owner": "content-agent"}, {"independent": False},
    {"prediction_evidence_id": "forged"}, {"prediction_hash": "forged"},
    {"measurement_snapshot_hash": "forged"}, {"invalid_hash": True},
    {"source_type": "human_observation"},
])
def test_calibration_requires_independent_hashed_forecast_and_outcome_bindings(harness, review):
    prepare_outcome(harness)
    harness.adjudicate(**review)
    assert harness.measure()["calibration_records"] == []
    assert harness.session.scalar(select(func.count(m.CalibrationRecord.id))) == 0


@pytest.mark.parametrize("change", ["missing_probability", "epistemic_confidence", "no_criterion", "false_agent"])
def test_unknown_forecasts_do_not_block_safe_action_or_become_retrospective_predictions(harness, change):
    analysis = harness.experiment.analysis_json
    if change == "missing_probability":
        harness.experiment.predicted_confidence = None
    elif change == "epistemic_confidence":
        harness.experiment.analysis_json = {**analysis, "prediction_semantics": "confidence_in_finding"}
    elif change == "no_criterion":
        harness.experiment.analysis_json = {**analysis, "success_criterion": ""}
    else:
        harness.experiment.analysis_json = {**analysis, "agent_id": "another-agent"}
    harness.session.commit()
    prepare_outcome(harness)
    frozen = harness.prediction()
    assert frozen.content["prediction_status"] == "UNKNOWN"
    assert frozen.content["probability_of_success"] is None
    harness.experiment.predicted_confidence = 0.99
    harness.experiment.analysis_json = {**harness.experiment.analysis_json, **analysis}
    harness.session.commit()
    harness.adjudicate()
    report = harness.measure()
    assert report["calibration_records"] == []
    assert report["calibration_exclusions"][0]["probability_of_success"] is None
    assert harness.cms.writes == 1


@pytest.mark.parametrize("change", ["baseline", "primary_outcome", "measurement_config", "controls", "windows", "conversion_definition", "deployment_time"])
def test_postdeployment_measurement_changes_cannot_redefine_forecast(harness, change):
    prepare_outcome(harness)
    harness.adjudicate()
    if change == "baseline":
        harness.experiment.baseline_start -= timedelta(days=1)
    elif change == "primary_outcome":
        harness.experiment.primary_outcome = "qualified_organic_conversions"
    elif change == "measurement_config":
        harness.experiment.analysis_json = {**harness.experiment.analysis_json, "measurement_config": {"minimum_days": 7}}
    elif change == "controls":
        harness.experiment.control_pages_json = [harness.page.id]
    elif change == "windows":
        harness.experiment.evaluation_windows_json = [14, 28, 56]
    elif change == "conversion_definition":
        harness.site.conversion_definition = {**harness.site.conversion_definition, "value_method": "different_value"}
    else:
        harness.experiment.deployed_at += timedelta(seconds=1)
    harness.session.commit()
    report = harness.measure()
    assert report["calibration_records"] == []
    assert report["calibration_exclusions"][0]["exclusion_reasons"]


def test_mutable_checkpoint_prose_cannot_replace_immutable_measured_outcome(harness):
    prepare_outcome(harness)
    harness.adjudicate()
    original = harness.experiment.analysis_json["checkpoints"]["28"]
    harness.experiment.analysis_json = {**harness.experiment.analysis_json,
        "checkpoints": {"28": {**original, "verdict": "benefit_signal", "input_hash": "invented-result"}}}
    harness.session.commit()
    report = harness.measure()
    assert len(report["calibration_records"]) == 1, report
    record = harness.session.scalar(select(m.CalibrationRecord))
    assert record.succeeded is False and record.outcome_json["primary_snapshot_hash"] == original["input_hash"]


def test_trusted_owner_label_and_mutable_pointer_cannot_replace_frozen_evidence(harness):
    prepare_outcome(harness)
    original = harness.prediction()
    content = {**original.content, "probability_of_success": 0.99}
    forged = m.Evidence(site_id=harness.site.id, source=original.source, source_type=original.source_type,
        owner=original.owner, confidence=1, observed_at=original.observed_at,
        content=content, content_hash=stable_hash(content))
    harness.session.add(forged)
    harness.session.flush()
    harness.experiment.analysis_json = {**harness.experiment.analysis_json, "prediction_evidence_id": forged.id}
    harness.session.commit()
    harness.adjudicate(prediction_evidence_id=forged.id, prediction_hash=forged.content_hash)
    assert harness.measure()["calibration_records"] == []
    harness.adjudicate()
    assert len(harness.measure()["calibration_records"]) == 1
    assert harness.session.scalar(select(m.CalibrationRecord)).predicted_confidence == 0.8


def test_prediction_evidence_is_database_immutable(harness):
    assert harness.execute()["status"] == "succeeded"
    identifier = harness.prediction().id
    with pytest.raises(IntegrityError):
        harness.session.execute(text("UPDATE evidence SET content_hash = 'forged' WHERE id = :identifier"), {"identifier": identifier})
    harness.session.rollback()
    prediction = harness.session.get(m.Evidence, identifier)
    assert prediction.content_hash == stable_hash(prediction.content)


def test_ambiguous_write_retains_prediction_through_reconciliation(harness):
    harness.cms.after_write_error = True
    result = harness.execute()
    assert result["status"] == "reconciliation_required", result
    prediction = harness.prediction()
    harness.session.rollback()  # Simulate losing transient work after the failed response.
    assert harness.execute()["idempotent_replay"] is True and harness.cms.writes == 1
    reconciled = reconcile_action(harness.session, harness.cms, action_id=result["action_id"], actor="operator")
    assert reconciled["status"] == "succeeded", reconciled
    assert reconciled["details"]["prediction_evidence_id"] == prediction.id
    assert harness.experiment.deployed_at > prediction.created_at
    harness.add_outcomes()
    harness.experiment.analysis_json = {**harness.experiment.analysis_json, "deployment_time_uncertain": False}
    harness.session.commit()
    report = harness.measure()
    assert "deployment_time_remains_uncertain" in report["calibration_exclusions"][0]["exclusion_reasons"]
    assert harness.cms.writes == 1


def test_storage_failure_before_prediction_commit_cannot_dispatch(harness):
    def reject_prediction(session, flush_context, instances):
        if any(isinstance(row, m.Evidence) and row.source_type == "experiment_prediction" for row in session.new):
            raise RuntimeError("Synthetic durable storage failure")

    event.listen(harness.session, "before_flush", reject_prediction)
    try:
        with pytest.raises(RuntimeError, match="durable storage failure"):
            harness.execute()
    finally:
        event.remove(harness.session, "before_flush", reject_prediction)
        harness.session.rollback()
    assert harness.cms.writes == 0
    with harness.factory() as observer:
        assert observer.scalar(select(func.count(m.ActionEvent.id)).where(m.ActionEvent.event_type == "requested")) == 1
        assert observer.scalar(select(func.count(m.Evidence.id)).where(m.Evidence.source_type == "experiment_prediction")) == 0


def test_fixture_prediction_and_outcome_never_earn_live_calibration(harness):
    harness.cms.is_fixture = True
    harness.site.config_json = {**harness.site.config_json, "source_mode": "fixture"}
    harness.session.commit()
    prepare_outcome(harness)
    harness.adjudicate()
    report = harness.measure()
    record = harness.session.scalar(select(m.CalibrationRecord))
    assert len(report["calibration_records"]) == 1
    assert record.evaluable is False and record.succeeded is None and record.brier_score is None
    assert report["calibration"]["groups"] == []
    assert harness.site.autonomy_level == 1
