"""Provider-neutral, evaluator-signed blind benchmark exchange.

The independent evaluator creates and signs a challenge while retaining truth.
The project owner can only run the observation-only detector against that signed
challenge.  The evaluator signs an aggregate attestation and never returns
case-level truth or scoring rows to the runtime control plane.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import inspect
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Callable, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_public_key
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.seo.benchmark_attestation import (
    AggregateMetrics,
    BenchmarkAttestation,
    EngineeringThresholds,
    IDENTIFIER_PATTERN,
    OPAQUE_ID_PATTERN,
    PROTOCOL,
    SHA256_PATTERN,
    SIGNATURE_DOMAIN,
    SignedBenchmarkAttestation,
    WireDateTime,
    attestation_signing_bytes,
    canonical_bytes,
)
ROOT = Path(__file__).resolve().parents[1]
MAX_CASES = 256
MAX_PAGES_PER_CASE = 64
MAX_ROWS_PER_CASE = 512
MAX_CASE_BYTES = 2 * 1024 * 1024
CASE_FIELDS = frozenset({"case_id", "crawls", "context", "gsc_rows", "ga4_rows", "rendered_crawls"})
CONTEXT_FIELDS = frozenset({
    "site_url", "inventory_urls", "inventory_complete", "crawl_coverage_complete",
    "entrypoint_urls", "sitemap_urls", "sitemap_complete", "intended_indexable_urls",
    "page_purposes", "thin_content_word_threshold",
})
RESERVED_OBSERVATION_KEYS = frozenset({
    "answer_key", "answers", "autonomy_level", "benchmark_labels", "budget", "budgets",
    "clean_control_pages", "expected_decisions", "expected_issues", "expected_outcomes",
    "family", "ground_truth", "guardrail", "guardrails", "label", "labels", "level_2_eligible",
    "paid_api_calls", "policy", "private_case_results", "private_label", "production_enabled",
    "production_write_budget", "production_writes", "scoring_key", "stratum", "system_prompt",
    "tool_permissions", "truth",
})
RESERVED_OBSERVATION_KEY_TOKENS = frozenset(
    re.sub(r"[^a-z0-9]+", "", key.casefold()) for key in RESERVED_OBSERVATION_KEYS
)
MAX_STRUCTURED_OBSERVATION_NODES = 100_000
RUNTIME_SOURCE_PATHS = (
    "backend/__init__.py",
    "backend/app/__init__.py",
    "backend/app/contracts.py",
    "backend/app/seo/__init__.py",
    "backend/app/seo/analysis.py",
    "backend/app/seo/benchmark_runtime.py",
)
PROTOCOL_SOURCE_PATHS = (
    *RUNTIME_SOURCE_PATHS,
    "pyproject.toml",
    "requirements.lock.txt",
    "backend/app/seo/benchmark_attestation.py",
    "benchmarks/isolated_worker.py",
    "benchmarks/blind_exchange_v3.py",
    "scripts/blind_evaluation_v3.py",
)
MAX_CHALLENGE_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_STDERR_BYTES = 1024 * 1024
MAX_DEFINITION_BYTES = 1024 * 1024
TRUTH_COMMITMENT_DOMAIN = b"spiral-max-seo/blind-benchmark/v3/private-truth\0"
SAFETY_CONTRACT = {
    "autonomy_level": 1,
    "production_enabled": False,
    "production_write_budget": 0,
    "production_writes": 0,
    "paid_api_calls": 0,
    "live_model_executed": False,
    "level_2_eligible": False,
}


def _reject_reserved_observation_keys(value: Any) -> None:
    pending = [value]
    nodes = 0
    while pending:
        current = pending.pop()
        nodes += 1
        if nodes > MAX_STRUCTURED_OBSERVATION_NODES:
            raise ValueError("Structured observation node budget exceeded")
        if isinstance(current, dict):
            for key, nested in current.items():
                if not isinstance(key, str):
                    raise ValueError("Structured observation keys must be strings")
                normalized = re.sub(r"[^a-z0-9]+", "", key.casefold())
                if normalized in RESERVED_OBSERVATION_KEY_TOKENS:
                    raise ValueError("Observation contains an evaluator-private or authority key")
                pending.append(nested)
        elif isinstance(current, list):
            pending.extend(current)


def validate_observation_cases(cases: Any) -> list[dict[str, Any]]:
    """Evaluator-side envelope validation with no detector imports."""
    if not isinstance(cases, list) or not 0 < len(cases) <= MAX_CASES:
        raise ValueError("Case collection budget exceeded")
    identifiers = []
    for case in cases:
        if not isinstance(case, dict) or set(case) - CASE_FIELDS:
            raise ValueError("Only observation fields are accepted")
        identifier = case.get("case_id")
        if not isinstance(identifier, str) or not re.fullmatch(OPAQUE_ID_PATTERN, identifier):
            raise ValueError("Blind case identifiers must be opaque UUIDv4 values")
        identifiers.append(identifier)
        raw = canonical_bytes(case)
        if len(raw) > MAX_CASE_BYTES:
            raise ValueError("Case byte budget exceeded")
        context = case.get("context", {})
        if not isinstance(context, dict) or set(context) - CONTEXT_FIELDS:
            raise ValueError("Context cannot carry policy, budgets, labels or evaluator data")
        for name in ("crawls", "rendered_crawls", "gsc_rows", "ga4_rows"):
            items = case.get(name, [])
            limit = MAX_PAGES_PER_CASE if "crawl" in name else MAX_ROWS_PER_CASE
            if not isinstance(items, list) or len(items) > limit:
                raise ValueError("Observation budget exceeded")
        _reject_reserved_observation_keys(case)
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Case identifiers must be unique")
    return cases


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def truth_commitment(truth: Any, secret: bytes) -> str:
    """Commit private truth without exposing guessable label digests."""
    if not isinstance(secret, bytes) or not 32 <= len(secret) <= 128:
        raise ValueError("Truth commitment secret must contain 32-128 private bytes")
    return hmac.new(secret, TRUTH_COMMITMENT_DOMAIN + canonical_bytes(truth), hashlib.sha256).hexdigest()


def scorer_source_fingerprint(scorer: Callable[[dict, dict], dict]) -> str:
    """Bind a scorer's module bytes and callable identity without exposing them."""
    if (not inspect.isfunction(scorer) or inspect.unwrap(scorer) is not scorer
            or scorer.__closure__ is not None or scorer.__defaults__ or scorer.__kwdefaults__):
        raise ValueError("Benchmark scorer must be an undecorated module-level function without mutable bindings")
    source_file = inspect.getsourcefile(scorer)
    source_text = inspect.getsource(scorer)
    module = getattr(scorer, "__module__", None)
    qualname = getattr(scorer, "__qualname__", None)
    if not source_file or not isinstance(module, str) or not isinstance(qualname, str):
        raise ValueError("Benchmark scorer must be a source-backed Python callable")
    path = Path(source_file)
    if not path.is_file() or path.stat().st_size > MAX_DEFINITION_BYTES:
        raise ValueError("Benchmark scorer source is missing or exceeds the definition budget")
    return digest(canonical_bytes({
        "module": module,
        "qualname": qualname,
        "module_sha256": digest(path.read_bytes()),
        "callable_source_sha256": digest(source_text.encode()),
    }))


