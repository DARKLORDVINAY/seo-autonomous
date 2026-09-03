"""Use toy truth to verify the transport-only corrigendum, not holdout answers."""
from __future__ import annotations

import json

from benchmarks import adjudication_v2, runner_v2
from tests.test_benchmark_v2_runtime import toy_case


def test_metadata_adjudication_cannot_change_candidates_scores_or_safety(tmp_path, monkeypatch):
    case = toy_case()
    truth = {"toy": True}
    target = tmp_path / "run"
    runner_v2.freeze_predictions([case], truth, target, split="test")
    original_metric = {key: {} for key in ("aggregate", "macro_family", "by_family", "by_stratum", "cases", "thresholds")}
    runner_v2.evaluate_frozen(target, evaluator=lambda packet, labels: original_metric)
    original_prediction = (target / "predictions.json").read_bytes()
    original_evaluation = (target / "evaluation.json").read_bytes()
    def toy_evaluator(packet, labels):
        assert labels == truth
        assert packet["cases"] == json.loads(original_prediction)["cases"]
        assert packet["metadata"]["autonomy_level"] == 1
        assert packet["metadata"]["production_enabled"] is False
        return {**original_metric, "invariant_checks": {"toy": True}, "runtime_corpus_commitment_matches": True}
    monkeypatch.setattr(adjudication_v2, "evaluate", toy_evaluator)
    report = adjudication_v2.adjudicate(target)
    assert report["candidate_bytes_unchanged"] and report["issue_and_decision_scores_unchanged"]
    assert report["predictions_rerun"] is False and report["level_2_eligible"] is False
    assert (target / "predictions.json").read_bytes() == original_prediction
    assert (target / "evaluation.json").read_bytes() == original_evaluation
    assert adjudication_v2.adjudicate(target) == report
