"""Retired holdouts must never become a routine optimization feedback loop."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_routine_ci_never_executes_or_uploads_retired_v2_holdout():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "benchmark_v2.py predict --split holdout" not in workflow
    assert "benchmark-v2-regression" not in workflow


def test_retirement_document_requires_a_new_independent_holdout():
    policy = (ROOT / "docs/DETECTOR_DEVELOPMENT_V3.md").read_text()
    assert "newly authored holdout" in policy
    assert "Level 2 remains blocked" in policy