def benchmark_definition_digest(
    definition: dict[str, Any], scorer: Callable[[dict, dict], dict],
) -> str:
    """Hash the public scoring definition together with exact scorer source."""
    if not isinstance(definition, dict) or not definition or "scorer_source_fingerprint" in definition:
        raise ValueError("Benchmark definition must be a nonempty object without reserved fields")
    bound = {**definition, "scorer_source_fingerprint": scorer_source_fingerprint(scorer)}
    raw = canonical_bytes(bound)
    if len(raw) > MAX_DEFINITION_BYTES:
        raise ValueError("Benchmark definition exceeds its byte budget")
    return digest(raw)


def protocol_source_snapshot() -> dict[str, bytes]:
    return {path: (ROOT / path).read_bytes() for path in PROTOCOL_SOURCE_PATHS}


def source_snapshot_hashes(snapshot: dict[str, bytes]) -> dict[str, str]:
    if set(snapshot) != set(PROTOCOL_SOURCE_PATHS):
        raise ValueError("Protocol source snapshot is incomplete")
    return {path: digest(snapshot[path]) for path in PROTOCOL_SOURCE_PATHS}


def protocol_source_hashes() -> dict[str, str]:
    return source_snapshot_hashes(protocol_source_snapshot())


def protocol_source_fingerprint() -> str:
    return digest(canonical_bytes(protocol_source_hashes()))


class ExchangeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BlindChallenge(ExchangeModel):
    schema_version: Literal["3.0"]
    protocol: Literal["blind_holdout_exchange_v3"]
    evaluation_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    evaluator_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    issued_at: WireDateTime
    expires_at: WireDateTime
    observations: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_CASES)
    observations_sha256: str = Field(pattern=SHA256_PATTERN)
    truth_commitment_sha256: str = Field(pattern=SHA256_PATTERN)
    benchmark_definition_sha256: str = Field(pattern=SHA256_PATTERN)
    accepted_source_fingerprint: str = Field(pattern=SHA256_PATTERN)
    runtime_profile: Literal["CPython 3.12 + requirements.lock.txt"]
    case_count: int = Field(ge=1, le=MAX_CASES)
    independent_evaluator: Literal[True]
    holdout_first_exposure: Literal[True]
    truth_withheld_from_predictor: Literal[True]
    level_2_eligible: Literal[False]

    @model_validator(mode="after")
    def validate_commitments(self):
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("Challenge timestamps must include timezones")
        lifetime = self.expires_at - self.issued_at
        if lifetime <= timedelta(0) or lifetime > timedelta(days=30):
            raise ValueError("Challenge lifetime must be positive and at most 30 days")
        if self.case_count != len(self.observations):
            raise ValueError("Challenge case count is inconsistent")
        validate_observation_cases(self.observations)
        if any(not re.fullmatch(OPAQUE_ID_PATTERN, case["case_id"]) for case in self.observations):
            raise ValueError("Blind case identifiers must be opaque UUIDv4 values")
        if self.observations_sha256 != digest(canonical_bytes(self.observations)):
            raise ValueError("Challenge observation commitment is inconsistent")
        if len(canonical_bytes(self)) > MAX_CHALLENGE_BYTES:
            raise ValueError("Signed challenge exceeds the input budget")
        return self


class SignedBlindChallenge(ExchangeModel):
    algorithm: Literal["Ed25519"]
    key_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenge: BlindChallenge
    signature_base64: str = Field(min_length=88, max_length=88)


class FrozenResponse(ExchangeModel):
    schema_version: Literal["3.0"]
    protocol: Literal["blind_holdout_exchange_v3"]
    evaluation_id: str = Field(pattern=OPAQUE_ID_PATTERN)
    challenge_sha256: str = Field(pattern=SHA256_PATTERN)
    observations_sha256: str = Field(pattern=SHA256_PATTERN)
    source_fingerprint: str = Field(pattern=SHA256_PATTERN)
    predictions_sha256: str = Field(pattern=SHA256_PATTERN)
    predictions: dict[str, Any]
    autonomy_level: Literal[1]
    production_enabled: Literal[False]
    production_write_budget: Literal[0]
    production_writes: Literal[0]
    paid_api_calls: Literal[0]
    live_model_executed: Literal[False]
    level_2_eligible: Literal[False]

    @model_validator(mode="after")
    def validate_predictions(self):
        if self.predictions_sha256 != digest(canonical_bytes(self.predictions)):
            raise ValueError("Prediction commitment is inconsistent")
        required = {
            "autonomy_level": 1,
            "production_enabled": False,
            "production_write_budget": 0,
            "production_writes": 0,
            "paid_api_calls": 0,
            "live_model_executed": False,
            "level_2_eligible": False,
        }
        if any(self.predictions.get(name) != expected for name, expected in required.items()):
            raise ValueError("Prediction packet violates the hard-zero authority contract")
        metadata = self.predictions.get("metadata")
        if not isinstance(metadata, dict) or any(metadata.get(name) != expected for name, expected in {
            **required,
            "input_sha256": self.observations_sha256,
            "source_fingerprint": self.source_fingerprint,
        }.items()):
            raise ValueError("Prediction metadata is not bound to the challenge and safety contract")
        if len(canonical_bytes(self)) > MAX_RESPONSE_BYTES:
            raise ValueError("Frozen response exceeds the output budget")
        return self


