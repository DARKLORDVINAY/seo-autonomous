"""Guarded descriptive experiment analysis, never automatic causal certification.

Aggregate before/after values do not identify incremental effects. Reference
groups permit descriptive difference-in-differences, but parallel trends,
spillovers and intervention selection still need independent review. Sample
floors below are operational heuristics, not a power analysis or significance
test. Repeated checkpoints never become independent calibration outcomes.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import Field, model_validator

from backend.app.contracts import GA4Row, StrictModel


class ExperimentSpec(StrictModel):
    experiment_id: str
    agent_id: str = "unknown"
    action_category: str = "unknown"
    hypothesis: str = "A useful page change increases qualified organic conversion value."
    mechanism: str = "Must be specified before execution"
    primary_outcome: Literal["qualified_organic_conversion_value", "qualified_organic_conversions"] = "qualified_organic_conversion_value"
    predicted_relative_effect: float | None = None
    predicted_confidence: float | None = Field(default=None, ge=0, le=1)
    minimum_qualified_conversions: float = Field(default=30, ge=1)
    minimum_sessions: int = Field(default=100, ge=1)
    minimum_days: int = Field(default=14, ge=7)
    material_change_fraction: float = Field(default=0.1, gt=0, lt=1)
    evaluation_checkpoints: list[int] = Field(default_factory=lambda: [7, 14, 28, 56])
    primary_checkpoint: int = Field(default=28, ge=1)
    require_reference_group: bool = False

    @model_validator(mode="after")
    def validate_checkpoints(self) -> "ExperimentSpec":
        if not self.evaluation_checkpoints or any(day <= 0 for day in self.evaluation_checkpoints):
            raise ValueError("Checkpoints must be positive day counts")
        if len(self.evaluation_checkpoints) != len(set(self.evaluation_checkpoints)):
            raise ValueError("Checkpoint days cannot be duplicated")
        if self.primary_checkpoint not in self.evaluation_checkpoints:
            raise ValueError("Prespecified primary checkpoint must be in evaluation checkpoints")
        if self.predicted_relative_effect is not None and not math.isfinite(self.predicted_relative_effect):
            raise ValueError("Predicted effect must be finite")
        return self


class ObservationWindow(StrictModel):
    start_date: date
    end_date: date
    sessions: int | None = Field(default=None, ge=0)
    qualified_conversions: float | None = Field(default=None, ge=0)
    qualified_conversion_value: float | None = Field(default=None, ge=0)
    tracking_complete: bool = False
    collection_complete: bool = False
    partial_gsc: bool = False
    quality_flags: list[str] = Field(default_factory=list)

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    @model_validator(mode="after")
    def validate_window(self) -> "ObservationWindow":
        if self.start_date > self.end_date:
            raise ValueError("Observation window ends before it starts")
        for value in (self.qualified_conversions, self.qualified_conversion_value):
            if value is not None and not math.isfinite(value):
                raise ValueError("Outcome values must be finite")
        return self


class ExperimentEvaluation(StrictModel):
    experiment_id: str
    checkpoint_day: int
    primary_outcome: str
    verdict: Literal["inconclusive", "benefit_signal", "regression_signal", "no_material_change"]
    recommended_action: Literal["NO-ACTION", "COLLECT_MORE_DATA", "PROPOSE_ROLLBACK", "REVIEW_MEASUREMENT", "REVIEW_CAUSAL_EVIDENCE"]
    primary_before: float | None = None
    primary_after: float | None = None
    absolute_change: float | None = None
    relative_change: float | None = None
    treated_change_per_day: float | None = None
    control_change_per_day: float | None = None
    difference_in_differences_per_day: float | None = None
    descriptive_effect_fraction: float | None = None
    organic_sessions_change: float | None = None
    reasons: list[str] = Field(default_factory=list)
    uncertainty: list[str] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)
    causal_effect_identified: bool = False
    causal_confidence: float | None = None
    confidence_interval: list[float] | None = None
    requires_human_review: bool = True
    calibration_eligible: bool = False
    repeated_checkpoints_are_independent: bool = False
    automatic_rollback_authorised: bool = False


def observation_window(
    rows: list[GA4Row],
    start_date: date,
    end_date: date,
    *,
    complete_dates: list[date] | None = None,
    tracking_complete: bool = False,
    conversion_value_mapping_verified: bool = False,
    qualified_conversion_mapping_verified: bool = False,
    landing_pages: list[str] | None = None,
) -> ObservationWindow:
    """Build from verified extraction metadata; absence alone is never a zero.

    An empty successfully completed and mapped query can report zero. A missing
    provider response cannot certify complete_dates and returns unknown instead.
    """
    if start_date > end_date:
        raise ValueError("Observation window ends before it starts")
    selected = [row for row in rows if start_date <= row.date <= end_date and row.channel == "Organic Search"
                and (landing_pages is None or row.landing_page in landing_pages)]
    expected_dates = {start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)}
    complete = expected_dates <= set(complete_dates or [])
    can_sum = bool(selected) or complete
    flags = sorted({flag for row in selected for flag in row.quality_flags})
    if not qualified_conversion_mapping_verified:
        flags.append("qualified_conversion_mapping_unverified")
    if not conversion_value_mapping_verified:
        flags.append("conversion_value_mapping_unverified")
    return ObservationWindow(
        start_date=start_date, end_date=end_date,
        sessions=sum(row.sessions for row in selected) if can_sum else None,
        qualified_conversions=(sum(row.qualified_conversions for row in selected if row.qualified_conversions is not None)
            if can_sum and qualified_conversion_mapping_verified and all(row.qualified_conversions is not None for row in selected) else None),
        qualified_conversion_value=(sum(row.conversion_value for row in selected if row.conversion_value is not None)
            if can_sum and conversion_value_mapping_verified and all(row.conversion_value is not None for row in selected) else None),
        tracking_complete=tracking_complete, collection_complete=complete, quality_flags=flags,
    )


def _primary(window: ObservationWindow, spec: ExperimentSpec) -> float | None:
    return window.qualified_conversion_value if spec.primary_outcome == "qualified_organic_conversion_value" else window.qualified_conversions


def evaluate_experiment(
    spec: ExperimentSpec,
    baseline: ObservationWindow,
    treatment: ObservationWindow,
    *,
    control_baseline: ObservationWindow | None = None,
    control_treatment: ObservationWindow | None = None,
    checkpoint_day: int = 28,
    previous_primary_verdict: str | None = None,
) -> ExperimentEvaluation:
    """Evaluate evidence; propose review, never grant execution/rollback authority.

    Positive or negative signals are associations. Calibration requires a later
    independently adjudicated primary outcome, not this function's own verdict.
    """
    before, after = _primary(baseline, spec), _primary(treatment, spec)
    result = ExperimentEvaluation(
        experiment_id=spec.experiment_id, checkpoint_day=checkpoint_day, primary_outcome=spec.primary_outcome,
        verdict="inconclusive", recommended_action="COLLECT_MORE_DATA", primary_before=before, primary_after=after,
        uncertainty=["Aggregate observations do not identify a causal effect or a defensible uncertainty interval.",
                     "Operational sample floors do not establish statistical power.",
                     "Selection, concurrent changes, attribution and measurement can affect comparisons."],
    )
    flags = set(baseline.quality_flags + treatment.quality_flags)
    if spec.primary_outcome == "qualified_organic_conversions":
        flags.add("business_selected_conversion_count_proxy_value_unknown")
    if baseline.partial_gsc or treatment.partial_gsc:
        flags.add("partial_gsc_visibility_diagnostics_incomplete")
        result.uncertainty.append("Partial GSC data limits search explanations; complete independent GA4 outcomes may still be described.")
    if baseline.sessions is not None and treatment.sessions is not None and baseline.sessions > 0:
        result.organic_sessions_change = treatment.sessions / baseline.sessions - 1
    if before is not None and after is not None:
        result.absolute_change = after - before
        result.relative_change = after / before - 1 if before > 0 else None
        result.treated_change_per_day = after / treatment.days - before / baseline.days
        if after < before and result.organic_sessions_change is not None and result.organic_sessions_change > 0:
            flags.add("goodhart_traffic_gain_with_primary_outcome_decline")
            result.reasons.append("Traffic increased while the business outcome declined; traffic cannot establish success.")
    blockers = []
    measurement_blocked = False
    if not baseline.tracking_complete or not treatment.tracking_complete:
        blockers.append("Tracking completeness is unverified or an outage occurred.")
        flags.add("tracking_incomplete")
        measurement_blocked = True
    if not baseline.collection_complete or not treatment.collection_complete:
        blockers.append("Outcome extraction is incomplete; absent observations are not zero.")
        flags.add("collection_incomplete")
        measurement_blocked = True
    measurement_flags = {"tracking_outage", "tracking_error", "api_disagreement", "thresholding", "sampled_report",
                         "conversion_definition_changed", "attribution_changed", "aggregated_other_rows", "row_budget_reached",
                         "qualified_outcome_missing", "data_not_final", "report_timezone_unconfirmed", "timezone_disagreement"}
    if flags & measurement_flags:
        blockers.append("Measurement quality flags prevent outcome adjudication.")
        measurement_blocked = True
    if before is None or after is None:
        blockers.append("Primary qualified conversion outcome is unknown, not zero.")
        flags.add("primary_outcome_missing")
        measurement_blocked = True
    if baseline.end_date >= treatment.start_date:
        blockers.append("Baseline and treatment windows overlap or are out of order.")
        flags.add("overlapping_windows")
    if baseline.days != treatment.days or baseline.days % 7:
        blockers.append("Windows do not have equal duration and comparable weekday composition.")
        flags.add("non_comparable_windows")
    if min(baseline.days, treatment.days) < spec.minimum_days:
        blockers.append("Insufficient elapsed observation time.")
        flags.add("insufficient_time")
    if any(window.sessions is None or window.sessions < spec.minimum_sessions for window in (baseline, treatment)):
        blockers.append("The configured minimum session floor has not been reached.")
        flags.add("insufficient_sessions")
    if any(window.qualified_conversions is None or window.qualified_conversions < spec.minimum_qualified_conversions for window in (baseline, treatment)):
        blockers.append("Too few or unknown qualified conversion events for the operational review threshold.")
        flags.add("insufficient_qualified_conversions")
    if checkpoint_day != spec.primary_checkpoint:
        blockers.append("This is an exploratory checkpoint, not the prespecified primary endpoint.")
        flags.add("exploratory_checkpoint_not_independent_evidence")
    if checkpoint_day not in spec.evaluation_checkpoints:
        flags.add("unscheduled_checkpoint")
    if checkpoint_day < treatment.days:
        blockers.append("The treatment window extends beyond the stated checkpoint.")
        flags.add("checkpoint_window_mismatch")
    if previous_primary_verdict is not None:
        flags.add("primary_outcome_already_evaluated_do_not_double_count")
        result.reasons.append("This revision cannot be counted as another independent experiment outcome.")

    has_control = control_baseline is not None and control_treatment is not None
    if (control_baseline is None) != (control_treatment is None):
        blockers.append("Both reference windows are required for a control comparison.")
        flags.add("reference_pair_incomplete")
    if spec.require_reference_group and not has_control:
        blockers.append("The prespecified reference comparison is missing.")
        flags.add("reference_required")
    if "seasonality_suspected" in flags and not has_control:
        blockers.append("Seasonality is plausible and no contemporaneous reference group is available.")
        flags.add("seasonality_unresolved")
    if has_control:
        assert control_baseline is not None and control_treatment is not None
        control_before, control_after = _primary(control_baseline, spec), _primary(control_treatment, spec)
        control_flags = set(control_baseline.quality_flags + control_treatment.quality_flags)
        flags.update(control_flags)
        if (control_baseline.start_date, control_baseline.end_date, control_treatment.start_date, control_treatment.end_date) != (
            baseline.start_date, baseline.end_date, treatment.start_date, treatment.end_date
        ):
            blockers.append("Reference and treated windows are not contemporaneous.")
            flags.add("reference_dates_mismatch")
        elif (not all(window.tracking_complete and window.collection_complete for window in (control_baseline, control_treatment))
              or control_flags & (measurement_flags | {"contaminated_control", "concurrent_change"})):
            blockers.append("Reference data are incomplete, contaminated or affected by measurement problems.")
            flags.add("reference_invalid")
        elif control_before is None or control_after is None:
            blockers.append("Reference primary outcome is unavailable.")
            flags.add("reference_outcome_missing")
        elif any(window.qualified_conversions is None or window.qualified_conversions < spec.minimum_qualified_conversions
                 or window.sessions is None or window.sessions < spec.minimum_sessions for window in (control_baseline, control_treatment)):
            blockers.append("Reference group has too few observations for the configured review floor.")
            flags.add("reference_sample_too_small")
        else:
            result.control_change_per_day = control_after / control_treatment.days - control_before / control_baseline.days
            if result.treated_change_per_day is not None:
                result.difference_in_differences_per_day = result.treated_change_per_day - result.control_change_per_day
            result.uncertainty.append("Difference-in-differences is descriptive: parallel trends, comparability and absence of spillovers are unverified.")
            flags.add("parallel_trends_unverified")
    else:
        flags.add("no_contemporaneous_reference")
        result.uncertainty.append("No untreated contemporaneous reference group is available.")

    effect_per_day = result.difference_in_differences_per_day if result.difference_in_differences_per_day is not None else result.treated_change_per_day
    if effect_per_day is not None and before is not None and before > 0:
        result.descriptive_effect_fraction = effect_per_day / (before / baseline.days)
    elif before == 0:
        flags.add("zero_baseline_relative_effect_undefined")
        blockers.append("A zero baseline does not support a relative effect estimate.")

    if blockers:
        result.reasons.extend(blockers)
        result.recommended_action = "REVIEW_MEASUREMENT" if measurement_blocked else "COLLECT_MORE_DATA"
    elif result.descriptive_effect_fraction is not None:
        effect = result.descriptive_effect_fraction
        if effect <= -spec.material_change_fraction:
            result.verdict = "regression_signal"
            result.recommended_action = "REVIEW_CAUSAL_EVIDENCE"
            result.reasons.append("The primary outcome shows a material descriptive decline. Review cause, reversibility and secondary effects before proposing rollback.")
        elif effect >= spec.material_change_fraction:
            result.verdict = "benefit_signal"
            result.recommended_action = "REVIEW_CAUSAL_EVIDENCE"
            result.reasons.append("The primary outcome shows a material descriptive improvement; causality and statistical significance remain unestablished.")
        else:
            result.verdict = "no_material_change"
            result.recommended_action = "NO-ACTION"
            result.reasons.append("The descriptive difference is below the configured decision threshold; this is not evidence of equivalence.")
    result.quality_flags = sorted(flags)
    return result


class CalibrationObservation(StrictModel):
    experiment_id: str
    agent_id: str
    action_category: str
    predicted_confidence: float = Field(ge=0, le=1)
    succeeded: bool | None = None
    adjudicated: bool = False
    is_primary_outcome: bool = True
    adjudication_source: str | None = None


def calibration_report(
    observations: list[CalibrationObservation | dict[str, Any]],
    *,
    min_observations: int = 20,
    max_brier: float = 0.25,
    max_calibration_gap: float = 0.2,
) -> dict[str, Any]:
    """Deduplicate adjudicated primary outcomes; permit reductions, never promotion.

    Brier score combines discrimination and calibration. Report bins and sample
    counts alongside it. Missing/unresolved outcomes may introduce selection
    bias. Thresholds are operational policy inputs, not universal standards.
    """
    if min_observations < 1 or not 0 <= max_brier <= 1 or not 0 <= max_calibration_gap <= 1:
        raise ValueError("Invalid calibration policy thresholds")
    rows = [row if isinstance(row, CalibrationObservation) else CalibrationObservation.model_validate(row) for row in observations]
    selected: dict[tuple[str, str, str], CalibrationObservation] = {}
    rejected = 0
    duplicates = 0
    for row in rows:
        if not row.adjudicated or row.succeeded is None or not row.is_primary_outcome or not row.adjudication_source:
            rejected += 1
            continue
        key = (row.experiment_id, row.agent_id, row.action_category)
        if key in selected:
            if selected[key].predicted_confidence != row.predicted_confidence or selected[key].succeeded != row.succeeded:
                raise ValueError("Contradictory adjudications or changed predictions for one experiment require explicit reconciliation")
            duplicates += 1
            continue
        selected[key] = row
    groups: dict[tuple[str, str], list[CalibrationObservation]] = defaultdict(list)
    for row in selected.values():
        groups[(row.agent_id, row.action_category)].append(row)
    reports = []
    for (agent, category), group in sorted(groups.items()):
        brier = sum((row.predicted_confidence - int(bool(row.succeeded))) ** 2 for row in group) / len(group)
        bins = []
        for index in range(5):
            lower, upper = index / 5, (index + 1) / 5
            members = [row for row in group if lower <= row.predicted_confidence < upper or (index == 4 and row.predicted_confidence == 1)]
            bins.append({"lower": lower, "upper": upper, "upper_inclusive": index == 4, "count": len(members),
                         "mean_prediction": sum(row.predicted_confidence for row in members) / len(members) if members else None,
                         "observed_success_fraction": sum(bool(row.succeeded) for row in members) / len(members) if members else None})
        gap = sum(item["count"] * abs(item["mean_prediction"] - item["observed_success_fraction"])
                  for item in bins if item["count"]) / len(group)
        sufficient = len(group) >= min_observations
        reduce = sufficient and (brier > max_brier or gap > max_calibration_gap)
        reports.append({"agent_id": agent, "action_category": category, "observations": len(group), "brier_score": brier,
                        "weighted_calibration_gap": gap, "bins": bins, "sample_sufficient_for_policy": sufficient,
                        "autonomy_recommendation": "reduce" if reduce else "maintain", "automatic_graduation": False,
                        "reason": "Poor observed calibration" if reduce else ("Insufficient adjudicated observations" if not sufficient else "No reduction threshold crossed")})
    return {"groups": reports, "adjudicated_unique_primary_outcomes": len(selected), "excluded_unresolved_or_unverified": rejected,
            "duplicates_ignored": duplicates, "automatic_graduation": False,
            "quality_flags": ["unresolved_outcomes_may_create_selection_bias", "brier_is_not_pure_calibration", "thresholds_are_policy_choices"]}


class FailurePacket(StrictModel):
    """Persist this structured packet in canonical failure_cases, never prose alone."""
    experiment_id: str | None = None
    action_id: str | None = None
    what_was_predicted: str
    what_happened: str
    magnitude: float | None = None
    magnitude_unit: str | None = None
    root_cause: str = "unknown"
    root_cause_confidence: float = Field(default=0, ge=0, le=1)
    incorrect_assumptions: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    agent_responsible: str
    detection_method: str
    preventative_change: str
    guardrails_should_change: bool | None = None
    alternative_explanations: list[str] = Field(default_factory=list)
