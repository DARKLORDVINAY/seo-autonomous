"""Metadata-only corrigendum for v2's first frozen packet/scorer mismatch.

The independent scorer expected a `metadata` envelope; the agreed runtime put
those exact fields at top level. Preserve the original run. Re-score only the
same immutable candidates with their verified commitment fields in the expected
envelope. Never rerun predictions, rewrite truth, change thresholds, or present
this as a second blind success.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path

from benchmarks.runner_v2 import digest, encode, evaluate_frozen
from benchmarks.seo_v2.evaluator import evaluate


def adjudicate(output: Path) -> dict:
    original = evaluate_frozen(output)
    packet = json.loads((output / "predictions.json").read_bytes())
    fields = {key: packet[key] for key in (
        "source_fingerprint", "input_sha256", "autonomy_level", "production_enabled",
        "production_write_budget", "paid_api_calls", "level_2_eligible",
    )}
    if (type(fields["autonomy_level"]) is not int or fields["autonomy_level"] != 1
            or fields["production_enabled"] is not False or fields["level_2_eligible"] is not False
            or any(type(fields[key]) is not int or fields[key] != 0
                   for key in ("production_write_budget", "paid_api_calls"))):
        raise ValueError("Frozen runtime safety fields are invalid")
    inputs = json.loads((output / "observations.json").read_bytes())
    fields["runtime_corpus_sha256"] = digest(json.dumps(
        inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode())
    adapted = copy.deepcopy(packet)
    if "metadata" in adapted:
        raise ValueError("This corrigendum only applies to the original flat-envelope protocol")
    adapted["metadata"] = fields
    truth = json.loads((output / "evaluator-truth.json").read_bytes())
    corrected = evaluate(adapted, truth)
    for key in ("aggregate", "macro_family", "by_family", "by_stratum", "cases", "thresholds"):
        if corrected[key] != original["metrics"][key]:
            raise ValueError("Metadata adaptation must not change any issue, decision, or scoring rule")
    if not all(corrected["invariant_checks"].values()) or corrected["runtime_corpus_commitment_matches"] is not True:
        raise ValueError("Frozen protocol commitments do not reconcile")
    report = {
        "status": "metadata_only_corrigendum", "split": original["split"],
        "original_commitment_sha256": original["commitment_sha256"],
        "original_evaluation_sha256": digest((output / "evaluation.json").read_bytes()),
        "original_prediction_sha256": original["predictions_sha256"],
        "adjudicator_sha256": digest(Path(__file__).read_bytes()),
        "candidate_bytes_unchanged": encode(adapted["cases"]) == encode(packet["cases"]),
        "issue_and_decision_scores_unchanged": True, "predictions_rerun": False,
        "blind_replications": 1, "metrics": corrected,
        "autonomy_level": 1, "production_enabled": False, "production_write_budget": 0,
        "paid_api_calls": 0, "level_2_eligible": False,
        "limitation": "The first-run packet/scorer envelope mismatch is retained as a protocol failure, not erased.",
    }
    encoded = encode(report)
    path = output / "metadata-adjudication.json"
    seal = output / "metadata-adjudication.sha256"
    if path.exists():
        if not seal.is_file() or seal.read_text().strip() != digest(path.read_bytes()) or path.read_bytes() != encoded:
            raise ValueError("Existing adjudication differs or was altered")
    else:
        with path.open("xb") as handle:
            handle.write(encoded)
        os.chmod(path, 0o400)
        with seal.open("x") as handle:
            handle.write(digest(encoded) + "\n")
        os.chmod(seal, 0o400)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = adjudicate(args.output)
    print(json.dumps({key: value for key, value in result.items() if key != "metrics"} | {
        "aggregate": result["metrics"]["aggregate"],
        "macro_family": result["metrics"]["macro_family"],
        "invariant_checks": result["metrics"]["invariant_checks"],
        "engineering_benchmark_gate_passed": result["metrics"]["engineering_benchmark_gate_passed"],
    }, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
