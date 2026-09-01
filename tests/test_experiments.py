from datetime import date, timedelta

import pytest

from backend.app.experiments.evaluation import (
    CalibrationObservation,
    ExperimentSpec,
    FailurePacket,
    ObservationWindow,
    calibration_report,
    evaluate_experiment,
    observation_window,
)


START = date(2026, 6, 1)


def windows(before_value=1000, after_value=1200, before_sessions=1000, after_sessions=1000, before_count=100, after_count=120, **kwargs):
    baseline = ObservationWindow(start_date=START, end_date=START + timedelta(days=27), sessions=before_sessions,
                                 qualified_conversions=before_count, qualified_conversion_value=before_value,
                                 tracking_complete=True, collection_complete=True)
    treatment = ObservationWindow(start_date=START + timedelta(days=28), end_date=START + timedelta(days=55), sessions=after_sessions,
                                  qualified_conversions=after_count, qualified_conversion_value=after_value,
                                  tracking_complete=True, collection_complete=True, **kwargs)
    return baseline, treatment


def test_fake_traffic_success_is_rejected_when_business_value_declines():
    baseline, treatment = windows(after_sessions=10000, after_value=600, after_count=60)
    result = evaluate_experiment(ExperimentSpec(experiment_id="traffic-trap"), baseline, treatment)
    assert result.verdict == "regression_signal"
    assert "goodhart_traffic_gain_with_primary_outcome_decline" in result.quality_flags
    assert result.automatic_rollback_authorised is False
    assert result.causal_confidence is None


def test_shared_seasonality_is_not_attributed_to_the_change():
    before, after = windows(after_value=500, after_count=50, quality_flags=["seasonality_suspected"])
    control_before, control_after = windows(after_value=500, after_count=50)
    spec = ExperimentSpec(experiment_id="seasonal")
    without = evaluate_experiment(spec, before, after)
    with_control = evaluate_experiment(spec, before, after, control_baseline=control_before, control_treatment=control_after)
    assert without.verdict == "inconclusive"
    assert with_control.verdict == "no_material_change"
    assert with_control.difference_in_differences_per_day == 0
    assert with_control.causal_effect_identified is False
    assert "parallel_trends_unverified" in with_control.quality_flags


@pytest.mark.parametrize("flag", ["tracking_outage", "api_disagreement", "conversion_definition_changed", "sampled_report"])
def test_measurement_problem_overrides_impressive_effect(flag):
    before, after = windows(after_value=100000, after_count=1000, quality_flags=[flag])
    result = evaluate_experiment(ExperimentSpec(experiment_id=flag), before, after)
    assert result.verdict == "inconclusive"
    assert result.recommended_action == "REVIEW_MEASUREMENT"


def test_tiny_counts_never_establish_success():
    before, after = windows(before_count=1, after_count=3, before_value=10, after_value=30)
    result = evaluate_experiment(ExperimentSpec(experiment_id="tiny"), before, after)
    assert result.verdict == "inconclusive"
    assert "insufficient_qualified_conversions" in result.quality_flags


def test_missing_is_different_from_observed_zero():
    before, after = windows(after_value=None)
    missing = evaluate_experiment(ExperimentSpec(experiment_id="missing"), before, after)
    assert missing.primary_after is None and missing.relative_change is None
    assert missing.verdict == "inconclusive"
    zero = after.model_copy(update={"qualified_conversion_value": 0})
    observed_zero = evaluate_experiment(ExperimentSpec(experiment_id="zero"), before, zero)
    assert observed_zero.relative_change == -1
    assert observed_zero.verdict == "regression_signal"


def test_outage_does_not_become_a_zero_conversion_result():
    before, after = windows(after_value=0, after_count=0)
    after.tracking_complete = False
    result = evaluate_experiment(ExperimentSpec(experiment_id="outage"), before, after)
    assert result.verdict == "inconclusive"
    assert result.recommended_action == "REVIEW_MEASUREMENT"