def challenge_signing_bytes(challenge: BlindChallenge, key_id: str) -> bytes:
    return canonical_bytes({
        "domain": SIGNATURE_DOMAIN,
        "kind": "blind_challenge",
        "algorithm": "Ed25519",
        "key_id": key_id,
        "challenge": challenge.model_dump(mode="json"),
    })


def _signature(message: bytes, private_key: Ed25519PrivateKey) -> str:
    return base64.b64encode(private_key.sign(message)).decode("ascii")


def _verify_signature(message: bytes, signature_base64: str, public_key: Ed25519PublicKey) -> None:
    try:
        signature = base64.b64decode(signature_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("Evaluator signature is malformed") from error
    if len(signature) != 64:
        raise ValueError("Evaluator signature length is invalid")
    try:
        public_key.verify(signature, message)
    except InvalidSignature as error:
        raise ValueError("Evaluator signature verification failed") from error


def public_key_pem(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)


def _trusted_key(public_key_pem_bytes: bytes) -> Ed25519PublicKey:
    try:
        key = load_pem_public_key(public_key_pem_bytes)
    except (TypeError, ValueError) as error:
        raise ValueError("Configured evaluator public key is invalid") from error
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Configured evaluator key must be Ed25519")
    return key


def create_signed_challenge(
    observations: list[dict[str, Any]], truth: Any, *, evaluation_id: str, evaluator_id: str,
    key_id: str, private_key: Ed25519PrivateKey, benchmark_definition: dict[str, Any],
    scorer: Callable[[dict, dict], dict],
    accepted_source_fingerprint: str, truth_commitment_secret: bytes,
    issued_at: datetime, expires_at: datetime,
) -> SignedBlindChallenge:
    """Evaluator-only helper: commit private truth without embedding it."""
    challenge = BlindChallenge(
        schema_version="3.0",
        protocol=PROTOCOL,
        evaluation_id=evaluation_id,
        evaluator_id=evaluator_id,
        issued_at=issued_at,
        expires_at=expires_at,
        observations=observations,
        observations_sha256=digest(canonical_bytes(observations)),
        truth_commitment_sha256=truth_commitment(truth, truth_commitment_secret),
        benchmark_definition_sha256=benchmark_definition_digest(benchmark_definition, scorer),
        accepted_source_fingerprint=accepted_source_fingerprint,
        runtime_profile="CPython 3.12 + requirements.lock.txt",
        case_count=len(observations),
        independent_evaluator=True,
        holdout_first_exposure=True,
        truth_withheld_from_predictor=True,
        level_2_eligible=False,
    )
    return SignedBlindChallenge(
        algorithm="Ed25519",
        key_id=key_id,
        challenge=challenge,
        signature_base64=_signature(challenge_signing_bytes(challenge, key_id), private_key),
    )


def verify_signed_challenge(
    value: Any, trusted_public_key_pem: bytes, *, expected_key_id: str,
    now: datetime | None = None, require_unexpired: bool = True,
) -> SignedBlindChallenge:
    envelope = value if isinstance(value, SignedBlindChallenge) else (
        SignedBlindChallenge.model_validate_json(canonical_bytes(value))
    )
    if envelope.key_id != expected_key_id:
        raise ValueError("Challenge key identifier is not trusted")
    _verify_signature(
        challenge_signing_bytes(envelope.challenge, envelope.key_id),
        envelope.signature_base64,
        _trusted_key(trusted_public_key_pem),
    )
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Verification time must include a timezone")
    if require_unexpired and not (envelope.challenge.issued_at <= current <= envelope.challenge.expires_at):
        raise ValueError("Signed challenge is not currently valid")
    return envelope


def _run_isolated(observations: list[dict[str, Any]], source_snapshot: dict[str, bytes]) -> dict[str, Any]:
    raw = canonical_bytes(observations)
    if len(raw) > MAX_CHALLENGE_BYTES:
        raise ValueError("Challenge observations exceed the input budget")
    with tempfile.TemporaryDirectory(prefix="seo-v3-runtime-") as folder:
        stage = Path(folder)
        for name in RUNTIME_SOURCE_PATHS:
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source_snapshot[name])
        (stage / "worker.py").write_bytes(source_snapshot["benchmarks/isolated_worker.py"])
        (stage / "observations.json").write_bytes(raw)
        stdout_path, stderr_path = stage / "worker.stdout", stage / "worker.stderr"
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            process = subprocess.run(
                [sys.executable, "-I", "-S", "-B", str(stage / "worker.py"), str(stage / "observations.json")],
                cwd=stage,
                env={"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC", "PYTHONHASHSEED": "0"},
                stdout=stdout,
                stderr=stderr,
                timeout=45,
                check=False,
            )
        if process.returncode:
            raise RuntimeError(f"Isolated predictor failed closed (exit {process.returncode})")
        if stdout_path.stat().st_size > MAX_RESPONSE_BYTES or stderr_path.stat().st_size > MAX_STDERR_BYTES:
            raise ValueError("Predictor output exceeds the response budget")
        output = stdout_path.read_bytes()
        try:
            return json.loads(output, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise ValueError("Isolated predictor emitted invalid JSON") from error


def _prediction_packet(challenge: BlindChallenge) -> tuple[dict[str, Any], str]:
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 12):
        raise ValueError("Blind evaluation requires the preregistered CPython 3.12 runtime")
    if challenge.runtime_profile != "CPython 3.12 + requirements.lock.txt":
        raise ValueError("Signed challenge runtime profile is unsupported")
    source_snapshot = protocol_source_snapshot()
    sources_before = source_snapshot_hashes(source_snapshot)
    fingerprint = digest(canonical_bytes(sources_before))
    if fingerprint != challenge.accepted_source_fingerprint:
        raise ValueError("Local detector/protocol source is not the preregistered release")
    packet = _run_isolated(challenge.observations, source_snapshot)
    if protocol_source_hashes() != sources_before:
        raise ValueError("Detector/protocol source changed during prediction")
    if any(packet.get(name) != expected for name, expected in SAFETY_CONTRACT.items()):
        raise ValueError("Isolated predictor did not preserve the hard-zero authority contract")
    packet = {
        **packet,
        "metadata": {
            **SAFETY_CONTRACT,
            "input_sha256": challenge.observations_sha256,
            "runtime_corpus_sha256": challenge.observations_sha256,
            "source_fingerprint": fingerprint,
        },
    }
    return packet, fingerprint


