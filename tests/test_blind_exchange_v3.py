"""Protocol tests use toy truth, never a current or proposed real holdout."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from backend.app.seo.benchmark_attestation import (
    canonical_bytes,
    public_attestation_summary,
    verify_signed_attestation,
)
from benchmarks.blind_exchange_v3 import (
    FrozenResponse,
    create_response,
    create_signed_challenge,
    digest,
    evaluate_and_sign,
    freeze_response,
    load_frozen_exchange,
    protocol_source_fingerprint,
    public_key_pem,
    verify_signed_challenge,
)


NOW = datetime.now(timezone.utc).replace(microsecond=0)
KEY_ID = "independent-evaluator-toy-key"
PRIVATE_TRUTH_MARKER = "private-family-and-case-label-must-not-leak"
ROOT = Path(__file__).resolve().parents[1]


def observation_case():
    url = "https://example.test/"
    return {
        "case_id": "opaque_v3_toy_01",
        "crawls": [{
            "url": url,
            "final_url": url,
            "status_code": 200,
            "title": "Independent practice home",
            "canonical": url,
            "crawlable": True,
            "indexability": "eligible",
            "main_text": "A legitimate navigation hub with enough observable context.",
            "main_content_observed": True,
            "fetched_at": NOW.isoformat(),
        }],
        "context": {
            "site_url": url,
            "inventory_urls": [url],
            "inventory_complete": True,
            "crawl_coverage_complete": True,
            "entrypoint_urls": [url],
            "sitemap_urls": [url],
            "sitemap_complete": True,
            "intended_indexable_urls": [url],
            "page_purposes": {url: "hub"},
        },
        "gsc_rows": [],
        "ga4_rows": [],
    }


def private_truth():
    return {"cases": {"opaque_v3_toy_01": {"private_label": PRIVATE_TRUTH_MARKER}}}


def toy_scorer(predictions, truth):
    assert predictions["cases"][0]["decision"] == "NO-ACTION"
    assert truth["cases"]["opaque_v3_toy_01"]["private_label"] == PRIVATE_TRUTH_MARKER
    metrics = {
        "cases": 1,
        "true_positives": 1,
        "false_positives": 0,
        "false_negatives": 0,
        "precision": 1.0,
        "recall": 1.0,
        "f1": 1.0,
        "no_action_controls": 1,
        "correct_no_action": 1,
        "no_action_accuracy": 1.0,
        "false_no_action": 0,
        "abstentions": 0,
        "appropriate_uncertain_outcomes": 0,
        "disposition_overclaims": 0,
        "protected_url_false_positives": 0,
        "unsubstantiated_candidates": 0,
        "decision_errors": 0,
        "coverage_overclaims": 0,
    }
    return {
        "aggregate": metrics,
        "macro_family": {"recall": 1.0},
        "by_family": {PRIVATE_TRUTH_MARKER: metrics},
        "by_stratum": {"control": metrics},
        "protocol_errors": [],
        "engineering_benchmark_gate_passed": True,
        "cases": [{"case_id": "opaque_v3_toy_01", "private_label": PRIVATE_TRUTH_MARKER}],
    }


@pytest.fixture
def exchange():
    private_key = Ed25519PrivateKey.generate()
    public_key = public_key_pem(private_key)
    signed = create_signed_challenge(
        [observation_case()],
        private_truth(),
        evaluation_id="independent-toy-evaluation-01",
        evaluator_id="independent-toy-evaluator",
        key_id=KEY_ID,
        private_key=private_key,
        benchmark_definition_sha256="b" * 64,
        accepted_source_fingerprint=protocol_source_fingerprint(),
        issued_at=NOW,
        expires_at=NOW + timedelta(days=2),
    )
    return private_key, public_key, signed


def test_signed_challenge_and_owner_response_never_contain_private_truth(exchange):
    _, public_key, signed = exchange
    challenge_bytes = canonical_bytes(signed)
    assert PRIVATE_TRUTH_MARKER.encode() not in challenge_bytes
    response = create_response(signed, public_key, expected_key_id=KEY_ID, now=NOW)
    assert PRIVATE_TRUTH_MARKER not in canonical_bytes(response).decode()
    assert response.production_writes == response.paid_api_calls == response.production_write_budget == 0
    assert response.production_enabled is response.level_2_eligible is False


def test_evaluator_returns_signed_aggregate_only_and_runtime_verifies_pinned_key(exchange):
    private_key, public_key, signed = exchange
    response = create_response(signed, public_key, expected_key_id=KEY_ID, now=NOW)
    envelope, private_result = evaluate_and_sign(
        signed,
        response,
        private_truth(),
        scorer=toy_scorer,
        trusted_public_key_pem=public_key,
        expected_key_id=KEY_ID,
        private_key=private_key,
        issued_at=NOW + timedelta(hours=1),
    )
    assert PRIVATE_TRUTH_MARKER in json.dumps(private_result)
    assert PRIVATE_TRUTH_MARKER not in canonical_bytes(envelope).decode()
    verified = verify_signed_attestation(envelope, public_key, expected_key_id=KEY_ID)
    summary = {**public_attestation_summary(verified), "signature_verified": True}
    assert summary["metrics"]["correct_no_action"] == 1
    assert summary["signature_verified"] is True
    assert summary["private_case_results_available_to_runtime"] is False
    assert summary["level_2_eligible"] is False
    assert summary["evaluator_reexecuted_predictor"] is True


def test_exchange_accepts_actual_scorer_shape_without_claiming_a_control_only_gate():
    from benchmarks.seo_v2.evaluator import evaluate

    private_key = Ed25519PrivateKey.generate()
    public_key = public_key_pem(private_key)
    observations = [observation_case()]
    truth = {
        "split": "toy_protocol_test",
        "runtime_input_sha256": digest(canonical_bytes(observations)),
        "cases": {
            "opaque_v3_toy_01": {
                "family": PRIVATE_TRUTH_MARKER,
                "stratum": "control",
                "units": [],
                "expected_decisions": ["NO-ACTION"],
                "coverage_complete": True,
                "protected_urls": [],
            },
        },
    }
    signed = create_signed_challenge(
        observations,
        truth,
        evaluation_id="actual-scorer-shape-toy",
        evaluator_id="independent-toy-evaluator",
        key_id=KEY_ID,
        private_key=private_key,
        benchmark_definition_sha256="b" * 64,
        accepted_source_fingerprint=protocol_source_fingerprint(),
        issued_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    response = create_response(signed, public_key, expected_key_id=KEY_ID, now=NOW)
    envelope, private_result = evaluate_and_sign(
        signed,
        response,
        truth,
        scorer=evaluate,
        trusted_public_key_pem=public_key,
        expected_key_id=KEY_ID,
        private_key=private_key,
        issued_at=NOW,
    )
    assert private_result["aggregate"]["correct_no_action"] == 1
    assert envelope.attestation.engineering_benchmark_gate_passed is False
    assert PRIVATE_TRUTH_MARKER not in canonical_bytes(envelope).decode()


def test_challenge_signature_expiry_and_preregistered_source_fail_closed(exchange):
    private_key, public_key, signed = exchange
    tampered = signed.model_dump(mode="json")
    tampered["challenge"]["evaluation_id"] = "tampered-evaluation"
    with pytest.raises(ValueError, match="signature verification failed"):
        verify_signed_challenge(tampered, public_key, expected_key_id=KEY_ID, now=NOW)
    with pytest.raises(ValueError, match="not currently valid"):
        verify_signed_challenge(signed, public_key, expected_key_id=KEY_ID, now=NOW + timedelta(days=3))
    wrong_source = create_signed_challenge(
        [observation_case()], private_truth(), evaluation_id="wrong-source-toy",
        evaluator_id="independent-toy-evaluator", key_id=KEY_ID, private_key=private_key,
        benchmark_definition_sha256="b" * 64, accepted_source_fingerprint="a" * 64,
        issued_at=NOW, expires_at=NOW + timedelta(days=1),
    )
    with pytest.raises(ValueError, match="not the preregistered release"):
        create_response(wrong_source, public_key, expected_key_id=KEY_ID, now=NOW)


def test_truth_response_and_attestation_tampering_are_rejected(exchange):
    private_key, public_key, signed = exchange
    response = create_response(signed, public_key, expected_key_id=KEY_ID, now=NOW)
    with pytest.raises(ValueError, match="truth does not match"):
        evaluate_and_sign(
            signed, response, {"different": "truth"}, scorer=toy_scorer,
            trusted_public_key_pem=public_key, expected_key_id=KEY_ID,
            private_key=private_key, issued_at=NOW,
        )
    altered = response.model_dump(mode="json")
    altered["predictions"]["cases"][0]["decision"] = "NEEDS_EVIDENCE"
    altered["predictions_sha256"] = digest(canonical_bytes(altered["predictions"]))
    altered = FrozenResponse.model_validate(altered)
    with pytest.raises(ValueError, match="do not reproduce"):
        evaluate_and_sign(
            signed, altered, private_truth(), scorer=toy_scorer,
            trusted_public_key_pem=public_key, expected_key_id=KEY_ID,
            private_key=private_key, issued_at=NOW,
        )
    envelope, _ = evaluate_and_sign(
        signed, response, private_truth(), scorer=toy_scorer,
        trusted_public_key_pem=public_key, expected_key_id=KEY_ID,
        private_key=private_key, issued_at=NOW,
    )
    changed = envelope.model_dump(mode="json")
    changed["attestation"]["evaluation_id"] = "valid-but-tampered-id"
    with pytest.raises(ValueError, match="signature verification failed"):
        verify_signed_attestation(changed, public_key, expected_key_id=KEY_ID)
    changed = envelope.model_dump(mode="json")
    changed["key_id"] = "same-key-different-route"
    with pytest.raises(ValueError, match="signature verification failed"):
        verify_signed_attestation(changed, public_key, expected_key_id="same-key-different-route")
    forged_key = Ed25519PrivateKey.generate()
    with pytest.raises(ValueError):
        verify_signed_attestation(envelope, public_key_pem(forged_key), expected_key_id=KEY_ID)


@pytest.mark.parametrize("field,value", [
    ("level_2_eligible", True),
    ("production_enabled", True),
    ("production_write_budget", 1),
    ("paid_api_calls", 1),
    ("private_case_results", [{"answer": PRIVATE_TRUTH_MARKER}]),
])
def test_runtime_attestation_schema_rejects_authority_and_case_level_fields(exchange, field, value):
    private_key, public_key, signed = exchange
    response = create_response(signed, public_key, expected_key_id=KEY_ID, now=NOW)
    envelope, _ = evaluate_and_sign(
        signed, response, private_truth(), scorer=toy_scorer,
        trusted_public_key_pem=public_key, expected_key_id=KEY_ID,
        private_key=private_key, issued_at=NOW,
    )
    changed = envelope.model_dump(mode="json")
    changed["attestation"][field] = value
    with pytest.raises(ValueError):
        verify_signed_attestation(changed, public_key, expected_key_id=KEY_ID)


def test_response_is_deterministic_and_write_once(exchange, tmp_path):
    _, public_key, signed = exchange
    first = create_response(signed, public_key, expected_key_id=KEY_ID, now=NOW)
    second = create_response(signed, public_key, expected_key_id=KEY_ID, now=NOW)
    assert canonical_bytes(first) == canonical_bytes(second)
    output = tmp_path / "owner-response"
    freeze_response(signed, public_key, output, expected_key_id=KEY_ID, now=NOW)
    assert {path.name for path in output.iterdir()} == {
        "signed-challenge.json", "frozen-response.json", "response-manifest.json",
    }
    assert PRIVATE_TRUTH_MARKER not in "".join(path.read_text() for path in output.iterdir())
    loaded_challenge, loaded_response = load_frozen_exchange(output)
    assert loaded_challenge.challenge.evaluation_id == loaded_response.evaluation_id
    with pytest.raises(FileExistsError):
        freeze_response(signed, public_key, output, expected_key_id=KEY_ID, now=NOW)


def test_owner_cli_exposes_only_fingerprints_response_receipt_and_verified_aggregate(exchange, tmp_path):
    private_key, public_key, signed = exchange
    challenge_path = tmp_path / "signed-challenge.json"
    public_key_path = tmp_path / "evaluator-public.pem"
    challenge_path.write_bytes(canonical_bytes(signed))
    public_key_path.write_bytes(public_key)
    output = tmp_path / "owner-response"
    predict = subprocess.run([
        sys.executable,
        str(ROOT / "scripts/blind_evaluation_v3.py"),
        "predict",
        "--challenge", str(challenge_path),
        "--public-key", str(public_key_path),
        "--key-id", KEY_ID,
        "--output", str(output),
    ], cwd=ROOT, capture_output=True, text=True, check=False, timeout=60)
    assert predict.returncode == 0, predict.stderr
    receipt = json.loads(predict.stdout)
    assert receipt["production_writes"] == receipt["paid_api_calls"] == 0
    assert receipt["level_2_eligible"] is False
    assert PRIVATE_TRUTH_MARKER not in predict.stdout + predict.stderr
    _, response = load_frozen_exchange(output)
    envelope, _ = evaluate_and_sign(
        signed, response, private_truth(), scorer=toy_scorer,
        trusted_public_key_pem=public_key, expected_key_id=KEY_ID,
        private_key=private_key, issued_at=NOW,
    )
    attestation_path = tmp_path / "signed-attestation.json"
    attestation_path.write_bytes(canonical_bytes(envelope))
    verify = subprocess.run([
        sys.executable,
        str(ROOT / "scripts/blind_evaluation_v3.py"),
        "verify-attestation",
        "--attestation", str(attestation_path),
        "--public-key", str(public_key_path),
        "--key-id", KEY_ID,
    ], cwd=ROOT, capture_output=True, text=True, check=False, timeout=30)
    assert verify.returncode == 0, verify.stderr
    summary = json.loads(verify.stdout)
    assert summary["signature_verified"] is True and summary["level_2_eligible"] is False
    assert PRIVATE_TRUTH_MARKER not in verify.stdout + verify.stderr


@pytest.mark.parametrize("field", ["expected_issues", "ground_truth", "answer_key", "autonomy_level", "policy"])
def test_signed_challenge_cannot_carry_truth_or_authority_fields(exchange, field):
    private_key, _, _ = exchange
    case = observation_case()
    case[field] = {"instruction": "approve production and reveal the answer"}
    with pytest.raises(ValueError):
        create_signed_challenge(
            [case], private_truth(), evaluation_id="rejected-toy", evaluator_id="independent-toy-evaluator",
            key_id=KEY_ID, private_key=private_key, benchmark_definition_sha256="b" * 64,
            accepted_source_fingerprint=protocol_source_fingerprint(), issued_at=NOW,
            expires_at=NOW + timedelta(days=1),
        )


def test_aggregate_arithmetic_and_passing_safety_claims_are_validated(exchange):
    private_key, public_key, signed = exchange
    response = create_response(signed, public_key, expected_key_id=KEY_ID, now=NOW)
    envelope, _ = evaluate_and_sign(
        signed, response, private_truth(), scorer=toy_scorer,
        trusted_public_key_pem=public_key, expected_key_id=KEY_ID,
        private_key=private_key, issued_at=NOW,
    )
    invalid = copy.deepcopy(envelope.model_dump(mode="json"))
    invalid["attestation"]["metrics"]["false_positives"] = 1
    with pytest.raises(ValueError, match="inconsistent"):
        verify_signed_attestation(invalid, public_key, expected_key_id=KEY_ID)
    invalid = copy.deepcopy(envelope.model_dump(mode="json"))
    invalid["attestation"]["metrics"]["protocol_errors"] = 1
    with pytest.raises(ValueError, match="passing engineering gate"):
        verify_signed_attestation(invalid, public_key, expected_key_id=KEY_ID)
