"""Truth-blind owner CLI for the evaluator-signed v3 exchange.

This command cannot create evaluator signatures, load ground truth, score cases,
or alter autonomy.  An independent evaluator retains those capabilities.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.seo.benchmark_attestation import public_attestation_summary, verify_signed_attestation
from benchmarks.blind_exchange_v3 import freeze_response, protocol_source_fingerprint


def _json(path: Path):
    if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Input file is missing or exceeds the protocol budget")
    return json.loads(path.read_bytes(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="operation", required=True)
    operations.add_parser("fingerprint", help="Print the exact detector/protocol source fingerprint")
    predict = operations.add_parser("predict", help="Run a signed observation-only challenge once")
    predict.add_argument("--challenge", type=Path, required=True)
    predict.add_argument("--public-key", type=Path, required=True)
    predict.add_argument("--key-id", required=True)
    predict.add_argument("--output", type=Path, required=True)
    verify = operations.add_parser("verify-attestation", help="Verify and print only a safe aggregate attestation")
    verify.add_argument("--attestation", type=Path, required=True)
    verify.add_argument("--public-key", type=Path, required=True)
    verify.add_argument("--key-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == "fingerprint":
            result = {"source_fingerprint": protocol_source_fingerprint(), "level_2_eligible": False}
        elif args.operation == "predict":
            response = freeze_response(
                _json(args.challenge), args.public_key.read_bytes(), args.output, expected_key_id=args.key_id,
            )
            result = {
                "status": "frozen",
                "evaluation_id": response.evaluation_id,
                "challenge_sha256": response.challenge_sha256,
                "predictions_sha256": response.predictions_sha256,
                "source_fingerprint": response.source_fingerprint,
                "output": str(args.output.resolve()),
                "production_writes": 0,
                "paid_api_calls": 0,
                "level_2_eligible": False,
            }
        else:
            attestation = verify_signed_attestation(
                _json(args.attestation), args.public_key.read_bytes(), expected_key_id=args.key_id,
            )
            result = {**public_attestation_summary(attestation), "signature_verified": True}
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"status": "blocked", "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