def create_response(
    signed_challenge: Any, trusted_public_key_pem: bytes, *, expected_key_id: str,
    now: datetime | None = None,
) -> FrozenResponse:
    envelope = verify_signed_challenge(
        signed_challenge, trusted_public_key_pem, expected_key_id=expected_key_id, now=now,
    )
    challenge = envelope.challenge
    packet, fingerprint = _prediction_packet(challenge)
    return FrozenResponse(
        schema_version="3.0",
        protocol=PROTOCOL,
        evaluation_id=challenge.evaluation_id,
        challenge_sha256=digest(canonical_bytes(envelope)),
        observations_sha256=challenge.observations_sha256,
        source_fingerprint=fingerprint,
        predictions_sha256=digest(canonical_bytes(packet)),
        predictions=packet,
        **SAFETY_CONTRACT,
    )


def freeze_response(
    signed_challenge: Any, trusted_public_key_pem: bytes, output: Path, *, expected_key_id: str,
    now: datetime | None = None,
) -> FrozenResponse:
    """Write-once owner response. No evaluator truth exists in this directory."""
    response = create_response(
        signed_challenge, trusted_public_key_pem, expected_key_id=expected_key_id, now=now,
    )
    envelope = signed_challenge if isinstance(signed_challenge, SignedBlindChallenge) else (
        SignedBlindChallenge.model_validate_json(canonical_bytes(signed_challenge))
    )
    output.mkdir(parents=True, exist_ok=False)
    os.chmod(output, 0o700)
    files = {
        "signed-challenge.json": canonical_bytes(envelope),
        "frozen-response.json": canonical_bytes(response),
    }
    manifest = {
        "schema_version": "3.0",
        "protocol": PROTOCOL,
        "evaluation_id": response.evaluation_id,
        "files": {name: digest(raw) for name, raw in files.items()},
        "contains_evaluator_truth": False,
        "contains_private_case_results": False,
        "level_2_eligible": False,
    }
    for name, raw in {**files, "response-manifest.json": canonical_bytes(manifest)}.items():
        with (output / name).open("xb") as handle:
            handle.write(raw)
        os.chmod(output / name, 0o400)
    return response


