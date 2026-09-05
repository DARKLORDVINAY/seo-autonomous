"""Administrator-owned business facts with narrow schemas and append-only audit."""
from __future__ import annotations

from datetime import timedelta
import hashlib
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AfterValidator, BeforeValidator, Field, StringConstraints, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.api.auth import Principal, administrator
from backend.app.config.settings import Settings, get_settings
from backend.app.contracts import StrictModel, stable_hash, utcnow
from backend.app.db import models as m
from backend.app.db.session import get_session
from backend.app.integrations.google_analytics import GA4Client
from backend.app.seo.benchmark_attestation import (
    SignedBenchmarkAttestation,
    public_attestation_summary,
    verify_signed_attestation,
)
from backend.app.services import control


DB = Annotated[Session, Depends(get_session)]
Admin = Annotated[Principal, Depends(administrator)]
Config = Annotated[Settings, Depends(get_settings)]
router = APIRouter(prefix="/api/sites/{site_id}", tags=["Business configuration"])


def _exact_true(value):
    if value is not True:
        raise ValueError("An explicit true attestation is required")
    return value


def _nonblank(value: str) -> str:
    if not value.strip() or value != value.strip():
        raise ValueError("Text must be nonempty without surrounding whitespace")
    return value


Attested = Annotated[Literal[True], BeforeValidator(_exact_true)]
ShortText = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200), AfterValidator(_nonblank)]
Description = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2000), AfterValidator(_nonblank)]
Source = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2048), AfterValidator(_nonblank)]
Reason = Annotated[str, StringConstraints(strict=True, min_length=10, max_length=2000), AfterValidator(_nonblank)]
Amount = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ConfigurationKind = Literal["conversion_definition", "model_price_bound", "brand_facts"]


class ConversionDefinition(StrictModel):
    verified: Attested
    tracking_verified: Attested
    qualification_verified: Attested
    deduplication_verified: Attested
    qualified_events: list[ShortText] = Field(min_length=1, max_length=100)
    qualification_definition: Description
    deduplication_method: Description
    value_method: Literal["fixed_per_qualified_conversion", "event_value"]
    currency: str = Field(strict=True, pattern=r"^[A-Z]{3}$")
    value_per_conversion: Amount | None = None
    currency_verified: Attested | None = None
    value_semantics_verified: Attested | None = None

    @model_validator(mode="after")
    def verified_ga4_definition(self):
        # Validation only: no client construction, credentials or API request.
        GA4Client._definition(self.model_dump(exclude_none=True))
        return self