def test_partial_gsc_is_preserved_without_erasing_independent_ga4_observations():
    before, after = windows(partial_gsc=True)
    result = evaluate_experiment(ExperimentSpec(experiment_id="partial-gsc"), before, after)
    assert "partial_gsc_visibility_diagnostics_incomplete" in result.quality_flags
    assert result.primary_after == 1200
    assert result.causal_effect_identified is False
    assert result.calibration_eligible is False


def test_incomplete_reference_or_different_dates_is_not_silent_uncontrolled_analysis():
    before, after = windows()
    cbefore, cafter = windows()
    cafter.start_date += timedelta(days=1)
    result = evaluate_experiment(ExperimentSpec(experiment_id="control-dates"), before, after, control_baseline=cbefore, control_treatment=cafter)
    assert result.verdict == "inconclusive"
    assert "reference_dates_mismatch" in result.quality_flags


def test_early_and_repeated_checks_do_not_count_as_new_evidence():
    before, after = windows()
    early = evaluate_experiment(ExperimentSpec(experiment_id="checkpoints"), before, after, checkpoint_day=7)
    assert early.verdict == "inconclusive"
    assert early.repeated_checkpoints_are_independent is False
    repeat = evaluate_experiment(ExperimentSpec(experiment_id="checkpoints"), before, after, previous_primary_verdict="benefit_signal")
    assert "primary_outcome_already_evaluated_do_not_double_count" in repeat.quality_flags
    assert repeat.calibration_eligible is False


def test_observation_builder_only_emits_zero_after_successful_complete_mapped_collection():
    end = START + timedelta(days=27)
    missing = observation_window([], START, end)
    assert missing.sessions is None
    assert missing.qualified_conversion_value is None
    complete_dates = [START + timedelta(days=offset) for offset in range(28)]
    zero = observation_window([], START, end, complete_dates=complete_dates, tracking_complete=True,
                              conversion_value_mapping_verified=True, qualified_conversion_mapping_verified=True)
    assert zero.sessions == zero.qualified_conversions == zero.qualified_conversion_value == 0
    assert zero.collection_complete is True


def test_calibration_counts_one_adjudicated_primary_outcome_per_experiment():
    row = CalibrationObservation(experiment_id="one", agent_id="analyst", action_category="title", predicted_confidence=0.8,
                                 succeeded=True, adjudicated=True, adjudication_source="review:12")
    unresolved = row.model_copy(update={"experiment_id": "two", "adjudicated": False})
    report = calibration_report([row, row, unresolved])
    assert report["adjudicated_unique_primary_outcomes"] == 1
    assert report["duplicates_ignored"] == 1
    assert report["excluded_unresolved_or_unverified"] == 1
    assert report["groups"][0]["brier_score"] == pytest.approx(0.04)
    assert report["groups"][0]["autonomy_recommendation"] == "maintain"
    assert report["automatic_graduation"] is False


def test_poor_calibration_has_operational_consequences_but_good_results_never_promote():
    rows = [CalibrationObservation(experiment_id=str(index), agent_id="overconfident", action_category="copy", predicted_confidence=0.95,
                                    succeeded=False, adjudicated=True, adjudication_source="review:verified") for index in range(20)]
    poor = calibration_report(rows)
    assert poor["groups"][0]["autonomy_recommendation"] == "reduce"
    good = calibration_report([row.model_copy(update={"succeeded": True}) for row in rows])
    assert good["groups"][0]["autonomy_recommendation"] == "maintain"
    assert good["automatic_graduation"] is False


def test_conflicting_calibration_adjudications_must_be_reconciled():
    row = CalibrationObservation(experiment_id="one", agent_id="analyst", action_category="title", predicted_confidence=0.8,
                                 succeeded=True, adjudicated=True, adjudication_source="review:12")
    with pytest.raises(ValueError, match="Contradictory"):
        calibration_report([row, row.model_copy(update={"succeeded": False})])


def test_failure_packet_preserves_unknown_root_cause_and_preventative_action():
    packet = FailurePacket(what_was_predicted="More qualified enquiries", what_happened="More traffic, fewer enquiries",
                           agent_responsible="opportunity", detection_method="GA4 qualified outcomes", preventative_change="Review conversion intent before title changes")
    assert packet.root_cause == "unknown"
    assert packet.root_cause_confidence == 0
    assert packet.guardrails_should_change is None

