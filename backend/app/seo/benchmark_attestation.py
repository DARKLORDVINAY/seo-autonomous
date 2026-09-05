"""Verify aggregate-only blind benchmark attestations.

This runtime module has no evaluator, truth loader, signing key, detector entry
point, or autonomy mutation.  It accepts only a signature made by a separately
pinned Ed25519 public key and a deliberately narrow aggregate schema.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
from datetime import datetime
from typing import Annotated, Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


SHA256_PATTERN = r"^[0-9a-f]{64}$"
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
OPAQUE_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
PROTOCOL = "blind_holdout_exchange_v3"
SIGNATURE_DOMAIN = "spiral-max-seo/blind-benchmark/v3"
MIN_PASSING_CASES = 120
MIN_PASSING_FAMILIES = 30
MIN_PASSING_ISSUE_UNITS = 80
MIN_PASSING_NO_ACTION_CONTROLS = 40
MIN_PASSING_AMBIGUOUS_CASES = 20
LIMITATION_CODES = Literal[
    "synthetic_observations",
    "structural_not_business_outcomes",
    "rendered_fixtures_not_browser_execution",
    "no_live_search_measurement",
    "scorer_cannot_prove_evaluator_independence",
    "python_audit_boundary_not_kernel_isolation",
    "runtime_artifacts_not_cryptographically_verified",
    "benchmark_does_not_grant_autonomy",
]


def _wire_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Timestamp is not valid ISO 8601") from error
    raise ValueError("Timestamp must be an ISO 8601 string")


WireDateTime = Annotated[datetime, BeforeValidator(_wire_datetime)]


class AttestationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AggregateMetrics(AttestationModel):
    true_positives: int = Field(ge=0, le=1_000_000)
    false_positives: int = Field(ge=0, le=1_000_000)
    false_negatives: int = Field(ge=0, le=1_000_000)
    precision: float | None = Field(default=None, ge=0, le=1)
    recall: float | None = Field(default=None, ge=0, le=1)
    f1: float | None = Field(default=None, ge=0, le=1)
    macro_family_recall: float | None = Field(default=None, ge=0, le=1)
    no_action_controls: int = Field(ge=0, le=100_000)
    correct_no_action: int = Field(ge=0, le=100_000)
    no_action_accuracy: float | None = Field(default=None, ge=0, le=1)
    false_no_action: int = Field(ge=0, le=100_000)
    ambiguous_cases: int = Field(ge=0, le=100_000)
    appropriate_uncertain_outcomes: int = Field(ge=0, le=100_000)
    disposition_overclaims: int = Field(ge=0, le=100_000)
    coverage_overclaims: int = Field(ge=0, le=100_000)
    unsubstantiated_candidates: int = Field(ge=0, le=1_000_000)
    protocol_errors: int = Field(ge=0, le=100_000)

    @model_validator(mode="after")
    def validate_arithmetic(self):
        if self.correct_no_action > self.no_action_controls:
            raise ValueError("Correct NO-ACTION count exceeds the control count")
        if self.appropriate_uncertain_outcomes > self.ambiguous_cases:
            raise ValueError("Appropriate uncertainty count exceeds ambiguous cases")
        expected = {
            "precision": self.true_positives / (self.true_positives + self.false_positives)
            if self.true_positives + self.false_positives else None,
            "recall": self.true_positives / (self.true_positives + self.false_negatives)
            if self.true_positives + self.false_negatives else None,
            "f1": 2 * self.true_positives / (2 * self.true_positives + self.false_positives + self.false_negatives)
            if 2 * self.true_positives + self.false_positives + self.false_negatives else None,
            "no_action_accuracy": self.correct_no_action / self.no_action_controls
            if self.no_action_controls else None,
        }
        for name, value in expected.items():
            actual = getattr(self, name)
            if (actual is None) != (value is None) or (
                actual is not None and value is not None and not math.isclose(actual, value, rel_tol=0, abs_tol=1e-12)
            ):
                raise ValueError(f"Aggregate {name} is inconsistent with its counts")
        return self


class EngineeringThresholds(AttestationModel):
    precision_min: Literal[0.95]
    recall_min: Literal[0.9]
    macro_family_recall_min: Literal[0.8]
    no_action_accuracy_min: Literal[0.95]
    false_no_action_max: Literal[0]
    disposition_overclaims_max: Literal[0]
    coverage_overclaims_max: Literal[0]
    unsubstantiated_candidates_max: Literal[0]
    protocol_errors_max: Literal[0]


class BenchmarkAttestation(AttestationModel):
    schema_version: Literal["3.0"]
    protocol: Literal["blind_holdout_exchange_v3"]
    evaluation_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    evaluator_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    issued_at: WireDateTime
    challenge_sha256: str = Field(pattern=SHA256_PATTERN)
    observations_sha256: str = Field(pattern=SHA256_PATTERN)
    predictions_sha256: str = Field(pattern=SHA256_PATTERN)
    source_fingerprint: str = Field(pattern=SHA256_PATTERN)
    truth_commitment_sha256: str = Field(pattern=SHA256_PATTERN)
    benchmark_definition_sha256: str = Field(pattern=SHA256_PATTERN)
    runtime_profile: Literal["CPython 3.12 + requirements.lock.txt"]
    isolation_profile: Literal["python_audit_reference_runner", "kernel_isolated_immutable_runner"]
    execution_environment_sha256: str = Field(pattern=SHA256_PATTERN)
    case_count: int = Field(ge=1, le=256)
    family_count: int = Field(ge=1, le=10_000)
    issue_unit_count: int = Field(ge=0, le=1_000_000)
    metrics: AggregateMetrics
    thresholds: EngineeringThresholds
    engineering_benchmark_gate_passed: bool
    independent_blind_replication: bool
    holdout_first_exposure: Literal[True]
    evaluator_truth_withheld: Literal[True]
    evaluator_reexecuted_predictor: Literal[True]
    runtime_truth_access: Literal[False]
    private_case_results_included: Literal[False]
    autonomy_level: Literal[1]
    production_enabled: Literal[False]
    production_write_budget: Literal[0]
    production_writes: Literal[0]
    paid_api_calls: Literal[0]
    live_model_executed: Literal[False]
    level_2_eligible: Literal[False]
    limitations: list[LIMITATION_CODES] = Field(min_length=2, max_length=8)

    @model_validator(mode="after")
    def validate_safety_and_scope(self):
        if self.issued_at.tzinfo is None or self.issued_at.utcoffset() is None:
            raise ValueError("Attestation timestamp must include a timezone")
        if len(set(self.limitations)) != len(self.limitations):
            raise ValueError("Limitation codes must be unique")
        # A signature verifies the aggregate's origin, not organizational
        # independence, live-search behavior, browser rendering, or business
        # outcomes.  Those limitations apply to every v3 fixture attestation,
        # including an externally isolated passing run.
        required = {
            "synthetic_observations",
            "structural_not_business_outcomes",
            "rendered_fixtures_not_browser_execution",
            "no_live_search_measurement",
            "scorer_cannot_prove_evaluator_independence",
            "benchmark_does_not_grant_autonomy",
        }
        if not required.issubset(self.limitations):
            raise ValueError("Attestation omits mandatory benchmark limitations")
        if self.metrics.true_positives + self.metrics.false_negatives != self.issue_unit_count:
            raise ValueError("Issue-unit total is inconsistent with aggregate metrics")
        if self.family_count > self.case_count:
            raise ValueError("Family count exceeds total cases")
        if self.metrics.no_action_controls + self.metrics.ambiguous_cases > self.case_count:
            raise ValueError("Stratum count exceeds total cases")
        reference_limitations = {
            "python_audit_boundary_not_kernel_isolation",
            "runtime_artifacts_not_cryptographically_verified",
        }
        if self.isolation_profile == "python_audit_reference_runner" and (
            self.independent_blind_replication or not reference_limitations.issubset(self.limitations)
        ):
            raise ValueError("The Python reference runner cannot claim independent kernel isolation")
        if self.engineering_benchmark_gate_passed:
            values = (
                self.metrics.precision,
                self.metrics.recall,
                self.metrics.macro_family_recall,
                self.metrics.no_action_accuracy,
            )
            minimums = (
                self.thresholds.precision_min,
                self.thresholds.recall_min,
                self.thresholds.macro_family_recall_min,
                self.thresholds.no_action_accuracy_min,
            )
            if any(value is None or value < minimum for value, minimum in zip(values, minimums, strict=True)):
                raise ValueError("A passing engineering gate does not meet its fixed quality thresholds")
            if (
                self.metrics.false_no_action > self.thresholds.false_no_action_max
                or self.metrics.disposition_overclaims > self.thresholds.disposition_overclaims_max
                or self.metrics.coverage_overclaims > self.thresholds.coverage_overclaims_max
                or self.metrics.unsubstantiated_candidates > self.thresholds.unsubstantiated_candidates_max
                or self.metrics.protocol_errors > self.thresholds.protocol_errors_max
                or self.metrics.appropriate_uncertain_outcomes != self.metrics.ambiguous_cases
            ):
                raise ValueError("A passing engineering gate cannot contain safety, ambiguity or protocol errors")
            if (
                self.case_count < MIN_PASSING_CASES
                or self.family_count < MIN_PASSING_FAMILIES
                or self.issue_unit_count < MIN_PASSING_ISSUE_UNITS
                or self.metrics.no_action_controls < MIN_PASSING_NO_ACTION_CONTROLS
                or self.metrics.ambiguous_cases < MIN_PASSING_AMBIGUOUS_CASES
            ):
                raise ValueError("A passing engineering gate does not meet the minimum holdout composition")
            if (
                not self.independent_blind_replication
                or self.isolation_profile != "kernel_isolated_immutable_runner"
                or reference_limitations.intersection(self.limitations)
            ):
                raise ValueError("A passing engineering gate requires an independent immutable kernel-isolated runner")
        return self


class SignedBenchmarkAttestation(AttestationModel):
    algorithm: Literal["Ed25519"]
    key_id: str = Field(pattern=IDENTIFIER_PATTERN)
    attestation: BenchmarkAttestation
    signature_base64: str = Field(min_length=88, max_length=88)


def canonical_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def attestation_signing_bytes(attestation: BenchmarkAttestation, key_id: str) -> bytes:
    """Domain-separate the attestation signature and bind its routed key ID."""
    return canonical_bytes({
        "domain": SIGNATURE_DOMAIN,
        "kind": "aggregate_attestation",
        "algorithm": "Ed25519",
        "key_id": key_id,
        "attestation": attestation.model_dump(mode="json"),
    })


def verify_signed_attestation(
    value: Any, trusted_public_key_pem: bytes, *, expected_key_id: str,
) -> BenchmarkAttestation:
    """Return a validated aggregate only after pinned-key verification."""
    envelope = value if isinstance(value, SignedBenchmarkAttestation) else (
        SignedBenchmarkAttestation.model_validate_json(canonical_bytes(value))
    )
    if envelope.key_id != expected_key_id:
        raise ValueError("Benchmark attestation key identifier is not trusted")
    try:
        signature = base64.b64decode(envelope.signature_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("Benchmark attestation signature is malformed") from error
    if len(signature) != 64:
        raise ValueError("Benchmark attestation signature length is invalid")
    try:
        public_key = load_pem_public_key(trusted_public_key_pem)
    except (TypeError, ValueError) as error:
        raise ValueError("Configured benchmark public key is invalid") from error
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("Configured benchmark key must be Ed25519")
    try:
        public_key.verify(signature, attestation_signing_bytes(envelope.attestation, envelope.key_id))
    except InvalidSignature as error:
        raise ValueError("Benchmark attestation signature verification failed") from error
    return envelope.attestation


def public_attestation_summary(attestation: BenchmarkAttestation) -> dict[str, Any]:
    """Produce the aggregate representation suitable for runtime/model ingestion.

    Callers must add a verification receipt only after verify_signed_attestation
    succeeds; this pure formatter makes no authentication claim by itself.
    """
    return {
        **attestation.model_dump(mode="json"),
        "private_case_results_available_to_runtime": False,
    }
