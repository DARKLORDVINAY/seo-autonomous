"""Static guard plus CI runtime inspection for evaluator/runtime separation."""
import json
from pathlib import Path

from scripts.public_lab_safe_summary import build_summary


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_dockerfile_uses_operator_script_allowlist_and_no_test_lab_copy():
    dockerfile = (ROOT / "docker/Dockerfile").read_text()
    assert "COPY --chown=10001:10001 scripts /app/scripts" not in dockerfile
    assert "COPY --chown=10001:10001 test_lab" not in dockerfile
    assert "COPY --chown=10001:10001 benchmarks" not in dockerfile
    assert "scripts/bootstrap.py scripts/grant_runtime.py scripts/deployment_preflight.py /app/scripts/" in dockerfile
    assert "docker/entrypoint.py /app/docker/" in dockerfile


def test_build_context_excludes_truth_corpora_and_nonruntime_scripts():
    patterns = set((ROOT / ".dockerignore").read_text().splitlines())
    assert {"benchmarks", "test_lab", "scripts/*"} <= patterns
    assert {
        "!scripts/bootstrap.py",
        "!scripts/grant_runtime.py",
        "!scripts/deployment_preflight.py",
    } <= patterns


def test_ci_inspects_actual_image_and_never_uploads_labelled_report():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "Prove runtime image excludes evaluator truth and operator tooling" in workflow
    assert 'not (root / "benchmarks").exists()' in workflow
    assert 'not (root / "test_lab").exists()' in workflow
    assert '"blind_evaluation_v3.py"' in workflow
    assert "artifacts/test-lab-*.json" not in workflow
    public_workflow = (ROOT / ".github/workflows/public-lab.yml").read_text()
    assert "aggregate-only public observation receipt" in public_workflow
    assert "artifacts/test-lab-*.json" not in public_workflow
    assert "public-lab-ci-state.sql" not in public_workflow
    assert "pg_dump" not in public_workflow
    assert "artifacts/public-lab-safe-summary.json" in public_workflow


def test_public_lab_export_is_a_strict_whitelist_not_a_report_copy():
    private_marker = "private-label:/held-out-canonical-case/"
    report = {
        "site_id": "86fb5cb7-5247-4cc7-a2fb-34a0a7bd5bc9",
        "base_url": "https://seo-test-lab.pages.dev",
        "manifest_sha256": "a" * 64,
        "decision_packet_sha256": "b" * 64,
        "assessment": {
            "true_positives": 3,
            "false_positives": 1,
            "false_negatives": 2,
            "precision": 0.75,
            "recall": 0.6,
            "correct_no_action": 5,
            "false_no_action": 0,
            "coverage_complete": True,
            "high_critical_intercepted": True,
            "zero_autonomous_production_changes": True,
            "structural_benchmark_passed": False,
            "matches": [{"path": private_marker}],
            "unexpected_detections": [{"path": private_marker}],
            "missed_issues": [{"path": private_marker}],
            "limitations": [private_marker],
        },
        "specialists": {"rationale": private_marker},
        "risk_previews": [{"page": private_marker}],
    }
    summary = build_summary(report)
    assert private_marker not in json.dumps(summary)
    assert summary["aggregate"]["false_positives"] == 1
    assert summary["private_case_results_included"] is False
    assert summary["disposable_database_exported"] is False
    assert summary["level_2_eligible"] is False
