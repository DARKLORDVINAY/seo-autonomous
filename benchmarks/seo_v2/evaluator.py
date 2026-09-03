"""Pure evaluator, intentionally unavailable to the prediction runtime.

The scorer consumes frozen packets; it never invokes a model, detector, network,
database, executor, or tool.  Evaluation does not grant autonomy.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DISPOSITION_ALIASES = {"INVESTIGATE": "REVIEW", "NEEDS-EVIDENCE": "NEEDS_EVIDENCE"}
_KIND_ALIASES = {
    "accidental_noindex": "indexability_review",
    "duplicate_description": "duplicate_meta_description",
    "duplicate_meta": "duplicate_meta_description",
    "soft404": "soft_404",
    "canonical_loop": "canonical_cycle",
}
_ASSERTIVE_DISPOSITIONS = {"CONFIRMED", "EXECUTE", "FIX", "PUBLISH", "APPROVED", "AUTO_FIX", "DELETE"}
_UNCERTAIN_CLASSES = {"hypothesis", "needs_evidence", "needs_owner_intent", "diagnostic_review"}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _divide(numerator: int | float, denominator: int | float) -> float | None:
    return numerator / denominator if denominator else None


def _normalise_kind(kind: str) -> str:
    return _KIND_ALIASES.get(kind, kind)


def _disposition(candidate: dict[str, Any]) -> str:
    disposition = str(candidate.get("disposition", "UNSPECIFIED")).upper()
    return _DISPOSITION_ALIASES.get(disposition, disposition)


def _matches(candidate: dict[str, Any], unit: dict[str, Any]) -> bool:
    if _normalise_kind(candidate["kind"]) != _normalise_kind(unit["kind"]):
        return False
    if candidate["page_url"] not in unit["page_urls"]:
        return False
    actual = set(candidate.get("related_urls", []))
    expected = set(unit.get("related_urls", []))
    mode = unit.get("related_mode", "optional_expected")
    if mode == "contains":
        # A correct source with a fabricated destination does not earn credit.
        return bool(expected) and actual == expected
    if mode == "exact":
        return actual == expected
    if mode == "optional_expected":
        # Some diagnostic groups permit a correct representative URL without
        # requiring every affected member to be re-listed.  Any supplied member
        # must still belong to the preregistered group.
        return actual <= (expected | set(unit["page_urls"]))
    raise ValueError(f"Unknown private related_url matching mode: {mode}")


def _valid_candidate(candidate: Any) -> bool:
    return (
        isinstance(candidate, dict)
        and isinstance(candidate.get("kind"), str) and bool(candidate["kind"])
        and isinstance(candidate.get("page_url"), str) and bool(candidate["page_url"])
        and isinstance(candidate.get("related_urls", []), list)
        and all(isinstance(value, str) for value in candidate.get("related_urls", []))
    )


def _one_to_one_matches(candidates: list[dict[str, Any]], units: list[dict[str, Any]]) -> dict[int, int]:
    """Maximum bipartite matching without rewarding multiple guesses per unit."""
    adjacency = [[index for index, unit in enumerate(units) if _matches(candidate, unit)] for candidate in candidates]
    unit_to_prediction: dict[int, int] = {}

    def augment(prediction: int, visited: set[int]) -> bool:
        for unit in adjacency[prediction]:
            if unit in visited:
                continue
            visited.add(unit)
            if unit not in unit_to_prediction or augment(unit_to_prediction[unit], visited):
                unit_to_prediction[unit] = prediction
                return True
        return False

    for prediction in range(len(candidates)):
        augment(prediction, set())
    return {prediction: unit for unit, prediction in unit_to_prediction.items()}


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tp = sum(row["true_positives"] for row in rows)
    fp = sum(row["false_positives"] for row in rows)
    fn = sum(row["false_negatives"] for row in rows)
    controls = [row for row in rows if row["stratum"] == "control"]
    return {
        "cases": len(rows), "true_positives": tp, "false_positives": fp, "false_negatives": fn,
        "precision": _divide(tp, tp + fp), "recall": _divide(tp, tp + fn),
        "f1": _divide(2 * tp, 2 * tp + fp + fn),
        "no_action_controls": len(controls),
        "correct_no_action": sum(row["correct_no_action"] for row in controls),
        "no_action_accuracy": _divide(sum(row["correct_no_action"] for row in controls), len(controls)),
        "false_no_action": sum(row["false_no_action"] for row in rows),
        "abstentions": sum(row["decision"] == "NEEDS_EVIDENCE" for row in rows),
        "appropriate_uncertain_outcomes": sum(row["appropriate_uncertain_outcome"] for row in rows),
        "disposition_overclaims": sum(len(row["disposition_overclaims"]) for row in rows),
        "protected_url_false_positives": sum(row["protected_url_false_positives"] for row in rows),
        "unsubstantiated_candidates": sum(row["unsubstantiated_candidates"] for row in rows),
        "decision_errors": sum(not row["decision_correct"] for row in rows),
        "coverage_overclaims": sum(row["coverage_overclaim"] for row in rows),
    }


def evaluate(predictions: dict[str, Any], truth: dict[str, Any]) -> dict[str, Any]:
    """Score once-frozen output against private truth, returning JSON-compatible data.

    The caller must independently attest filesystem/process isolation and verify
    file commitments.  A scorer cannot establish either from self-reported model
    metadata.  Unknown/missing cases, extra guesses and malformed candidates are
    retained as failures, not silently discarded.
    """
    if not isinstance(predictions, dict) or not isinstance(truth, dict):
        raise TypeError("predictions and truth must be dictionaries")
    truth_cases = truth.get("cases")
    if not isinstance(truth_cases, dict) or not truth_cases:
        raise ValueError("Private truth must contain nonempty case records")
    if len(truth_cases) > 10000:
        raise ValueError("Evaluator case limit exceeded")
    raw_cases = predictions.get("cases", [])
    if isinstance(raw_cases, dict):
        raw_cases = [{**packet, "case_id": case_id} if isinstance(packet, dict) else packet
                     for case_id, packet in raw_cases.items()]
    if not isinstance(raw_cases, list) or len(raw_cases) > 20000:
        raise ValueError("Predicted cases must be a bounded list or mapping")
    predicted: dict[str, dict[str, Any]] = {}
    protocol_errors: list[str] = []
    extras = 0
    duplicate_case_predictions = 0
    for index, packet in enumerate(raw_cases):
        if not isinstance(packet, dict) or not isinstance(packet.get("case_id"), str):
            protocol_errors.append(f"malformed_case:{index}")
            extras += 1
            continue
        case_id = packet["case_id"]
        if case_id in predicted:
            duplicate_case_predictions += 1
            protocol_errors.append(f"duplicate_case:{case_id}")
            extras += max(1, len(packet.get("candidates", [])) if isinstance(packet.get("candidates", []), list) else 1)
            continue
        if case_id not in truth_cases:
            protocol_errors.append(f"unknown_case:{case_id}")
            extras += max(1, len(packet.get("candidates", [])) if isinstance(packet.get("candidates", []), list) else 1)
            continue
        predicted[case_id] = packet
    rows: list[dict[str, Any]] = []
    exact_duplicates = 0
    raw_candidate_count = 0
    for case_id, expected in truth_cases.items():
        packet = predicted.get(case_id)
        missing = packet is None
        packet = packet or {}
        raw_candidates = packet.get("candidates", [])
        if not isinstance(raw_candidates, list) or len(raw_candidates) > 2000:
            protocol_errors.append(f"malformed_or_oversized_candidates:{case_id}")
            raw_candidates = [None]
        raw_candidate_count += len(raw_candidates)
        candidates: list[dict[str, Any]] = []
        malformed = 0
        seen: set[str] = set()
        for candidate in raw_candidates:
            if not _valid_candidate(candidate):
                malformed += 1
                continue
            try:
                identity = _hash(candidate)
            except (ValueError, TypeError):
                malformed += 1
                continue
            if identity in seen:
                exact_duplicates += 1
                continue
            seen.add(identity)
            candidates.append(candidate)
        units = expected["units"]
        matches = _one_to_one_matches(candidates, units)
        unmatched = [index for index in range(len(candidates)) if index not in matches]
        overclaims: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates):
            disposition = _disposition(candidate)
            unit = units[matches[index]] if index in matches else None
            allowed = unit.get("allowed_dispositions", ["REVIEW", "NEEDS_EVIDENCE"]) if unit else ["REVIEW", "NEEDS_EVIDENCE"]
            specificity_overclaim = bool(unit and unit.get("epistemic_class") in _UNCERTAIN_CLASSES and
                                         candidate["kind"] in {"accidental_noindex", "cannibalisation", "cannibalization", "confirmed_cannibalisation"})
            if disposition not in allowed or specificity_overclaim:
                overclaims.append({"candidate_index": index, "kind": candidate["kind"],
                                   "disposition": disposition, "unit_id": unit["unit_id"] if unit else None,
                                   "assertive": disposition in _ASSERTIVE_DISPOSITIONS or specificity_overclaim})
        decision = packet.get("decision", "MISSING")
        if not isinstance(decision, str):
            decision = "INVALID"
        expected_decisions = expected.get("expected_decisions", ["INVESTIGATE"])
        no_candidates = not candidates and not malformed
        correct_no_action = (not missing and expected["stratum"] == "control" and decision == "NO-ACTION" and
                             no_candidates and packet.get("coverage_complete") is True)
        false_no_action = decision == "NO-ACTION" and (bool(units) or "NO-ACTION" not in expected_decisions or
                                                       not no_candidates or packet.get("coverage_complete") is not True)
        coverage_overclaim = packet.get("coverage_complete") is True and expected.get("coverage_complete") is False
        if missing:
            protocol_errors.append(f"missing_case:{case_id}")
        elif type(packet.get("coverage_complete")) is not bool:
            protocol_errors.append(f"missing_or_invalid_coverage:{case_id}")
        if decision not in {"INVESTIGATE", "NO-ACTION", "NEEDS_EVIDENCE", "MISSING"}:
            protocol_errors.append(f"invalid_decision:{case_id}")
        inconsistent_decision = (decision == "NO-ACTION" and (not no_candidates or packet.get("coverage_complete") is not True)) or (decision == "INVESTIGATE" and no_candidates)
        decision_correct = decision in expected_decisions and not inconsistent_decision and not missing
        rows.append({
            "case_id": case_id, "family": expected["family"], "stratum": expected["stratum"],
            "true_positives": len(matches), "false_positives": len(unmatched) + malformed,
            "false_negatives": len(units) - len(matches),
            "matched_unit_ids": [units[index]["unit_id"] for index in sorted(matches.values())],
            "missed_unit_ids": [unit["unit_id"] for index, unit in enumerate(units) if index not in set(matches.values())],
            "unmatched_candidate_indices": unmatched,
            "decision": decision, "expected_decisions": expected_decisions,
            "decision_correct": decision_correct, "correct_no_action": correct_no_action,
            "false_no_action": false_no_action, "coverage_overclaim": coverage_overclaim,
            "appropriate_uncertain_outcome": (expected["stratum"] == "ambiguous" and decision_correct and
                                               not overclaims and not unmatched and not malformed and not coverage_overclaim),
            "disposition_overclaims": overclaims,
            "protected_url_false_positives": sum(candidates[index]["page_url"] in expected.get("protected_urls", []) for index in unmatched),
            "unsubstantiated_candidates": sum(not isinstance(c.get("evidence"), list) or not c["evidence"] for c in candidates),
            "missing_prediction": missing,
        })
    aggregate = _metrics(rows)
    aggregate["false_positives"] += extras
    tp, fp, fn = aggregate["true_positives"], aggregate["false_positives"], aggregate["false_negatives"]
    aggregate.update({"precision": _divide(tp, tp + fp), "recall": _divide(tp, tp + fn), "f1": _divide(2 * tp, 2 * tp + fp + fn)})
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    strata: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        families[row["family"]].append(row)
        strata[row["stratum"]].append(row)
    family_metrics = {name: _metrics(values) for name, values in sorted(families.items())}
    macro = {}
    for name in ("precision", "recall", "f1", "no_action_accuracy"):
        values = [value[name] for value in family_metrics.values() if value[name] is not None]
        macro[name] = sum(values) / len(values) if values else None
        macro[f"{name}_families"] = len(values)
    metadata = predictions.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    invariants = {
        "autonomy_level_1": type(metadata.get("autonomy_level")) is int and metadata["autonomy_level"] == 1,
        "production_disabled": metadata.get("production_enabled") is False,
        "zero_production_write_budget": type(metadata.get("production_write_budget")) is int and metadata["production_write_budget"] == 0,
        "zero_paid_api_calls": type(metadata.get("paid_api_calls")) is int and metadata["paid_api_calls"] == 0,
        "input_commitment_present": isinstance(metadata.get("input_sha256"), str) and bool(_SHA256.fullmatch(metadata["input_sha256"])),
        "source_commitment_present": isinstance(metadata.get("source_fingerprint"), str) and bool(_SHA256.fullmatch(metadata["source_fingerprint"])),
    }
    corpus_commitment = metadata.get("runtime_corpus_sha256")
    corpus_identity = (corpus_commitment == truth.get("runtime_input_sha256")) if corpus_commitment is not None else None
    if corpus_identity is False:
        protocol_errors.append("runtime_corpus_commitment_mismatch")
    if metadata.get("level_2_eligible") is True:
        protocol_errors.append("unauthorised_autonomy_graduation_claim")
    # Preregistered engineering regression gate, deliberately not an autonomy
    # gate or estimated real-world success probability.
    thresholds = {"precision_min": 0.95, "recall_min": 0.90, "macro_family_recall_min": 0.80,
                  "no_action_accuracy_min": 0.95, "false_no_action_max": 0,
                  "disposition_overclaims_max": 0, "coverage_overclaims_max": 0}
    gate_passed = (
        bool(aggregate["precision"] is not None and aggregate["precision"] >= thresholds["precision_min"])
        and bool(aggregate["recall"] is not None and aggregate["recall"] >= thresholds["recall_min"])
        and bool(macro["recall"] is not None and macro["recall"] >= thresholds["macro_family_recall_min"])
        and bool(aggregate["no_action_accuracy"] is not None and aggregate["no_action_accuracy"] >= thresholds["no_action_accuracy_min"])
        and aggregate["false_no_action"] == 0 and aggregate["disposition_overclaims"] == 0
        and aggregate["coverage_overclaims"] == 0 and aggregate["unsubstantiated_candidates"] == 0
        and all(invariants.values()) and not protocol_errors
    )
    return {
        "schema_version": "2.0", "split": truth.get("split", "toy"),
        "aggregate": aggregate, "macro_family": macro, "by_family": family_metrics,
        "by_stratum": {name: _metrics(values) for name, values in sorted(strata.items())},
        "cases": rows, "protocol_errors": protocol_errors,
        "exact_duplicate_candidates_removed": exact_duplicates, "raw_candidate_count": raw_candidate_count,
        "duplicate_case_predictions": duplicate_case_predictions, "unknown_or_invalid_case_penalties": extras,
        "missing_cases": sum(row["missing_prediction"] for row in rows),
        "invariant_checks": invariants, "runtime_corpus_commitment_matches": corpus_identity,
        "source_fingerprint": metadata.get("source_fingerprint"), "input_sha256": metadata.get("input_sha256"),
        "prediction_sha256": _hash(predictions), "truth_commitment_sha256": truth.get("truth_commitment_sha256"),
        "thresholds": thresholds, "engineering_benchmark_gate_passed": gate_passed,
        "level_2_eligible": False, "autonomy_level": 1, "production_enabled": False,
        "production_write_budget": 0, "paid_api_calls": 0,
        "limitations": [
            "Synthetic, family-correlated examples are not independent estimates of real-world SEO performance.",
            "Reviewable structural signals are not proven Google indexing errors or causal business benefits.",
            "Rendered observations are simulated snapshots, not real browser execution.",
            "Empty Google rows provide no evidence about search demand, conversions, or query cannibalisation.",
            "Input/source metadata are commitments, not proof of isolation; the caller must verify the frozen process/filesystem boundary.",
            "Only byte-equivalent candidate objects are deduplicated; additional unassigned or redundant alert units count against precision.",
            "Evidence presence is checked, but this scorer alone cannot establish evidence provenance or factual faithfulness.",
            "Publishing first-run errors consumes this holdout; do not tune on it and relabel a rerun as blind.",
            "No benchmark result grants Level 2 eligibility or production authority.",
        ],
    }
