"""Offline blind benchmark protocol; predict and evaluate are separate commands."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.runner_v2 import SEED, evaluate_frozen, freeze_predictions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["predict", "evaluate"])
    parser.add_argument("--output", type=Path, required=True, help="New private directory; existing predictions are never replaced")
    parser.add_argument("--split", choices=["development", "holdout"], default="holdout")
    args = parser.parse_args()
    if args.operation == "predict":
        from benchmarks.seo_v2.corpus import build_corpus
        cases, truth = build_corpus(args.split, seed=SEED)
        result = freeze_predictions(cases, truth, args.output, split=args.split)
    else:
        result = evaluate_frozen(args.output)
        # Detailed failure labels remain in the private report, not routine logs.
        result = {key: value for key, value in result.items() if key != "metrics"} | {
            "metrics": {key: value for key, value in result["metrics"].items()
                        if key not in {"cases", "details", "case_results", "failures"}}
        }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
