"""Export a bounded aggregate receipt from a private legacy Test Lab report."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID


DIGEST = re.compile(r"[0-9a-f]{64}\Z")
ASSESSMENT_FIELDS = (
    "true_positives",
    "false_positives",
    "false_negatives",
    "precision",
    "recall",
    "correct_no_action",
    "false_no_action",
    "coverage_complete",
    "high_critical_intercepted",
    "zero_autonomous_production_changes",
    "structural_benchmark_passed",
)


def _identifier(value: Any) -> str | None:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _origin(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        return None
    return f"https://{parsed.hostname}"


def _digest(value: Any) -> str | None:
    return value if isinstance(value, str) and DIGEST.fullmatch(value) else None


def _scalar(value: Any) -> bool | int | float | None:
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return None


def build_summary(report: Any) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise ValueError("A report object is required")
    assessment = report.get("assessment") if isinstance(report.get("assessment"), dict) else {}
    return {
        "schema_version": 1,
        "scope": "aggregate_only_public_structural_observation",
        "site_id": _identifier(report.get("site_id")),
        "base_url": _origin(report.get("base_url")),
        "manifest_sha256": _digest(report.get("manifest_sha256")),
        "decision_packet_sha256": _digest(report.get("decision_packet_sha256")),
        "autonomy_level": 1,
        "production_enabled": False,
        "production_write_budget": 0,
        "paid_api_calls": 0,
        "level_2_eligible": False,
        "aggregate": {key: _scalar(assessment.get(key)) for key in ASSESSMENT_FIELDS},
        "private_case_results_included": False,
        "disposable_database_exported": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if not args.input.is_file() or args.input.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Private report is unavailable or too large")
        report = json.loads(args.input.read_bytes(), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        summary = build_summary(report)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as handle:
            json.dump(summary, handle, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
        print(json.dumps({"status": "exported", "output": str(args.output), "aggregate_only": True}))
        return 0
    except Exception as error:
        print(json.dumps({"status": "blocked", "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
