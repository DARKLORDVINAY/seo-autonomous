"""Commit predictions before evaluator exposure; never use truth in the child.

One fixed holdout seed avoids seed shopping. Disclosed holdout results are
regression evidence thereafter, not a new independent blind evaluation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SEED = 20260903
SOURCE_PATHS = (
    "backend/__init__.py", "backend/app/__init__.py", "backend/app/contracts.py",
    "backend/app/seo/__init__.py", "backend/app/seo/analysis.py", "backend/app/seo/benchmark_runtime.py",
)
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 32 * 1024 * 1024


def encode(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def source_hashes() -> dict[str, str]:
    # Parent-only generator/scorer commitments prevent post-prediction scoring
    # changes. Their bytes are NEVER copied into the runtime stage.
    paths = (*SOURCE_PATHS, "benchmarks/isolated_worker.py", "benchmarks/runner_v2.py",
             "benchmarks/seo_v2/__init__.py", "benchmarks/seo_v2/corpus.py", "benchmarks/seo_v2/evaluator.py")
    return {path: digest((ROOT / path).read_bytes()) for path in paths}


def source_fingerprint() -> str:
    return digest(encode(source_hashes()))


def run_isolated(cases: list[dict], *, isolation_probe: Path | None = None) -> dict:
    raw = encode(cases)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("Input byte budget exceeded")
    with tempfile.TemporaryDirectory(prefix="seo-v2-runtime-") as folder:
        stage = Path(folder)
        for name in SOURCE_PATHS:
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        shutil.copyfile(ROOT / "benchmarks/isolated_worker.py", stage / "worker.py")
        (stage / "observations.json").write_bytes(raw)
        command = [sys.executable, "-I", "-B", str(stage / "worker.py")]
        command += ["--check-isolation", str(isolation_probe.resolve())] if isolation_probe else [str(stage / "observations.json")]
        process = subprocess.run(
            command, cwd=stage, env={"PATH": os.defpath, "LANG": "C.UTF-8", "TZ": "UTC"},
            capture_output=True, timeout=45, check=False,
        )
        if process.returncode:
            # Untrusted values and ambient diagnostics must not enter reports.
            raise RuntimeError(f"Isolated worker failed closed (exit {process.returncode})")
        if len(process.stdout) > MAX_OUTPUT_BYTES:
            raise ValueError("Output byte budget exceeded")
        return json.loads(process.stdout)


def freeze_predictions(cases: list[dict], truth: dict, output: Path, *, split: str) -> dict:
    if split not in {"development", "holdout", "test"}:
        raise ValueError("Unsupported split")
    output.mkdir(parents=True, exist_ok=False)
    os.chmod(output, 0o700)
    # Truth remains exclusively in the parent; it is never staged or passed to
    # the process which imports the actual production detector.
    inputs = encode(cases)
    truth_bytes = encode(truth)
    sources_before = source_hashes()
    packet = run_isolated(cases)
    if source_hashes() != sources_before:
        raise ValueError("Source changed during prediction; discard this run")
    packet.update(source_fingerprint=digest(encode(sources_before)), input_sha256=digest(inputs))
    encoded_packet = encode(packet)
    files = {"observations.json": inputs, "evaluator-truth.json": truth_bytes, "predictions.json": encoded_packet}
    manifest = {
        "schema_version": 2, "split": split, "seed": SEED,
        "frozen_at": datetime.now(timezone.utc).isoformat(), "case_count": len(cases),
        "source_hashes": sources_before, "source_fingerprint": packet["source_fingerprint"],
        "files": {name: digest(raw) for name, raw in files.items()},
        "protocol": "source_and_prediction_commitment_before_evaluation",
        "prediction_runtime_contains_truth": False, "level_2_eligible": False,
    }
    for name, raw in {**files, "commitment.json": encode(manifest)}.items():
        with (output / name).open("xb") as handle:
            handle.write(raw)
        os.chmod(output / name, 0o400)
    return manifest


def evaluate_frozen(output: Path, *, evaluator=None) -> dict:
    manifest = json.loads((output / "commitment.json").read_bytes())
    if set(manifest["files"]) != {"observations.json", "predictions.json", "evaluator-truth.json"}:
        raise ValueError("Invalid commitment file set")
    for name, expected in manifest["files"].items():
        if digest((output / name).read_bytes()) != expected:
            raise ValueError(f"Frozen artifact changed: {name}")
    if source_hashes() != manifest["source_hashes"]:
        raise ValueError("Detector/protocol source changed after prediction")
    packet = json.loads((output / "predictions.json").read_bytes())
    if (packet.get("source_fingerprint") != manifest["source_fingerprint"]
            or packet.get("input_sha256") != manifest["files"]["observations.json"]):
        raise ValueError("Prediction commitment mismatch")
    existing = output / "evaluation.json"
    if existing.exists():
        report_bytes = existing.read_bytes()
        seal = output / "evaluation.sha256"
        if not seal.is_file() or seal.read_text().strip() != digest(report_bytes):
            raise ValueError("Frozen evaluation report changed or has no integrity seal")
        previous = json.loads(report_bytes)
        if previous.get("commitment_sha256") != digest((output / "commitment.json").read_bytes()):
            raise ValueError("Evaluation commitment mismatch")
        if any(previous.get(key) != value for key, value in {
            "autonomy_level": 1, "production_enabled": False, "production_write_budget": 0,
            "production_writes": 0, "paid_api_calls": 0, "level_2_eligible": False,
            "live_model_executed": False,
        }.items()):
            raise ValueError("Evaluation safety invariant mismatch")
        return previous
    if evaluator is None:
        from benchmarks.seo_v2.evaluator import evaluate
        evaluator = evaluate
    result = evaluator(packet, json.loads((output / "evaluator-truth.json").read_bytes()))
    report = {
        "split": manifest["split"], "case_count": manifest["case_count"], "metrics": result,
        "source_fingerprint": manifest["source_fingerprint"],
        "commitment_sha256": digest((output / "commitment.json").read_bytes()),
        "input_sha256": manifest["files"]["observations.json"],
        "predictions_sha256": manifest["files"]["predictions.json"],
        "truth_sha256": manifest["files"]["evaluator-truth.json"],
        "holdout_exposed_after_evaluation": manifest["split"] == "holdout",
        "later_runs_are_regressions_not_fresh_holdout": manifest["split"] == "holdout",
        "autonomy_level": 1, "production_enabled": False, "production_write_budget": 0,
        "production_writes": 0, "paid_api_calls": 0, "level_2_eligible": False,
        "live_model_executed": False,
        "limitations": ["synthetic observations, not search outcomes", "rendered snapshots are simulated DOM fixtures",
                        "Python audit hooks are not an OS sandbox", "no empirical Level 2 qualification"],
    }
    report_bytes = encode(report)
    with existing.open("xb") as handle:
        handle.write(report_bytes)
    os.chmod(existing, 0o400)
    with (output / "evaluation.sha256").open("x") as handle:
        handle.write(digest(report_bytes) + "\n")
    os.chmod(output / "evaluation.sha256", 0o400)
    return report
