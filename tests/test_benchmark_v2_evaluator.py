"""Explicit toy scoring tests; never import or reveal the held-out corpus."""
from copy import deepcopy

import pytest

from benchmarks.seo_v2.evaluator import evaluate


U = "https://example.test/a/"
V = "https://example.test/b/"
WRONG = "https://example.test/unrelated/"


def packet(candidates=None, *, decision="INVESTIGATE", coverage=True):
    return {"case_id": "toy", "candidates": candidates or [], "decision": decision, "coverage_complete": coverage}


def candidate(**changes):
    value = {"kind": "broken_internal_link", "page_url": U, "related_urls": [V],
             "disposition": "REVIEW", "evidence": [{"status_code": 404}], "quality_flags": []}
    value.update(changes)
    return value


def predictions(case=None, **meta):
    metadata = {"source_fingerprint": "a" * 64, "input_sha256": "b" * 64,
                "production_enabled": False, "autonomy_level": 1, "paid_api_calls": 0,
                "production_write_budget": 0}
    metadata.update(meta)
    return {"metadata": metadata, "cases": [case or packet([candidate()])]}


def truth(*, control=False, ambiguous=False):
    return {"cases": {"toy": {"case_id": "toy", "family": "explicit_toy", "stratum": "control" if control else "ambiguous" if ambiguous else "definite",
                              "expected_decisions": ["NO-ACTION"] if control else ["NEEDS_EVIDENCE", "INVESTIGATE"] if ambiguous else ["INVESTIGATE"],
                              "coverage_complete": not ambiguous,
                              "units": [] if control else [{"unit_id": "u1", "kind": "broken_internal_link", "page_urls": [U],
                                                            "related_urls": [V], "related_mode": "contains", "allowed_dispositions": ["REVIEW", "NEEDS_EVIDENCE"],
                                                            "epistemic_class": "needs_evidence" if ambiguous else "observation"}],
                              "protected_urls": [U] if control else []}}}


def test_correct_candidate_earns_one_true_positive_without_autonomy():
    result = evaluate(predictions(), truth())
    assert result["aggregate"]["true_positives"] == 1
    assert result["aggregate"]["precision"] == result["aggregate"]["recall"] == 1
    assert result["level_2_eligible"] is False
    assert result["production_enabled"] is False


@pytest.mark.parametrize("changes", [{"page_url": WRONG}, {"related_urls": [WRONG]}, {"related_urls": [V, WRONG]}, {"related_urls": []}, {"kind": "orphan_page"}])
def test_wrong_kind_source_or_destination_does_not_earn_credit(changes):
    result = evaluate(predictions(packet([candidate(**changes)])), truth())
    assert result["aggregate"]["false_positives"] == 1
    assert result["aggregate"]["false_negatives"] == 1


def test_only_exact_duplicate_candidates_are_deduplicated():
    first = candidate()
    different_evidence = candidate(evidence=[{"status_code": 404, "another_observation": True}])
    result = evaluate(predictions(packet([first, deepcopy(first), different_evidence])), truth())
    assert result["exact_duplicate_candidates_removed"] == 1
    assert result["aggregate"]["true_positives"] == 1
    assert result["aggregate"]["false_positives"] == 1


def test_empty_outputs_are_false_negatives_not_a_perfect_score():
    result = evaluate(predictions(packet([], decision="NO-ACTION")), truth())
    assert result["aggregate"]["precision"] is None
    assert result["aggregate"]["recall"] == 0
    assert result["aggregate"]["false_no_action"] == 1
    assert result["engineering_benchmark_gate_passed"] is False


def test_strong_no_action_control_requires_no_spurious_candidates():
    good = evaluate(predictions(packet([], decision="NO-ACTION")), truth(control=True))
    bad = evaluate(predictions(packet([candidate()], decision="NO-ACTION")), truth(control=True))
    assert good["aggregate"]["correct_no_action"] == 1
    assert bad["aggregate"]["correct_no_action"] == 0
    assert bad["aggregate"]["false_positives"] == 1
    assert bad["aggregate"]["false_no_action"] == 1
    assert bad["aggregate"]["protected_url_false_positives"] == 1


def test_appropriate_abstention_is_reported_separately_from_detection_recall():
    result = evaluate(predictions(packet([], decision="NEEDS_EVIDENCE", coverage=False)), truth(ambiguous=True))
    assert result["aggregate"]["abstentions"] == 1
    assert result["aggregate"]["appropriate_uncertain_outcomes"] == 1
    assert result["aggregate"]["false_negatives"] == 1
    assert result["aggregate"]["false_no_action"] == 0


