"""Scoped forecast and independent outcome review; no authority to publish."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, StrictBool
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.auth import Principal, authenticate, reviewer
from backend.app.contracts import StrictModel, stable_hash, utcnow
from backend.app.db import models as m
from backend.app.db.session import get_session
from backend.app.services import control
from backend.app.services.execution import PREDICTION_SEMANTICS

router = APIRouter(prefix="/api/sites/{site_id}/experiments", tags=["Experiment review"])
DB = Annotated[Session, Depends(get_session)]
User = Annotated[Principal, Depends(authenticate)]
Reviewer = Annotated[Principal, Depends(reviewer)]


class Forecast(StrictModel):
    probability_of_success: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    predicted_effect: float | None = Field(default=None, allow_inf_nan=False)
    success_criterion: str = Field(min_length=10, max_length=2000)
    uncertainty: list[str] = Field(min_length=1, max_length=20)


class OutcomeReview(StrictModel):
    measurement_action_id: UUID
    measurement_snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    succeeded: StrictBool
    reason: str = Field(min_length=10, max_length=3000)
    alternative_explanations: list[str] = Field(min_length=1, max_length=20)
    causal_confidence: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    rollback_safe_to_propose: StrictBool = False


@router.post("/{experiment_id}/forecast")
def forecast(site_id: UUID, experiment_id: UUID, body: Forecast, session: DB, user: User):
    site = control.site_record(session, str(site_id))
    if site.autonomy_level < 1:
        raise HTTPException(403, "Observer mode cannot propose forecasts")
    experiment = session.scalar(select(m.Experiment).where(m.Experiment.site_id == site.id,
        m.Experiment.id == str(experiment_id)).with_for_update().execution_options(populate_existing=True))
    if experiment is None:
        raise HTTPException(404, "Experiment not found in this site")
    revisions = list(session.scalars(select(m.Revision).where(m.Revision.site_id == site.id,
        m.Revision.experiment_id == experiment.id)))
    if not revisions or any(r.created_by != user.actor for r in revisions):
        raise HTTPException(403, "Forecasts must belong to the principal who proposed the exact revision")
    dispatched = session.scalar(select(m.ActionEvent.id).join(m.Action, m.Action.id == m.ActionEvent.action_id)
        .where(m.Action.experiment_id == experiment.id, m.ActionEvent.event_type == "dispatching").limit(1))
    if experiment.deployed_at or dispatched:
        raise HTTPException(409, "A dispatched experiment cannot acquire or change its forecast")
    experiment.predicted_confidence = body.probability_of_success
    experiment.predicted_effect = body.predicted_effect
    experiment.analysis_json = {**experiment.analysis_json, "agent_id": user.actor,
        "prediction_semantics": PREDICTION_SEMANTICS, "success_criterion": body.success_criterion,
        "forecast_uncertainty": body.uncertainty}
    content = {**body.model_dump(), "experiment_id": experiment.id, "actor": user.actor,
               "confidence_semantics": PREDICTION_SEMANTICS}
    evidence = m.Evidence(site_id=site.id, source="control-plane:forecast-proposal", source_type="forecast_proposal",
        content=content, content_hash=stable_hash(content), owner=user.actor, observed_at=utcnow(),
        is_fixture=site.config_json.get("source_mode") == "fixture")
    session.add(evidence)
    session.flush()
    control.local_audit(session, site.id, "propose_forecast", user.actor, body.success_criterion,
                        {"experiment_id": experiment.id, "evidence_id": evidence.id})
    session.commit()
    return {"status": "prespecified_proposal", "evidence_id": evidence.id,
            "note": "Executor freezes the forecast at dispatch; independent outcomes are still required"}


@router.post("/{experiment_id}/adjudicate")
def adjudicate(site_id: UUID, experiment_id: UUID, body: OutcomeReview, session: DB, user: Reviewer):
    site = control.site_record(session, str(site_id))
    if user.actor not in site.config_json.get("trusted_outcome_adjudicators", ["human-reviewer"]):
        raise HTTPException(403, "This principal is not an independent outcome adjudicator")
    experiment = control.scoped_record(session, m.Experiment, site.id, str(experiment_id))
    if session.scalar(select(m.CalibrationRecord.id).where(m.CalibrationRecord.site_id == site.id,
                                                          m.CalibrationRecord.experiment_id == experiment.id)):
        raise HTTPException(409, "Already calibrated; conflicting reviews require explicit reconciliation")
    # Pick the actual immutable analysis referenced by the review.
    metrics = session.scalars(select(m.ExperimentMetric).where(m.ExperimentMetric.site_id == site.id,
        m.ExperimentMetric.experiment_id == experiment.id))
    metric = next((item for item in metrics if item.analysis_json.get("measurement_action_id") == str(body.measurement_action_id)
                   and item.analysis_json.get("input_hash") == body.measurement_snapshot_hash), None)
    primary_day = experiment.analysis_json.get("measurement_config", {}).get("primary_checkpoint", 28)
    if (metric is None or metric.analysis_json.get("verdict") == "inconclusive"
            or metric.metric != experiment.primary_outcome or metric.checkpoint_days != primary_day):
        raise HTTPException(409, "Review must reference a measured, non-inconclusive immutable checkpoint")
    prediction = metric.analysis_json.get("prediction", {})
    if prediction.get("agent_id") == user.actor:
        raise HTTPException(403, "The forecaster cannot independently adjudicate its own outcome")
    content = {"independent": True, "experiment_id": experiment.id, "primary_outcome": experiment.primary_outcome,
        "checkpoint_day": metric.checkpoint_days, "succeeded": body.succeeded, "reason": body.reason,
        "alternative_explanations": body.alternative_explanations, "causal_confidence": body.causal_confidence,
        "rollback_safe_to_propose": body.rollback_safe_to_propose,
        "measurement_snapshot_hash": body.measurement_snapshot_hash,
        "prediction_evidence_id": prediction.get("evidence_id"), "prediction_hash": prediction.get("content_hash")}
    evidence = m.Evidence(site_id=site.id, source="control-plane:independent-outcome-review",
        source_type="experiment_adjudication", content=content, content_hash=stable_hash(content),
        owner=user.actor, observed_at=utcnow(), confidence=body.causal_confidence,
        is_fixture=site.config_json.get("source_mode") == "fixture")
    session.add(evidence)
    session.flush()
    experiment.analysis_json = {**experiment.analysis_json, "adjudication_evidence_id": evidence.id}
    control.local_audit(session, site.id, "adjudicate_experiment", user.actor, body.reason,
                        {"experiment_id": experiment.id, "evidence_id": evidence.id})
    session.commit()
    return {"status": "review_recorded", "evidence_id": evidence.id,
            "note": "Measurement engine rechecks current data, forecast provenance and independence before calibration"}
