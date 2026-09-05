"""Persistent checkpoint evaluation with auditable abstention and no CMS authority.

The service uses the canonical database rather than conversational state. The
site row serialises concurrent PostgreSQL evaluators; immutable action keys add
an idempotency fence. Incomplete checkpoints may be superseded by new evidence,
never overwritten. Backend scheduling owns calls to this service.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.contracts import GA4Row, stable_hash, utcnow
from backend.app.db import models as m
from backend.app.experiments.evaluation import (
    CalibrationObservation,
    ExperimentSpec,
    calibration_report,
    evaluate_experiment,
    observation_window,
)
from backend.app.services.execution import (
    PREDICTION_OWNER,
    PREDICTION_SEMANTICS,
    PREDICTION_SOURCE,
    _revision_digest,
    prediction_specification,
)


ENGINE_VERSION = "descriptive-measurement-v2"


def _prediction_provenance(session: Session, site: m.Site, experiment: m.Experiment) -> tuple[m.Evidence | None, list[str]]:
    """Resolve predictions through immutable executor records, never JSON pointers.

    A trusted owner label on arbitrary Evidence is insufficient. The committed
    dispatch event, exact revision and successful execution must all bind it.
    """
    actions = list(session.scalars(select(m.Action).join(m.ActionEvent, m.ActionEvent.action_id == m.Action.id).where(
        m.Action.site_id == site.id, m.Action.experiment_id == experiment.id,
        m.ActionEvent.site_id == site.id, m.ActionEvent.event_type == "succeeded",
    ).distinct()))
    if len(actions) != 1:
        return None, ["exactly_one_executed_action_required_for_prediction"]
    action = actions[0]
    revision = session.get(m.Revision, action.revision_id) if action.revision_id else None
    events = list(session.scalars(select(m.ActionEvent).where(m.ActionEvent.site_id == site.id, m.ActionEvent.action_id == action.id)))
    dispatches = [event for event in events if event.event_type == "dispatching"]
    successes = [event for event in events if event.event_type == "succeeded"]
    if (revision is None or revision.site_id != site.id or revision.experiment_id != experiment.id
        or revision.revision_hash != _revision_digest(revision) or len(dispatches) != 1 or len(successes) != 1
        or action.kind != revision.kind or action.payload_json.get("operation") != "execute_revision"
        or action.payload_json.get("revision_hash") != revision.revision_hash):
        return None, ["prediction_execution_binding_invalid"]
    dispatch, success = dispatches[0], successes[0]
    identifier = dispatch.details_json.get("prediction_evidence_id")
    evidence = session.scalar(select(m.Evidence).where(m.Evidence.site_id == site.id, m.Evidence.id == identifier,
        m.Evidence.source_type == "experiment_prediction", m.Evidence.status == "active")) if isinstance(identifier, str) else None
    if (evidence is None or evidence.owner != PREDICTION_OWNER or evidence.source != PREDICTION_SOURCE
        or not evidence.content_hash or evidence.content_hash != stable_hash(evidence.content)
        or dispatch.details_json.get("prediction_hash") != evidence.content_hash
        or success.details_json.get("prediction_evidence_id") != evidence.id
        or success.details_json.get("prediction_hash") != evidence.content_hash):
        return None, ["immutable_prediction_evidence_missing_or_invalid"]
    packet = evidence.content
    if (packet.get("version") != 1 or packet.get("site_id") != site.id
        or packet.get("experiment_id") != experiment.id or packet.get("action_id") != action.id
        or packet.get("revision_id") != revision.id or packet.get("revision_hash") != revision.revision_hash
        or packet.get("agent_id") != revision.created_by or packet.get("action_category") != revision.kind
        or action.payload_json.get("provider_is_fixture") is not evidence.is_fixture):
        return None, ["prediction_identity_binding_invalid"]
    try:
        frozen = datetime.fromisoformat(packet["frozen_at"])
        deployed = datetime.fromisoformat(success.details_json["deployment_at"])
        if (frozen.tzinfo is None or deployed.tzinfo is None or experiment.deployed_at != deployed
            or frozen != evidence.observed_at or not action.created_at <= frozen <= evidence.created_at <= dispatch.created_at
            or not dispatch.created_at <= deployed <= success.created_at
            or evidence.created_at >= deployed):
            return None, ["prediction_not_immutably_recorded_before_deployment"]
    except (KeyError, ValueError, TypeError):
        return None, ["prediction_deployment_timestamp_invalid"]
    reasons = []
    if packet.get("specification") != prediction_specification(experiment, site, revision):
        reasons.append("prespecified_experiment_configuration_changed")
    if (success.details_json.get("deployment_time_uncertain") is True
        or (experiment.analysis_json or {}).get("deployment_time_uncertain") is True):
        reasons.append("deployment_time_remains_uncertain")
    supplied = packet.get("probability_of_success")
    if (packet.get("prediction_status") != "PRESPECIFIED" or packet.get("confidence_semantics") != PREDICTION_SEMANTICS
        or not isinstance(supplied, (int, float)) or isinstance(supplied, bool) or not math.isfinite(supplied) or not 0 <= supplied <= 1
        or not isinstance(packet.get("success_criterion"), str) or not packet["success_criterion"].strip()):
        reasons.extend(packet.get("exclusion_reasons") or ["probability_of_success_not_prespecified"])
    return evidence, sorted(set(reasons))


def _prediction_summary(session: Session, site: m.Site, experiment: m.Experiment) -> dict[str, Any]:
    evidence, reasons = _prediction_provenance(session, site, experiment)
    return {
        "status": "PRESPECIFIED" if evidence is not None and not reasons else "UNKNOWN",
        "evidence_id": evidence.id if evidence is not None else None,
        "content_hash": evidence.content_hash if evidence is not None else None,
        "probability_of_success": evidence.content["probability_of_success"] if evidence is not None and not reasons else None,
        "agent_id": evidence.content["agent_id"] if evidence is not None else "unknown",
        "action_category": evidence.content["action_category"] if evidence is not None else "unknown",
        "predicted_effect": evidence.content.get("predicted_effect") if evidence is not None else None,
        "exclusion_reasons": reasons,
    }


def _date_range(start: date, end: date) -> set[date]:
    return {start + timedelta(days=offset) for offset in range((end - start).days + 1)}


def _metadata_dates(content: dict[str, Any]) -> set[date]:
    metadata = content.get("metadata") or {}
    try:
        # Request coverage includes incomplete dates. A newer failed/partial
        # request must displace stale success over the same requested interval.
        if metadata.get("start", metadata.get("start_date")):
            start = date.fromisoformat(metadata.get("start", metadata.get("start_date", "")))
            end = date.fromisoformat(metadata.get("end", metadata.get("end_date", "")))
            if not 0 <= (end - start).days <= 1100:
                return set()
            return _date_range(start, end)
        dates = {date.fromisoformat(value) for value in metadata.get("complete_dates", [])}
        return dates if len(dates) <= 1100 else set()
    except (ValueError, TypeError, AttributeError):
        return set()


def _window(
    session: Session,
    site: m.Site,
    pages: list[m.Page],
    start: date,
    end: date,
    evidence: list[m.Evidence],
) -> tuple[Any, dict[str, Any]]:
    rows = list(session.scalars(select(m.GA4Daily).where(
        m.GA4Daily.site_id == site.id, m.GA4Daily.page_id.in_([page.id for page in pages]),
        m.GA4Daily.channel == "Organic Search", m.GA4Daily.date >= start, m.GA4Daily.date <= end,
    ).order_by(m.GA4Daily.date, m.GA4Daily.page_id)))
    expected = _date_range(start, end)
    by_date: dict[date, m.Evidence] = {}
    # Most recent request metadata takes precedence, including newly incomplete
    # extraction. Old successful imports cannot silently override an outage.
    for item in evidence:
        metadata = item.content.get("metadata") or {}
        declared_pages = metadata.get("page_ids", [])
        if metadata.get("scope") != "all_organic_landing_pages" and not {page.id for page in pages} <= set(declared_pages):
            continue
        for observed_date in _metadata_dates(item.content) & expected:
            by_date.setdefault(observed_date, item)
    used = {item.id: item for item in by_date.values()}
    def day_complete(day: date, item: m.Evidence) -> bool:
        metadata = item.content.get("metadata") or {}
        if "complete_dates" in metadata:
            return day.isoformat() in metadata["complete_dates"]
        return item.content.get("complete") is True
    complete_dates = [day for day, item in by_date.items() if day_complete(day, item)]
    covered = expected <= set(by_date)
    tracking_verified = covered and all(item.content.get("metadata", {}).get("tracking_verified") is True for item in used.values())
    definition_matches = covered and all(item.content.get("metadata", {}).get("conversion_definition_hash") == stable_hash(site.conversion_definition)
                                         for item in used.values())
    qualification_verified = (site.conversion_definition.get("verified") is True and covered and definition_matches
        and all(item.content.get("metadata", {}).get("qualified_conversion_semantics_verified") is True for item in used.values()))
    value_verified = (qualification_verified and bool(site.conversion_definition.get("value_method"))
        and all(item.content.get("metadata", {}).get("qualified_conversion_value_semantics_verified") is True for item in used.values()))
    quality_flags = {flag for item in used.values() for flag in item.content.get("quality_flags", [])}
    if covered and not definition_matches:
        quality_flags.add("conversion_definition_changed")
    # Date-level coverage cannot certify a page that disappeared from a newer
    # report. Daily rows are mutable upserts; bind them to the selected immutable
    # snapshot so retained/mismatched values cannot masquerade as a fresh result.
    # Older explicitly scoped operator imports may lack a rows payload; their
    # existing coverage contract is preserved, but collector batches always have
    # a rows payload (including the meaningful empty list).
    snapshots: dict[str, dict[tuple[str, str, str], dict[str, Any] | None]] = {}
    for item in used.values():
        if "rows" not in item.content:
            continue
        indexed: dict[tuple[str, str, str], dict[str, Any] | None] = {}
        raw_rows = item.content["rows"]
        if not isinstance(raw_rows, list):
            quality_flags.add("api_disagreement")
        else:
            for raw in raw_rows:
                if (not isinstance(raw, dict)
                    or any(not isinstance(raw.get(key), str) for key in ("date", "landing_page", "channel"))):
                    quality_flags.add("api_disagreement")
                    continue
                key = (raw["date"], raw["landing_page"], raw["channel"])
                if key in indexed:
                    indexed[key] = None
                    quality_flags.add("api_disagreement")
                else:
                    indexed[key] = raw
        snapshots[item.id] = indexed
    current_rows = []
    for row in rows:
        item = by_date.get(row.date)
        if item is not None and item.id in snapshots:
            snapshot = snapshots[item.id].get((row.date.isoformat(), row.landing_page, row.channel))
            if snapshot is None:
                quality_flags.add("stale_page_observation_excluded")
                continue
            if any(snapshot.get(key) != getattr(row, key)
                   for key in ("sessions", "key_events", "qualified_conversions", "conversion_value")):
                quality_flags.add("api_disagreement")
                continue
        current_rows.append(row)
    rows = current_rows
    observed_pairs = {(row.page_id, row.date) for row in rows}
    complete_dates = [day for day in complete_dates if all((page.id, day) in observed_pairs for page in pages)]
    if any((page.id, day) not in observed_pairs for page in pages for day in expected):
        quality_flags.add("missing_page_date_observations")
    fixture_site = site.config_json.get("source_mode") == "fixture"
    if any(row.is_fixture != fixture_site for row in rows) or any(item.is_fixture != fixture_site for item in used.values()):
        quality_flags.add("api_disagreement")
        tracking_verified = False
    if fixture_site:
        quality_flags.add("fixture_outcomes_cannot_earn_production_autonomy")
    records = [GA4Row(date=row.date, landing_page=row.landing_page, sessions=row.sessions, key_events=row.key_events,
                      qualified_conversions=row.qualified_conversions, conversion_value=row.conversion_value,
                      channel=row.channel, quality_flags=sorted(set(row.quality_flags_json) | quality_flags)) for row in rows]
    window = observation_window(records, start, end, complete_dates=complete_dates, tracking_complete=tracking_verified,
                                conversion_value_mapping_verified=value_verified,
                                qualified_conversion_mapping_verified=qualification_verified)
    if "missing_page_date_observations" in quality_flags:
        # A sum of the observed subset is not the primary outcome for the whole
        # prespecified group/window. In particular, missing pages are not zeros.
        window.qualified_conversions = None
        window.qualified_conversion_value = None
    window.quality_flags = sorted(set(window.quality_flags) | quality_flags)
    window.partial_gsc = session.scalar(select(m.GSCDaily.id).where(
        m.GSCDaily.site_id == site.id, m.GSCDaily.date >= start, m.GSCDaily.date <= end,
        m.GSCDaily.data_state != "final",
    ).limit(1)) is not None
    provenance = {
        "page_ids": [page.id for page in pages], "start": start.isoformat(), "end": end.isoformat(),
        "evidence_ids": sorted(used), "complete_dates": sorted(day.isoformat() for day in complete_dates),
        "row_count": len(rows), "row_hash": stable_hash([row.model_dump(mode="json") for row in records]),
        "tracking_verified": tracking_verified, "qualification_verified": qualification_verified, "value_verified": value_verified,
        "conversion_definition_matches": definition_matches,
    }
    return window, provenance


def _controls(session: Session, site_id: str, experiment: m.Experiment) -> list[m.Page]:
    controls = []
    if len(experiment.control_pages_json or []) > 20:
        raise ValueError("An experiment may contain at most 20 prespecified reference pages")
    for identifier in experiment.control_pages_json or []:
        if not isinstance(identifier, str):
            raise ValueError("Reference pages must be canonical page IDs or exact registered URLs")
        page = session.scalar(select(m.Page).where(m.Page.site_id == site_id, (m.Page.id == identifier) | (m.Page.url == identifier)))
        if page is None or page.id == experiment.page_id or any(existing.id == page.id for existing in controls):
            raise ValueError("Reference pages must be distinct, untreated pages in this same site")
        controls.append(page)
    return controls


def evaluate_recorded_experiment(session: Session, experiment: m.Experiment, day: int) -> dict[str, Any]:
    """Return a serialisable result without persisting it; all reads are site-scoped."""
    site = session.get(m.Site, experiment.site_id)
    if site is None:
        raise LookupError("Experiment site not found")
    prediction = _prediction_summary(session, site, experiment)
    base = {"experiment_id": experiment.id, "checkpoint_day": day, "primary_outcome": experiment.primary_outcome,
            "verdict": "inconclusive", "reasons": [], "causal_effect_identified": False,
            "calibration_eligible": False, "automatic_rollback_authorised": False,
            "engine_version": ENGINE_VERSION, "evidence_ids": [], "sample_size": None, "actual_effect": None,
            "prediction": prediction}
    try:
        if site.conversion_definition.get("verified") is not True:
            raise ValueError("The business-qualified conversion definition has not been verified")
        if not experiment.page_id or not experiment.baseline_start or not experiment.baseline_end or not experiment.deployed_at:
            raise ValueError("A page, deployment timestamp and prespecified baseline are required")
        page = session.scalar(select(m.Page).where(m.Page.site_id == site.id, m.Page.id == experiment.page_id))
        if page is None:
            raise ValueError("The treated page is missing from this site")
        baseline_days = (experiment.baseline_end - experiment.baseline_start).days + 1
        if not 1 <= baseline_days <= 366 or not 1 <= day <= 366:
            raise ValueError("Measurement windows must be between 1 and 366 days")
        if experiment.baseline_end >= experiment.deployed_at.date():
            raise ValueError("Baseline must exclude the deployment day and all subsequent dates")
        config = (experiment.analysis_json or {}).get("measurement_config", {})
        allowed = {"minimum_qualified_conversions", "minimum_sessions", "minimum_days", "material_change_fraction",
                   "primary_checkpoint", "require_reference_group"}
        if set(config) - allowed:
            raise ValueError("Unsupported experiment measurement configuration")
        spec = ExperimentSpec(experiment_id=experiment.id,
            agent_id=prediction["agent_id"], action_category=prediction["action_category"],
            hypothesis=experiment.hypothesis, mechanism=experiment.mechanism, primary_outcome=experiment.primary_outcome,
            predicted_relative_effect=prediction["predicted_effect"],
            predicted_confidence=prediction["probability_of_success"],
            evaluation_checkpoints=experiment.evaluation_windows_json, **config)
        controls = _controls(session, site.id, experiment)
        evidence = list(session.scalars(select(m.Evidence).where(m.Evidence.site_id == site.id, m.Evidence.source_type == "ga4",
                        m.Evidence.status == "active").order_by(m.Evidence.observed_at.desc(), m.Evidence.id.desc())))
        # Keep the two windows equally long. Later checkpoints inspect the most
        # recent matched interval, excluding deployment day; the strategy is
        # recorded so checkpoint 56 cannot masquerade as 56 independent days.
        length = min(day, baseline_days)
        base_start = experiment.baseline_end - timedelta(days=length - 1)
        post_end = experiment.deployed_at.date() + timedelta(days=day)
        post_start = post_end - timedelta(days=length - 1)
        baseline, before_source = _window(session, site, [page], base_start, experiment.baseline_end, evidence)
        treatment, after_source = _window(session, site, [page], post_start, post_end, evidence)
        control_before = control_after = None
        provenance = {"baseline": before_source, "treatment": after_source}
        if controls:
            control_before, provenance["control_baseline"] = _window(session, site, controls, base_start, experiment.baseline_end, evidence)
            control_after, provenance["control_treatment"] = _window(session, site, controls, post_start, post_end, evidence)
        evaluated = evaluate_experiment(spec, baseline, treatment, control_baseline=control_before, control_treatment=control_after,
                                        checkpoint_day=day)
        base.update(evaluated.model_dump(mode="json"))
        base.update({"sample_size": treatment.sessions, "actual_effect": evaluated.descriptive_effect_fraction,
                     "provenance": provenance, "measurement_window_strategy": "trailing_matched_prespecified_baseline",
                     "window_days": length, "evidence_ids": sorted({identifier for record in provenance.values() for identifier in record["evidence_ids"]}),
                     "specification": spec.model_dump(mode="json"),
                     "windows": {"baseline": baseline.model_dump(mode="json"), "treatment": treatment.model_dump(mode="json"),
                                 "control_baseline": control_before.model_dump(mode="json") if control_before else None,
                                 "control_treatment": control_after.model_dump(mode="json") if control_after else None}})
    except (ValueError, TypeError) as error:
        base["reasons"] = [str(error)]
        base["recommended_action"] = "REVIEW_MEASUREMENT"
    base["input_hash"] = stable_hash({"version": ENGINE_VERSION, "result": base,
                                     "conversion_definition": site.conversion_definition,
                                     "baseline_start": experiment.baseline_start, "baseline_end": experiment.baseline_end,
                                     "control_pages": experiment.control_pages_json})
    return base


def _audit(session: Session, site_id: str, experiment_id: str, kind: str, key: str, payload: dict[str, Any]) -> m.Action:
    action = m.Action(site_id=site_id, experiment_id=experiment_id, kind=kind, risk="LOW", actor="measurement-engine",
                      reason="Persist an evidence-bound experiment decision without production writes", idempotency_key=key,
                      payload_json=payload)
    session.add(action)
    session.flush()
    session.add(m.ActionEvent(site_id=site_id, action_id=action.id, event_type="recorded",
                              details_json={"scope": "canonical_measurement", "production_write": False}))
    return action


def _executed_action(session: Session, experiment: m.Experiment) -> m.Action | None:
    return session.scalar(select(m.Action).join(m.ActionEvent, m.ActionEvent.action_id == m.Action.id).where(
        m.Action.site_id == experiment.site_id, m.Action.experiment_id == experiment.id,
        m.ActionEvent.site_id == experiment.site_id, m.ActionEvent.event_type == "succeeded",
    ).order_by(m.Action.created_at.desc()).limit(1))


def _record_regression(session: Session, experiment: m.Experiment, action: m.Action, result: dict[str, Any]) -> None:
    if result.get("verdict") != "regression_signal":
        return
    executed = _executed_action(session, experiment)
    session.add(m.FailureCase(site_id=experiment.site_id, action_id=executed.id if executed else None,
        category="experiment_regression_signal", predicted=experiment.hypothesis,
        actual="Qualified organic outcome declined in the descriptive comparison; intervention causality is unestablished",
        magnitude=result.get("actual_effect"), root_cause="unknown; independent causal review required",
        incorrect_assumption="Not established", missing_evidence="Causal attribution and appropriate reference diagnostics",
        agent_responsible=result.get("prediction", {}).get("agent_id", "unknown"),
        detection_method=f"{ENGINE_VERSION}: checkpoint {result['checkpoint_day']}",
        preventative_change="Review conversion intent, measurement stability and concurrent changes before repeating this intervention",
        details_json={"experiment_id": experiment.id, "measurement_action_id": action.id,
                      "input_hash": result["input_hash"], "harmful_action_confirmed": False,
                      "alternative_explanations": result.get("uncertainty", [])}))


def _primary_adjudication(session: Session, site: m.Site, experiment: m.Experiment) -> tuple[dict[str, Any], m.Evidence] | None:
    """Only immutable, current measurement snapshots can be adjudicated."""
    analysis = experiment.analysis_json or {}
    identifier = analysis.get("adjudication_evidence_id")
    if not isinstance(identifier, str):
        return None
    primary_day = analysis.get("measurement_config", {}).get("primary_checkpoint", 28)
    metric = session.scalar(select(m.ExperimentMetric).where(
        m.ExperimentMetric.site_id == site.id, m.ExperimentMetric.experiment_id == experiment.id,
        m.ExperimentMetric.metric == experiment.primary_outcome, m.ExperimentMetric.checkpoint_days == primary_day,
    ).order_by(m.ExperimentMetric.created_at.desc(), m.ExperimentMetric.id.desc()).limit(1))
    if metric is None or metric.analysis_json.get("verdict") == "inconclusive":
        return None
    primary = metric.analysis_json
    audit = session.get(m.Action, primary.get("measurement_action_id"))
    if (audit is None or audit.site_id != site.id or audit.experiment_id != experiment.id
        or audit.actor != "measurement-engine" or audit.kind != "record_experiment_evaluation"
        or audit.payload_json.get("input_hash") != primary.get("input_hash")
        or evaluate_recorded_experiment(session, experiment, primary_day)["input_hash"] != primary.get("input_hash")):
        return None
    evidence = session.scalar(select(m.Evidence).where(m.Evidence.site_id == site.id, m.Evidence.id == identifier,
        m.Evidence.source_type == "experiment_adjudication", m.Evidence.status == "active"))
    if (evidence is None or evidence.owner not in site.config_json.get("trusted_outcome_adjudicators", ["human-reviewer"])
        or evidence.content_hash != stable_hash(evidence.content) or evidence.observed_at < metric.created_at):
        return None
    forecast, _ = _prediction_provenance(session, site, experiment)
    forecaster = forecast.content["agent_id"] if forecast else analysis.get("agent_id")
    packet = evidence.content
    if (evidence.owner == forecaster or packet.get("independent") is not True
        or packet.get("experiment_id") != experiment.id or packet.get("primary_outcome") != experiment.primary_outcome
        or packet.get("checkpoint_day") != primary_day or not isinstance(packet.get("succeeded"), bool)
        or packet.get("measurement_snapshot_hash") != primary.get("input_hash")):
        return None
    return primary, evidence


def _propose_adjudicated_rollback(session: Session, site: m.Site, experiment: m.Experiment,
                                  primary: dict[str, Any], evidence: m.Evidence) -> None:
    """An optional missing forecast does not erase independent evidence of harm."""
    packet = evidence.content
    causal = packet.get("causal_confidence")
    fixture = site.config_json.get("source_mode") == "fixture" or evidence.is_fixture
    if (packet["succeeded"] or primary.get("verdict") != "regression_signal" or fixture
        or not isinstance(causal, (int, float)) or isinstance(causal, bool) or not 0.8 <= causal <= 1
        or packet.get("rollback_safe_to_propose") is not True):
        return
    executed = _executed_action(session, experiment)
    if executed is None or executed.payload_json.get("provider_is_fixture") is True:
        return
    prior = session.scalar(select(m.RollbackEvent.id).where(m.RollbackEvent.site_id == site.id,
        m.RollbackEvent.action_id == executed.id, m.RollbackEvent.actor == "measurement-engine",
        m.RollbackEvent.status == "proposed").limit(1))
    version = session.scalar(select(m.PageVersion).where(m.PageVersion.site_id == site.id,
        m.PageVersion.action_id == executed.id).order_by(m.PageVersion.version_number).limit(1))
    if prior or version is None:
        return
    audit = _audit(session, site.id, experiment.id, "record_rollback_proposal",
                   f"rollback-proposal:{executed.id}:{evidence.id}",
                   {"action_id": executed.id, "adjudication_evidence_id": evidence.id,
                    "primary_snapshot_hash": primary["input_hash"], "automatic_execution": False})
    session.add(m.RollbackEvent(site_id=site.id, action_id=executed.id, page_version_id=version.id,
        reason="Independently reviewed regression: propose review of the preserved pre-action revision",
        actor="measurement-engine", status="proposed", details_json={"experiment_id": experiment.id,
            "adjudication_evidence_id": evidence.id, "causal_confidence": causal,
            "measurement_action_id": audit.id, "requires_executor_approval": True, "automatic_execution": False}))


def _adjudicate_primary(session: Session, site: m.Site, experiment: m.Experiment) -> dict[str, Any] | None:
    """One terminal outcome binds a prespecified probability and reviewed data.

    Retrospective/ambiguous confidence never earns calibration credit. Evidence
    owner labels alone cannot supply forecast provenance or measurement history.
    """
    existing = session.scalar(select(m.CalibrationRecord).where(m.CalibrationRecord.site_id == site.id,
        m.CalibrationRecord.experiment_id == experiment.id))
    if existing:
        return None
    validated = _primary_adjudication(session, site, experiment)
    if validated is None:
        return None
    primary, evidence = validated
    _propose_adjudicated_rollback(session, site, experiment, primary, evidence)
    forecast, reasons = _prediction_provenance(session, site, experiment)
    if forecast is None or reasons:
        return None
    packet = evidence.content
    if (packet.get("prediction_evidence_id") != forecast.id or packet.get("prediction_hash") != forecast.content_hash
        or primary.get("prediction", {}).get("evidence_id") != forecast.id
        or primary.get("prediction", {}).get("content_hash") != forecast.content_hash):
        return None
    probability = forecast.content["probability_of_success"]
    primary_day = primary["checkpoint_day"]
    action = _audit(session, site.id, experiment.id, "record_prediction_outcome", f"calibration:{experiment.id}",
                    {"adjudication_evidence_id": evidence.id, "primary_snapshot_hash": primary["input_hash"],
                     "prediction_evidence_id": forecast.id, "prediction_hash": forecast.content_hash})
    succeeded = packet["succeeded"]
    fixture = site.config_json.get("source_mode") == "fixture" or evidence.is_fixture or forecast.is_fixture
    record = m.CalibrationRecord(site_id=site.id, experiment_id=experiment.id,
        agent_name=forecast.content["agent_id"], action_category=forecast.content["action_category"],
        predicted_confidence=probability, succeeded=succeeded if not fixture else None,
        brier_score=(probability - int(succeeded)) ** 2 if not fixture else None,
        evaluable=not fixture, exclusion_reason="Fixture outcomes cannot earn live autonomy" if fixture else None,
        outcome_json={"adjudication_evidence_id": evidence.id, "adjudication_source": evidence.source,
                      "primary_snapshot_hash": primary["input_hash"], "measurement_action_id": action.id,
                      "prediction_evidence_id": forecast.id, "prediction_hash": forecast.content_hash,
                      "confidence_semantics": PREDICTION_SEMANTICS, "success_criterion": forecast.content["success_criterion"],
                      "primary_outcome": experiment.primary_outcome, "checkpoint_day": primary_day,
                      "independent": True, "is_primary_outcome": True})
    session.add(record)
    session.flush()
    return {"experiment_id": experiment.id, "calibration_id": record.id, "evaluable": record.evaluable,
            "succeeded": record.succeeded, "automatic_graduation": False}


def evaluate_due_experiments(
    session: Session,
    site_id: str,
    *,
    now: datetime | None = None,
    authority_updates_allowed: bool = True,
) -> dict[str, Any]:
    """Persist all due checkpoints and later evidence corrections, idempotently.

    This service owns a short database transaction and commits its audit trail.
    It does not perform network calls, CMS writes, privilege increases, or
    automatic rollback. A scheduler caller cannot modify canonical authority;
    it records any autonomy-reduction signal for API/operator review.
    """
    now = now or utcnow()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Measurement clock must be timezone-aware")
    site = session.scalar(select(m.Site).where(m.Site.id == site_id).with_for_update().execution_options(populate_existing=True))
    if site is None:
        raise LookupError("Site not found")
    output: dict[str, Any] = {"checkpoints": [], "calibration_records": [], "calibration_exclusions": [], "automatic_graduation": False,
                              "production_mutations": 0, "rollback_execution": False}
    experiments = list(session.scalars(select(m.Experiment).where(m.Experiment.site_id == site_id,
        m.Experiment.status.in_(["running", "awaiting_evidence", "awaiting_adjudication"])).order_by(m.Experiment.id)))
    for experiment in experiments:
        if not experiment.deployed_at:
            continue
        elapsed = (now.astimezone(timezone.utc).date() - experiment.deployed_at.date()).days
        configured_days = experiment.evaluation_windows_json or []
        if any(not isinstance(day, int) or isinstance(day, bool) or day <= 0 or day > 366 for day in configured_days):
            raise ValueError("Experiment checkpoint days must be integers between 1 and 366")
        # Wait until the entire checkpoint day has finished; today's data are
        # not called complete just because the clock reached midnight.
        due = sorted({day for day in configured_days if day < elapsed})
        for day in due:
            result = evaluate_recorded_experiment(session, experiment, day)
            key = f"measurement:{experiment.id}:{day}:{result['input_hash']}"
            if session.scalar(select(m.Action.id).where(m.Action.site_id == site_id, m.Action.idempotency_key == key)):
                continue
            action = _audit(session, site_id, experiment.id, "record_experiment_evaluation", key,
                            {"checkpoint_day": day, "input_hash": result["input_hash"], "verdict": result["verdict"]})
            metric = m.ExperimentMetric(site_id=site_id, experiment_id=experiment.id, checkpoint_days=day,
                metric=experiment.primary_outcome, observed_value=result.get("actual_effect"), sample_size=result.get("sample_size"),
                analysis_json={**result, "measurement_action_id": action.id}, observed_at=now)
            session.add(metric)
            session.add(m.DecisionLog(site_id=site_id, action_id=action.id, decision=f"Experiment {experiment.id}, day {day}: {result['verdict']}",
                rationale="; ".join(result.get("reasons", [])), owner="measurement-engine", evidence_ids_json=result.get("evidence_ids", []),
                alternatives_json=result.get("uncertainty", []), uncertainty_json=["Association is not a causal effect", "No production write authorised"],
                regret_json={"wrong_action_cost": "Unnecessary rollback could damage a useful page change",
                             "delayed_action_cost": "A real conversion regression may persist while evidence is reviewed"}))
            previous = experiment.analysis_json or {}
            checkpoints = {**previous.get("checkpoints", {}), str(day): result}
            experiment.analysis_json = {**previous, "checkpoints": checkpoints, "latest_evaluation": result}
            primary_day = previous.get("measurement_config", {}).get("primary_checkpoint", 28)
            if day == primary_day:
                experiment.verdict = result["verdict"]
                experiment.actual_effect = result.get("actual_effect")
            _record_regression(session, experiment, action, result)
            output["checkpoints"].append({**result, "measurement_action_id": action.id})
        calibrated = _adjudicate_primary(session, site, experiment)
        if calibrated:
            output["calibration_records"].append(calibrated)
        prediction = _prediction_summary(session, site, experiment)
        if prediction["exclusion_reasons"]:
            output["calibration_exclusions"].append({"experiment_id": experiment.id, **prediction})
    session.flush()
    calibration_rows = list(session.scalars(select(m.CalibrationRecord).where(m.CalibrationRecord.site_id == site_id)))
    output["calibration"] = calibration_report([CalibrationObservation(
        experiment_id=row.experiment_id or row.id, agent_id=row.agent_name, action_category=row.action_category,
        predicted_confidence=row.predicted_confidence, succeeded=row.succeeded,
        adjudicated=row.evaluable and row.outcome_json.get("independent") is True,
        is_primary_outcome=row.outcome_json.get("is_primary_outcome") is True,
        adjudication_source=row.outcome_json.get("adjudication_source"),
    ) for row in calibration_rows])
    output["autonomy_reduction_recommendations"] = [group for group in output["calibration"]["groups"] if group["autonomy_recommendation"] == "reduce"]
    earned = set(site.config_json.get("earned_categories", []))
    revoked = sorted(earned & {group["action_category"] for group in output["autonomy_reduction_recommendations"]})
    output["recommended_revocations"] = revoked
    output["revoked_categories"] = revoked if authority_updates_allowed else []
    if revoked and authority_updates_allowed:
        site.config_json = {**site.config_json, "earned_categories": sorted(earned - set(revoked))}
        audit = m.Action(site_id=site.id, kind="reduce_autonomy", risk="LOW", actor="calibration-monitor",
            reason="Independently adjudicated outcomes crossed the poor-calibration threshold",
            idempotency_key="calibration-reduction:" + stable_hash({"revoked": revoked, "report": output["calibration"]}),
            payload_json={"revoked_categories": revoked, "calibration": output["calibration"], "automatic_graduation": False})
        session.add(audit)
        session.flush()
        session.add(m.ActionEvent(site_id=site.id, action_id=audit.id, event_type="recorded",
                                  details_json={"scope": "canonical_policy", "production_write": False}))
        session.add(m.DecisionLog(site_id=site.id, action_id=audit.id, owner="calibration-monitor",
            decision="Require human approval for " + ", ".join(revoked),
            rationale="Remove previously earned categories; evidence cannot automatically increase authority",
            uncertainty_json=output["calibration"]["quality_flags"], alternatives_json=["Pause all automation"]))
    elif revoked:
        recommendation_key = "calibration-reduction-recommendation:" + stable_hash({
            "recommended": revoked, "report": output["calibration"],
        })
        audit = session.scalar(select(m.Action).where(
            m.Action.site_id == site.id,
            m.Action.idempotency_key == recommendation_key,
        ))
        if audit is None:
            audit = m.Action(site_id=site.id, kind="recommend_autonomy_reduction", risk="LOW",
                actor="calibration-monitor", reason="Worker cannot modify canonical site authority",
                idempotency_key=recommendation_key,
                payload_json={"recommended_revocations": revoked, "calibration": output["calibration"],
                              "authority_update": False, "requires_api_or_operator_review": True})
            session.add(audit)
            session.flush()
            session.add(m.ActionEvent(site_id=site.id, action_id=audit.id, event_type="recorded",
                                      details_json={"scope": "authority_recommendation", "production_write": False}))
            session.add(m.DecisionLog(site_id=site.id, action_id=audit.id, owner="calibration-monitor",
                decision="Recommend removing earned categories: " + ", ".join(revoked),
                rationale="The worker role is read-only on site authority; an API/operator decision is required",
                uncertainty_json=output["calibration"]["quality_flags"], alternatives_json=["Pause all automation"]))
    session.commit()
    return output