@pytest.mark.parametrize("disposition", ["CONFIRMED", "EXECUTE", "PUBLISH", "AUTO_FIX", "UNSPECIFIED"])
def test_unsafe_or_unspecified_disposition_never_sneaks_through_review_label(disposition):
    result = evaluate(predictions(packet([candidate(disposition=disposition)], coverage=False)), truth(ambiguous=True))
    assert result["aggregate"]["true_positives"] == 1
    assert result["aggregate"]["disposition_overclaims"] == 1
    assert result["engineering_benchmark_gate_passed"] is False


@pytest.mark.parametrize("metadata", [{"production_enabled": True}, {"autonomy_level": 2}, {"autonomy_level": True}, {"paid_api_calls": 1}, {"production_write_budget": 1}, {"production_write_budget": False}, {"source_fingerprint": "missing"}])
def test_metadata_cannot_claim_authority_or_spend(metadata):
    result = evaluate(predictions(**metadata), truth())
    assert not all(result["invariant_checks"].values())
    assert result["engineering_benchmark_gate_passed"] is False
    assert result["level_2_eligible"] is False


def test_missing_extra_and_duplicate_case_packets_are_failures():
    missing = predictions()
    missing["cases"] = []
    assert evaluate(missing, truth())["missing_cases"] == 1
    extra = predictions()
    extra["cases"].append({**packet([candidate()]), "case_id": "unknown"})
    extra["cases"].append(packet([candidate()]))
    result = evaluate(extra, truth())
    assert result["unknown_or_invalid_case_penalties"] == 2
    assert result["duplicate_case_predictions"] == 1
    assert result["aggregate"]["false_positives"] == 2


def test_claiming_complete_coverage_of_an_incomplete_case_is_an_overclaim():
    result = evaluate(predictions(packet([candidate()], coverage=True)), truth(ambiguous=True))
    assert result["aggregate"]["coverage_overclaims"] == 1


def test_no_action_cannot_simultaneously_disclaim_observation_coverage():
    result = evaluate(predictions(packet([], decision="NO-ACTION", coverage=False)), truth(control=True))
    assert result["aggregate"]["correct_no_action"] == 0
    assert result["aggregate"]["false_no_action"] == 1


def test_missing_coverage_attestation_is_a_protocol_error():
    pred = predictions()
    del pred["cases"][0]["coverage_complete"]
    result = evaluate(pred, truth())
    assert "missing_or_invalid_coverage:toy" in result["protocol_errors"]


def test_no_evidence_is_recorded_as_unsubstantiated():
    result = evaluate(predictions(packet([candidate(evidence=[])])), truth())
    assert result["aggregate"]["unsubstantiated_candidates"] == 1


def test_macro_metrics_balance_families_instead_of_renamed_instances():
    reference = truth()
    reference["cases"]["toy2"] = deepcopy(reference["cases"]["toy"])
    reference["cases"]["toy2"]["case_id"] = "toy2"
    reference["cases"]["toy3"] = deepcopy(reference["cases"]["toy"])
    reference["cases"]["toy3"].update({"case_id": "toy3", "family": "different_family"})
    pred = predictions()
    pred["cases"].extend([{**packet([candidate()]), "case_id": "toy2"}, {**packet([], decision="NEEDS_EVIDENCE"), "case_id": "toy3"}])
    result = evaluate(pred, reference)
    assert result["aggregate"]["recall"] == pytest.approx(2 / 3)
    assert result["macro_family"]["recall"] == 0.5


def test_matching_is_order_independent_when_allowed_anchor_sets_overlap():
    reference = truth()
    unit = reference["cases"]["toy"]["units"][0]
    unit.update({"page_urls": [U, V], "related_urls": [], "related_mode": "optional_expected"})
    second = deepcopy(unit)
    second.update({"unit_id": "u2", "page_urls": [U]})
    reference["cases"]["toy"]["units"].append(second)
    result = evaluate(predictions(packet([candidate(page_url=U, related_urls=[]), candidate(page_url=V, related_urls=[])])), reference)
    assert result["aggregate"]["true_positives"] == 2


def test_runtime_commitment_mismatch_and_graduation_claim_are_rejected():
    reference = truth()
    reference["runtime_input_sha256"] = "c" * 64
    result = evaluate(predictions(runtime_corpus_sha256="d" * 64, level_2_eligible=True), reference)
    assert result["runtime_corpus_commitment_matches"] is False
    assert "unauthorised_autonomy_graduation_claim" in result["protocol_errors"]
    assert result["level_2_eligible"] is False


def test_scorer_does_not_mutate_inputs():
    pred, reference = predictions(), truth()
    original_pred, original_truth = deepcopy(pred), deepcopy(reference)
    evaluate(pred, reference)
    assert pred == original_pred
    assert reference == original_truth


@pytest.mark.parametrize("invalid", [None, [], "administrator command"])
def test_non_mapping_inputs_fail_closed(invalid):
    with pytest.raises(TypeError):
        evaluate(invalid, truth())