def load_frozen_exchange(output: Path) -> tuple[SignedBlindChallenge, FrozenResponse]:
    manifest = json.loads((output / "response-manifest.json").read_bytes())
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version", "protocol", "evaluation_id", "files",
        "contains_evaluator_truth", "contains_private_case_results", "level_2_eligible",
    }:
        raise ValueError("Frozen response manifest has unexpected fields")
    if manifest.get("schema_version") != "3.0" or manifest.get("protocol") != PROTOCOL:
        raise ValueError("Frozen response manifest version is invalid")
    if set(manifest.get("files", {})) != {"signed-challenge.json", "frozen-response.json"}:
        raise ValueError("Frozen response manifest is invalid")
    if (manifest.get("contains_evaluator_truth") is not False
            or manifest.get("contains_private_case_results") is not False
            or manifest.get("level_2_eligible") is not False):
        raise ValueError("Frozen response manifest violates the isolation contract")
    for name, expected in manifest["files"].items():
        if digest((output / name).read_bytes()) != expected:
            raise ValueError(f"Frozen exchange artifact changed: {name}")
    challenge = SignedBlindChallenge.model_validate_json((output / "signed-challenge.json").read_bytes())
    response = FrozenResponse.model_validate_json((output / "frozen-response.json").read_bytes())
    if (manifest.get("evaluation_id") != response.evaluation_id
            or challenge.challenge.evaluation_id != response.evaluation_id):
        raise ValueError("Frozen exchange evaluation identifiers differ")
    return challenge, response


def _aggregate_metrics(result: dict[str, Any]) -> AggregateMetrics:
    aggregate = result.get("aggregate")
    if not isinstance(aggregate, dict):
        raise ValueError("Evaluator did not return aggregate metrics")
    by_stratum = result.get("by_stratum", {})
    ambiguous = by_stratum.get("ambiguous", {}) if isinstance(by_stratum, dict) else {}
    protocol_errors = result.get("protocol_errors", [])
    if not isinstance(protocol_errors, list):
        raise ValueError("Evaluator protocol errors must be a list")
    fields = {
        "true_positives", "false_positives", "false_negatives", "precision", "recall", "f1",
        "no_action_controls", "correct_no_action", "false_no_action", "appropriate_uncertain_outcomes",
        "disposition_overclaims", "coverage_overclaims", "unsubstantiated_candidates",
    }
    if any(name not in aggregate for name in fields):
        raise ValueError("Evaluator aggregate is incomplete")
    return AggregateMetrics(
        **{name: aggregate[name] for name in fields},
        macro_family_recall=(result.get("macro_family", {}).get("recall")
                             if isinstance(result.get("macro_family"), dict) else None),
        no_action_accuracy=aggregate.get("no_action_accuracy"),
        ambiguous_cases=ambiguous.get("cases", 0),
        protocol_errors=len(protocol_errors),
    )


