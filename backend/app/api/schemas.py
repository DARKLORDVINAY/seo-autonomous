from typing import Literal
from uuid import UUID

from pydantic import Field

from backend.app.contracts import StrictModel


class SiteCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=8, max_length=2048)


class CycleRequest(StrictModel):
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=150)


class TaskCreate(StrictModel):
    title: str = Field(min_length=3, max_length=300)
    objective: str = Field(min_length=8, max_length=4000)


class MetadataDraft(StrictModel):
    page_id: UUID
    title: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=10, max_length=2000)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=20)


class DescriptionDraft(StrictModel):
    page_id: UUID
    meta_description: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=10, max_length=2000)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=20)


class ContentDraft(StrictModel):
    page_id: UUID
    proposed_text: str = Field(min_length=10, max_length=12000)
    reason: str = Field(min_length=10, max_length=2000)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=20)


class LinkDraft(StrictModel):
    page_id: UUID
    target_page_id: UUID
    anchor_text: str = Field(min_length=2, max_length=120)
    reason: str = Field(min_length=10, max_length=2000)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=20)


class ExperimentCreate(StrictModel):
    page_id: UUID
    hypothesis: str = Field(min_length=10, max_length=3000)
    mechanism: str = Field(min_length=10, max_length=3000)


class HypothesisCreate(StrictModel):
    hypothesis: str = Field(min_length=10, max_length=3000)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=20)


class ExecuteRequest(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=150, pattern=r"^[A-Za-z0-9:_-]+$")


class ApprovalRequest(StrictModel):
    reason: str = Field(min_length=10, max_length=3000)


class VetoRequest(ApprovalRequest):
    decision: Literal["REJECT", "REVOKE"]


class PauseRequest(ApprovalRequest):
    pass


class HumanReview(StrictModel):
    verdict: Literal["PASS", "BLOCK", "NEEDS_EVIDENCE"]
    confidence: float = Field(ge=0, le=1)
    reasons: list[str] = Field(min_length=1, max_length=20)
    factual_accuracy: bool
    policy_compliance: bool
    conversion_guard: bool
    source_independence: bool
    alternatives_considered: bool
    tracking_quality: bool
    alternative_explanations: list[str] = Field(min_length=1, max_length=20)


class CrawlRequest(StrictModel):
    page_id: UUID | None = None