class ModelPriceBound(StrictModel):
    model: ShortText
    usd_per_million_tokens: float = Field(strict=True, gt=0, allow_inf_nan=False)
    verified: Attested
    source: Source

    @field_validator("source")
    @classmethod
    def official_pricing_source(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            valid = (parsed.scheme == "https" and parsed.hostname in {
                "openai.com", "www.openai.com", "platform.openai.com", "developers.openai.com", "help.openai.com",
            } and not parsed.username and not parsed.password and parsed.port in (None, 443) and not parsed.query)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Price source must be an HTTPS OpenAI page without credentials or query parameters")
        return value


class BrandFacts(StrictModel):
    brand_name: ShortText
    services: list[ShortText] = Field(max_length=50)
    service_areas: list[ShortText] = Field(max_length=50)
    source: Source
    reason: Reason

    @field_validator("services", "service_areas")
    @classmethod
    def distinct_facts(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("Facts must not contain duplicate entries")
        return values


class ConfigurationChangeReport(StrictModel):
    status: Literal["recorded"] = "recorded"
    configuration: ConfigurationKind
    site_id: UUID
    action_id: UUID
    evidence_id: UUID
    actor: Literal["site-administrator"]
    previous_hash: Digest | None
    configuration_hash: Digest
    evidence_hash: Digest
    changed: bool


class BenchmarkAttestationReport(StrictModel):
    status: Literal["recorded", "existing"]
    site_id: UUID
    evidence_id: UUID
    action_id: UUID | None
    evaluation_id: ShortText
    signature_verified: Literal[True]
    aggregate_only: Literal[True]
    engineering_benchmark_gate_passed: bool
    level_2_eligible: Literal[False]
    production_write: Literal[False]


def _registered_site(session: Session, site_id: UUID) -> m.Site:
    # Serialize configuration updates with model reservations and other site
    # writers; refreshing the locked row avoids merging stale config_json.
    site = session.scalar(select(m.Site).where(m.Site.id == str(site_id)).with_for_update()
                          .execution_options(populate_existing=True))
    if site is None:
        raise HTTPException(404, "Requested site does not exist")
    return site


def _record_change(session: Session, site: m.Site, user: Principal, kind: ConfigurationKind,
                   content: dict, *, source: str, reason: str, previous_hash: str | None) -> ConfigurationChangeReport:
    digest = stable_hash(content)
    evidence = m.Evidence(site_id=site.id, source=source, source_type=kind, content=content,
                          content_hash=digest, owner=user.actor, observed_at=utcnow(), confidence=1,
                          is_fixture=site.config_json.get("source_mode") == "fixture")
    session.add(evidence)
    session.flush()
    action = control.local_audit(session, site.id,
        "record_brand_facts" if kind == "brand_facts" else "configure_" + kind, user.actor, reason,
        {"configuration": kind, "previous_hash": previous_hash, "configuration_hash": digest,
         "evidence_id": evidence.id, "evidence_hash": digest})
    report = ConfigurationChangeReport(configuration=kind, site_id=site.id, action_id=action.id,
        evidence_id=evidence.id, actor=user.actor, previous_hash=previous_hash,
        configuration_hash=digest, evidence_hash=digest, changed=previous_hash != digest)
    # The site change, immutable evidence, action and event share one commit.
    session.commit()
    return report


@router.put("/conversion-definition", response_model=ConfigurationChangeReport)
def put_conversion_definition(site_id: UUID, body: ConversionDefinition, session: DB, user: Admin):
    site = _registered_site(session, site_id)
    previous_hash = stable_hash(site.conversion_definition)
    definition = body.model_dump(exclude_none=True)
    site.conversion_definition = definition
    return _record_change(session, site, user, "conversion_definition", definition,
        source="administrator:conversion-definition",
        reason="Administrator attested qualified conversion, deduplication and value semantics",
        previous_hash=previous_hash)


@router.put("/model-price-bound", response_model=ConfigurationChangeReport)
def put_model_price_bound(site_id: UUID, body: ModelPriceBound, session: DB, user: Admin, settings: Config):
    site = _registered_site(session, site_id)
    if not settings.openai_model:
        raise HTTPException(409, "Select OPENAI_MODEL before recording its price bound")
    if body.model != settings.openai_model:
        raise HTTPException(422, "Price bound model must match the configured OPENAI_MODEL")
    bound = body.model_dump()
    previous = site.config_json.get("model_price_bound")
    site.config_json = {**site.config_json, "model_price_bound": bound}
    return _record_change(session, site, user, "model_price_bound", bound, source=body.source,
        reason="Administrator verified a conservative price bound for the configured model",
        previous_hash=stable_hash(previous) if previous is not None else None)


@router.post("/brand-facts", status_code=201, response_model=ConfigurationChangeReport)
def post_brand_facts(site_id: UUID, body: BrandFacts, session: DB, user: Admin):
    site = _registered_site(session, site_id)
    # Only these manually supplied administrator fields become operator facts.
    # No existing external evidence, arbitrary content or trust label is accepted.
    facts = body.model_dump(include={"brand_name", "services", "service_areas"})
    return _record_change(session, site, user, "brand_facts", facts, source=body.source,
                          reason=body.reason, previous_hash=None)


@router.post("/benchmark-attestations", status_code=201, response_model=BenchmarkAttestationReport)
def import_benchmark_attestation(
    site_id: UUID, body: SignedBenchmarkAttestation, session: DB, user: Admin, settings: Config,
):
    """Persist a pinned-key-verified aggregate; never accept evaluator case data."""
    site = _registered_site(session, site_id)
    required = (
        settings.benchmark_evaluator_public_key_file,
        settings.benchmark_evaluator_key_id,
        settings.benchmark_expected_definition_sha256,
        settings.benchmark_expected_source_fingerprint,
    )
    if not all(required):
        raise HTTPException(409, "The pinned benchmark evaluator and expected release are not fully configured")
    key_path = Path(settings.benchmark_evaluator_public_key_file)
    try:
        if not key_path.is_file() or key_path.stat().st_size > 16_384:
            raise ValueError("invalid key file")
        public_key = key_path.read_bytes()
    except OSError as error:
        raise HTTPException(409, "The configured benchmark evaluator public key is unavailable") from error
    try:
        attestation = verify_signed_attestation(
            body, public_key, expected_key_id=settings.benchmark_evaluator_key_id,
        )
    except ValueError as error:
        raise HTTPException(422, "Benchmark attestation failed pinned-key or schema verification") from error
    if attestation.issued_at > utcnow() + timedelta(minutes=5):
        raise HTTPException(422, "Benchmark attestation timestamp is in the future")
    if (attestation.benchmark_definition_sha256 != settings.benchmark_expected_definition_sha256
            or attestation.source_fingerprint != settings.benchmark_expected_source_fingerprint):
        raise HTTPException(422, "Benchmark attestation does not match the preregistered definition and source release")
    content = {
        **public_attestation_summary(attestation),
        "signature_verified": True,
        "signature_receipt": {
            "algorithm": body.algorithm,
            "key_id": body.key_id,
            "signature_base64": body.signature_base64,
            "trusted_public_key_sha256": hashlib.sha256(public_key).hexdigest(),
        },
    }
    content_hash = stable_hash(content)
    source = f"benchmark_attestation:{attestation.evaluation_id}"
    existing = session.scalar(select(m.Evidence).where(
        m.Evidence.site_id == site.id,
        m.Evidence.source_type == "benchmark_attestation",
        m.Evidence.source == source,
    ).limit(1))
    if existing:
        if existing.content_hash != content_hash:
            raise HTTPException(409, "This evaluation identifier is already bound to another aggregate")
        return BenchmarkAttestationReport(
            status="existing",
            site_id=site.id,
            evidence_id=existing.id,
            action_id=None,
            evaluation_id=attestation.evaluation_id,
            signature_verified=True,
            aggregate_only=True,
            engineering_benchmark_gate_passed=attestation.engineering_benchmark_gate_passed,
            level_2_eligible=False,
            production_write=False,
        )
    evidence = m.Evidence(
        site_id=site.id,
        source_type="benchmark_attestation",
        source=source,
        content=content,
        content_hash=content_hash,
        owner=user.actor,
        observed_at=attestation.issued_at,
        confidence=1,
        is_fixture=True,
    )
    session.add(evidence)
    session.flush()
    action = control.local_audit(
        session,
        site.id,
        "import_benchmark_attestation",
        user.actor,
        "Record a pinned-key-verified aggregate benchmark attestation",
        {
            "evidence_id": evidence.id,
            "evidence_hash": content_hash,
            "evaluation_id": attestation.evaluation_id,
            "engineering_benchmark_gate_passed": attestation.engineering_benchmark_gate_passed,
            "aggregate_only": True,
            "level_2_eligible": False,
        },
    )
    session.commit()
    return BenchmarkAttestationReport(
        status="recorded",
        site_id=site.id,
        evidence_id=evidence.id,
        action_id=action.id,
        evaluation_id=attestation.evaluation_id,
        signature_verified=True,
        aggregate_only=True,
        engineering_benchmark_gate_passed=attestation.engineering_benchmark_gate_passed,
        level_2_eligible=False,
        production_write=False,
    )