def evaluate_and_sign(
    signed_challenge: Any, response: Any, truth: Any, *, scorer: Callable[[dict, dict], dict],
    benchmark_definition: dict[str, Any],
    trusted_public_key_pem: bytes, expected_key_id: str, private_key: Ed25519PrivateKey,
    truth_commitment_secret: bytes, issued_at: datetime,
) -> tuple[SignedBenchmarkAttestation, dict[str, Any]]:
    """Evaluator-only operation; detailed results stay in evaluator custody."""
    challenge_envelope = verify_signed_challenge(
        signed_challenge,
        trusted_public_key_pem,
        expected_key_id=expected_key_id,
        now=issued_at,
    )
    challenge = challenge_envelope.challenge
    if issued_at.tzinfo is None or issued_at < challenge.issued_at:
        raise ValueError("Evaluation timestamp must follow the signed challenge")
    frozen = FrozenResponse.model_validate(response)
    bindings = {
        "evaluation_id": challenge.evaluation_id,
        "challenge_sha256": digest(canonical_bytes(challenge_envelope)),
        "observations_sha256": challenge.observations_sha256,
        "source_fingerprint": challenge.accepted_source_fingerprint,
    }
    if any(getattr(frozen, name) != expected for name, expected in bindings.items()):
        raise ValueError("Frozen response is not bound to the signed challenge")
    if truth_commitment(truth, truth_commitment_secret) != challenge.truth_commitment_sha256:
        raise ValueError("Private truth does not match its signed commitment")
    definition_before = benchmark_definition_digest(benchmark_definition, scorer)
    if definition_before != challenge.benchmark_definition_sha256:
        raise ValueError("Scorer or benchmark definition does not match the signed challenge")
    # The independent evaluator reruns the accepted source in the same minimal,
    # truth-blind child boundary and refuses owner-supplied prediction changes.
    expected_packet, expected_fingerprint = _prediction_packet(challenge)
    if (expected_fingerprint != frozen.source_fingerprint
            or canonical_bytes(expected_packet) != canonical_bytes(frozen.predictions)):
        raise ValueError("Frozen predictions do not reproduce from the accepted source")
    private_result = scorer(frozen.predictions, truth)
    if benchmark_definition_digest(benchmark_definition, scorer) != definition_before:
        raise ValueError("Scorer or benchmark definition changed during evaluation")
    if not isinstance(private_result, dict):
        raise ValueError("Evaluator returned an invalid private result")
    metrics = _aggregate_metrics(private_result)
    by_family = private_result.get("by_family")
    if not isinstance(by_family, dict) or not by_family:
        raise ValueError("Evaluator family aggregates are unavailable")
    attestation = BenchmarkAttestation(
        schema_version="3.0",
        protocol=PROTOCOL,
        evaluation_id=challenge.evaluation_id,
        evaluator_id=challenge.evaluator_id,
        issued_at=issued_at,
        challenge_sha256=frozen.challenge_sha256,
        observations_sha256=frozen.observations_sha256,
        predictions_sha256=frozen.predictions_sha256,
        source_fingerprint=frozen.source_fingerprint,
        truth_commitment_sha256=challenge.truth_commitment_sha256,
        benchmark_definition_sha256=challenge.benchmark_definition_sha256,
        runtime_profile=challenge.runtime_profile,
        isolation_profile="python_audit_reference_runner",
        execution_environment_sha256=digest(canonical_bytes({
            "profile": "python_audit_reference_runner",
            "runtime_profile": challenge.runtime_profile,
            "source_fingerprint": frozen.source_fingerprint,
        })),
        case_count=challenge.case_count,
        family_count=len(by_family),
        issue_unit_count=metrics.true_positives + metrics.false_negatives,
        metrics=metrics,
        thresholds=EngineeringThresholds(
            precision_min=0.95,
            recall_min=0.9,
            macro_family_recall_min=0.8,
            no_action_accuracy_min=0.95,
            false_no_action_max=0,
            disposition_overclaims_max=0,
            coverage_overclaims_max=0,
            unsubstantiated_candidates_max=0,
            protocol_errors_max=0,
        ),
        # This in-repository helper is a defense-in-depth reference runner, not
        # the external immutable kernel boundary required for a passing result.
        engineering_benchmark_gate_passed=False,
        independent_blind_replication=False,
        holdout_first_exposure=challenge.holdout_first_exposure,
        evaluator_truth_withheld=challenge.truth_withheld_from_predictor,
        evaluator_reexecuted_predictor=True,
        runtime_truth_access=False,
        private_case_results_included=False,
        autonomy_level=1,
        production_enabled=False,
        production_write_budget=0,
        production_writes=0,
        paid_api_calls=0,
        live_model_executed=False,
        level_2_eligible=False,
        limitations=[
            "synthetic_observations",
            "structural_not_business_outcomes",
            "rendered_fixtures_not_browser_execution",
            "no_live_search_measurement",
            "scorer_cannot_prove_evaluator_independence",
            "python_audit_boundary_not_kernel_isolation",
            "runtime_artifacts_not_cryptographically_verified",
            "benchmark_does_not_grant_autonomy",
        ],
    )
    envelope = SignedBenchmarkAttestation(
        algorithm="Ed25519",
        key_id=expected_key_id,
        attestation=attestation,
        signature_base64=_signature(attestation_signing_bytes(attestation, expected_key_id), private_key),
    )
    return envelope, private_result
