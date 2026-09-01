"""Shared, strict contracts. External observations never carry executable authority."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stable_hash(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Risk(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ActionKind(StrEnum):
    CREATE_CONTENT_DRAFT = "create_content_draft"
    CREATE_METADATA_DRAFT = "create_metadata_draft"
    PROPOSE_INTERNAL_LINK = "propose_internal_link"
    CREATE_CMS_DRAFT = "create_cms_draft"
    UPDATE_TITLE = "update_title"
    UPDATE_META_DESCRIPTION = "update_meta_description"
    ADD_INTERNAL_LINK = "add_internal_link"
    UPDATE_SCHEMA = "update_schema"
    UPDATE_EXISTING_COPY = "update_existing_copy"
    PUBLISH_PAGE = "publish_page"
    CHANGE_SLUG = "change_slug"
    CHANGE_CANONICAL = "change_canonical"
    CHANGE_ROBOTS = "change_robots"
    REDIRECT_URL = "redirect_url"
    DELETE_PAGE = "delete_page"
    MODIFY_TEMPLATE = "modify_template"
    DEPLOY_CODE = "deploy_code"


class ClaimType(StrEnum):
    FACT = "FACT"
    INFERENCE = "INFERENCE"
    HYPOTHESIS = "HYPOTHESIS"
    ASSUMPTION = "ASSUMPTION"
    DECISION = "DECISION"
    ACTION = "ACTION"


class CMSPage(StrictModel):
    external_id: str
    url: str
    title: str
    content: str = ""
    meta_description: str = ""
    status: str = "publish"
    slug: str = ""
    modified_gmt: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return stable_hash(self.model_dump(exclude={"modified_gmt"}))


class CMSProvider(Protocol):
    is_fixture: bool

    def get_page(self, external_id: str) -> CMSPage: ...
    def list_pages(self) -> list[CMSPage]: ...
    def update_page(self, external_id: str, changes: dict[str, Any], *, expected_fingerprint: str) -> CMSPage: ...
    def create_draft(self, title: str, content: str) -> CMSPage: ...


class GSCRow(StrictModel):
    date: date
    page: str
    query: str = ""
    country: str = ""
    device: str = ""
    clicks: int = Field(ge=0)
    impressions: int = Field(ge=0)
    position: float = Field(ge=0)
    data_state: Literal["final", "partial", "unknown"] = "unknown"


class GA4Row(StrictModel):
    date: date
    landing_page: str
    sessions: int = Field(ge=0)
    key_events: float = Field(default=0, ge=0)
    qualified_conversions: float | None = Field(default=None, ge=0)
    conversion_value: float | None = Field(default=None, ge=0)
    channel: str = "Organic Search"
    quality_flags: list[str] = Field(default_factory=list)


class CrawlResult(StrictModel):
    url: str
    final_url: str
    status_code: int | None = None
    title: str = ""
    meta_description: str = ""
    canonical: str | None = None
    robots_directives: list[str] = Field(default_factory=list)
    crawlable: bool | None = None
    indexability: Literal["eligible", "blocked", "unknown"] = "unknown"
    links: list[str] = Field(default_factory=list)
    schema: list[Any] = Field(default_factory=list)
    redirect_chain: list[str] = Field(default_factory=list)
    content_hash: str = ""
    text: str = ""
    # Extracted content fields are observations, never policy or proof of usefulness.
    main_text: str = ""
    main_heading: str = ""
    main_content_observed: bool = False
    has_interactive_content: bool = False
    issues: list[dict[str, Any]] = Field(default_factory=list)
    fetched_at: datetime = Field(default_factory=utcnow)
    source_trust: Literal["untrusted_external"] = "untrusted_external"


class TaskContract(StrictModel):
    objective: str
    scope: list[str]
    allowed_inputs: list[str]
    available_tools: list[str]
    expected_output_schema: str = "FindingPacket"
    evidence_requirements: list[str]
    non_goals: list[str]
    stop_condition: str
    max_turns: int = Field(default=4, ge=1, le=12)


class FindingPacket(StrictModel):
    finding: str
    claim_type: ClaimType = ClaimType.INFERENCE
    confidence: float = Field(ge=0, le=1)
    supporting_evidence: list[str]
    contradicting_evidence: list[str] = Field(default_factory=list)
    alternative_explanations: list[str] = Field(default_factory=list)
    recommended_action: str = "NO-ACTION"
    expected_impact: str = "Unknown"
    risk: Risk = Risk.LOW
    reversibility: float = Field(default=1, ge=0, le=1)
    uncertainty: list[str] = Field(default_factory=list)
    needs_human_review: bool = True
    content_classification: Literal["KEEP", "UPDATE", "EXPAND", "MERGE", "CREATE", "DELETE-CANDIDATE", "NO-ACTION"] = "NO-ACTION"


class VerificationPacket(StrictModel):
    verdict: Literal["PASS", "BLOCK", "NEEDS_EVIDENCE"]
    verifier_id: str
    independent: bool
    confidence: float = Field(ge=0, le=1)
    reasons: list[str]
    evidence_ids: list[str]
    alternative_explanations: list[str] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)
    action_safe: bool = False


class OpportunityCandidate(StrictModel):
    kind: str
    page_url: str
    finding: str
    evidence: list[dict[str, Any]]
    components: dict[str, float]
    score: float
    confidence: float = Field(ge=0, le=1)
    recommended_action: str = "investigate"
    quality_flags: list[str] = Field(default_factory=list)


class ProviderUnavailable(RuntimeError):
    """Missing credentials/capability. Never translate this into zero observations."""


class ConcurrencyConflict(RuntimeError):
    """CMS state changed after the revision was prepared."""
