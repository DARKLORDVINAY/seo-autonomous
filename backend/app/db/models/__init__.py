"""Explicit, tenant-scoped canonical entities. Audit facts are append-only.

JSON columns carry bounded structured evidence, never executable instructions.
Relational identity, authority, tenancy, ownership and lifecycle are explicit.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, Date, DateTime, Float, ForeignKey,
    ForeignKeyConstraint, Index, Integer, MetaData, String, Text,
    UniqueConstraint, event, inspect, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Canonical timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })


class Identified:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class TenantOwned(Identified):
    site_id: Mapped[str] = mapped_column(String(36), ForeignKey("sites.id", ondelete="RESTRICT"), index=True)


class AppendOnly:
    """Marker checked by ORM and database triggers. Corrections append new rows."""


def tenant(table: str, *constraints):
    return (UniqueConstraint("id", "site_id", name=f"uq_{table}_id_site"), *constraints)


def reference(table: str, column: str):
    return ForeignKeyConstraint([column, "site_id"], [f"{table}.id", f"{table}.site_id"], ondelete="RESTRICT")


class Site(Identified, Base):
    __tablename__ = "sites"
    __table_args__ = (
        UniqueConstraint("base_url"),
        CheckConstraint("autonomy_level BETWEEN 0 AND 5", name="autonomy_range"),
    )
    name: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str] = mapped_column(String(2048))
    autonomy_level: Mapped[int] = mapped_column(Integer, default=1)
    production_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    conversion_definition: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Page(TenantOwned, Base):
    __tablename__ = "pages"
    __table_args__ = tenant("pages", UniqueConstraint("site_id", "url"))
    url: Mapped[str] = mapped_column(String(2048))
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    title: Mapped[str] = mapped_column(Text, default="")
    content_html: Mapped[str] = mapped_column(Text, default="")
    meta_description: Mapped[str] = mapped_column(Text, default="")
    canonical: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    indexed_status: Mapped[str] = mapped_column(String(40), default="unknown")
    crawlable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class PageEntity(TenantOwned, Base):
    __tablename__ = "page_entities"
    __table_args__ = tenant("page_entities", reference("pages", "page_id"), UniqueConstraint("page_id", "entity_name", "entity_type"))
    page_id: Mapped[str] = mapped_column(String(36))
    entity_name: Mapped[str] = mapped_column(String(250))
    entity_type: Mapped[str] = mapped_column(String(100))
    attributes_json: Mapped[dict] = mapped_column(JSON, default=dict)


class QueryCluster(TenantOwned, Base):
    __tablename__ = "query_clusters"
    __table_args__ = tenant("query_clusters", UniqueConstraint("site_id", "name"))
    name: Mapped[str] = mapped_column(String(250))
    intent: Mapped[str] = mapped_column(String(100), default="unknown")
    method: Mapped[str] = mapped_column(String(100), default="deterministic_tokens")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Query(TenantOwned, Base):
    __tablename__ = "queries"
    __table_args__ = tenant("queries", reference("query_clusters", "cluster_id"), UniqueConstraint("site_id", "text"))
    text: Mapped[str] = mapped_column(String(2048))
    cluster_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    is_brand: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class GSCDaily(TenantOwned, Base):
    __tablename__ = "gsc_daily"
    __table_args__ = tenant("gsc_daily", reference("pages", "page_id"),
        UniqueConstraint("site_id", "date", "page_url", "query", "country", "device"),
        CheckConstraint("clicks >= 0 AND impressions >= 0 AND position >= 0", name="nonnegative_metrics"),
        Index("ix_gsc_site_date", "site_id", "date"))
    page_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    date: Mapped[date] = mapped_column(Date)
    page_url: Mapped[str] = mapped_column(String(2048))
    query: Mapped[str] = mapped_column(String(2048), default="")
    country: Mapped[str] = mapped_column(String(8), default="")
    device: Mapped[str] = mapped_column(String(30), default="")
    clicks: Mapped[int] = mapped_column(Integer)
    impressions: Mapped[int] = mapped_column(Integer)
    position: Mapped[float] = mapped_column(Float)
    data_state: Mapped[str] = mapped_column(String(20), default="unknown")
    is_fixture: Mapped[bool] = mapped_column(Boolean, default=False)
    quality_flags_json: Mapped[list] = mapped_column(JSON, default=list)


class GA4Daily(TenantOwned, Base):
    __tablename__ = "ga4_daily"
    __table_args__ = tenant("ga4_daily", reference("pages", "page_id"),
        UniqueConstraint("site_id", "date", "landing_page", "channel"),
        CheckConstraint("sessions >= 0 AND key_events >= 0", name="nonnegative_metrics"),
        CheckConstraint("qualified_conversions IS NULL OR qualified_conversions >= 0", name="qualified_nonnegative"),
        CheckConstraint("conversion_value IS NULL OR conversion_value >= 0", name="value_nonnegative"),
        Index("ix_ga4_site_date", "site_id", "date"))
    page_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    date: Mapped[date] = mapped_column(Date)
    landing_page: Mapped[str] = mapped_column(String(2048))
    channel: Mapped[str] = mapped_column(String(100), default="Organic Search")
    sessions: Mapped[int] = mapped_column(Integer)
    key_events: Mapped[float] = mapped_column(Float, default=0)
    qualified_conversions: Mapped[float | None] = mapped_column(Float, nullable=True)
    conversion_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_fixture: Mapped[bool] = mapped_column(Boolean, default=False)
    quality_flags_json: Mapped[list] = mapped_column(JSON, default=list)


class SERPSnapshot(TenantOwned, AppendOnly, Base):
    __tablename__ = "serp_snapshots"
    __table_args__ = tenant("serp_snapshots", Index("ix_serp_site_query", "site_id", "query"))
    query: Mapped[str] = mapped_column(String(2048))
    provider: Mapped[str] = mapped_column(String(100))
    location: Mapped[str] = mapped_column(String(200), default="")
    device: Mapped[str] = mapped_column(String(30), default="desktop")
    results_json: Mapped[list] = mapped_column(JSON, default=list)
    features_json: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    is_fixture: Mapped[bool] = mapped_column(Boolean, default=False)


class AISearchSnapshot(TenantOwned, AppendOnly, Base):
    __tablename__ = "ai_search_snapshots"
    __table_args__ = tenant("ai_search_snapshots")
    query: Mapped[str] = mapped_column(String(2048))
    provider: Mapped[str] = mapped_column(String(100))
    response_json: Mapped[dict] = mapped_column(JSON, default=dict)
    citations_json: Mapped[list] = mapped_column(JSON, default=list)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    is_fixture: Mapped[bool] = mapped_column(Boolean, default=False)
    capability_status: Mapped[str] = mapped_column(String(50), default="unknown")


class CrawlSnapshot(TenantOwned, AppendOnly, Base):
    __tablename__ = "crawl_snapshots"
    __table_args__ = tenant("crawl_snapshots", reference("pages", "page_id"))
    page_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    url: Mapped[str] = mapped_column(String(2048))
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    is_fixture: Mapped[bool] = mapped_column(Boolean, default=False)


class CrawlIssue(TenantOwned, Base):
    __tablename__ = "crawl_issues"
    __table_args__ = tenant("crawl_issues", reference("crawl_snapshots", "crawl_snapshot_id"), reference("pages", "page_id"))
    crawl_snapshot_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    page_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(100))
    severity: Mapped[str] = mapped_column(String(20), default="LOW")
    description: Mapped[str] = mapped_column(Text, default="")
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="open")


class Evidence(TenantOwned, AppendOnly, Base):
    __tablename__ = "evidence"
    __table_args__ = tenant("evidence", CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_range"))
    source: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(100), default="unknown")
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    owner: Mapped[str] = mapped_column(String(200), default="system")
    status: Mapped[str] = mapped_column(String(30), default="active")
    is_fixture: Mapped[bool] = mapped_column(Boolean, default=False)


class Claim(TenantOwned, AppendOnly, Base):
    __tablename__ = "claims"
    __table_args__ = tenant("claims", CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_range"),
        CheckConstraint("claim_type IN ('FACT','INFERENCE','HYPOTHESIS','ASSUMPTION','DECISION','ACTION')", name="claim_type"))
    claim: Mapped[str] = mapped_column(Text)
    claim_type: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(Text, default="")
    evidence_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float)
    owner: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="proposed")
    contradicting_evidence_json: Mapped[list] = mapped_column(JSON, default=list)
    alternative_explanations_json: Mapped[list] = mapped_column(JSON, default=list)
    supersedes_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class ClaimEvidence(TenantOwned, AppendOnly, Base):
    __tablename__ = "claim_evidence"
    __table_args__ = tenant("claim_evidence", reference("claims", "claim_id"), reference("evidence", "evidence_id"),
        UniqueConstraint("claim_id", "evidence_id", "relationship"))
    claim_id: Mapped[str] = mapped_column(String(36))
    evidence_id: Mapped[str] = mapped_column(String(36))
    relationship: Mapped[str] = mapped_column(String(30), default="supports")


class Assumption(TenantOwned, Base):
    __tablename__ = "assumptions"
    __table_args__ = tenant("assumptions")
    statement: Mapped[str] = mapped_column(Text)
    owner: Mapped[str] = mapped_column(String(200), default="governor")
    confidence: Mapped[float] = mapped_column(Float, default=0)
    impact: Mapped[str] = mapped_column(Text, default="")
    falsification_test: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="unverified")
    evidence_ids_json: Mapped[list] = mapped_column(JSON, default=list)


class Contradiction(TenantOwned, Base):
    __tablename__ = "contradictions"
    __table_args__ = tenant("contradictions", reference("claims", "claim_a_id"), reference("claims", "claim_b_id"))
    claim_a_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    claim_b_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="open")
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class Opportunity(TenantOwned, Base):
    __tablename__ = "opportunities"
    __table_args__ = tenant("opportunities", reference("pages", "page_id"),
        UniqueConstraint("site_id", "dedup_key"), Index("ix_opportunities_rank", "site_id", "status", "score"))
    page_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    page_url: Mapped[str] = mapped_column(String(2048), default="")
    kind: Mapped[str] = mapped_column(String(100))
    finding: Mapped[str] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    components_json: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    evidence_json: Mapped[list] = mapped_column(JSON, default=list)
    quality_flags_json: Mapped[list] = mapped_column(JSON, default=list)
    recommended_action: Mapped[str] = mapped_column(String(100), default="investigate")
    status: Mapped[str] = mapped_column(String(30), default="open")
    dedup_key: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Task(TenantOwned, Base):
    __tablename__ = "tasks"
    __table_args__ = tenant("tasks", reference("opportunities", "opportunity_id"))
    title: Mapped[str] = mapped_column(String(500))
    objective: Mapped[str] = mapped_column(Text, default="")
    opportunity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="open")
    priority: Mapped[float] = mapped_column(Float, default=0)
    contract_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class TaskDependency(TenantOwned, Base):
    __tablename__ = "task_dependencies"
    __table_args__ = tenant("task_dependencies", reference("tasks", "task_id"), reference("tasks", "depends_on_id"),
        UniqueConstraint("task_id", "depends_on_id"), CheckConstraint("task_id <> depends_on_id", name="no_self_dependency"))
    task_id: Mapped[str] = mapped_column(String(36))
    depends_on_id: Mapped[str] = mapped_column(String(36))


class TaskOwnership(TenantOwned, Base):
    __tablename__ = "task_ownership"
    __table_args__ = tenant("task_ownership", reference("tasks", "task_id"), UniqueConstraint("task_id"))
    task_id: Mapped[str] = mapped_column(String(36))
    owner: Mapped[str] = mapped_column(String(200))
    token: Mapped[str] = mapped_column(String(36), default=new_id)
    acquired_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    fencing_token: Mapped[int] = mapped_column(Integer, default=1)


class AgentRun(TenantOwned, Base):
    __tablename__ = "agent_runs"
    __table_args__ = tenant("agent_runs", reference("tasks", "task_id"))
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    agent_name: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(100), default="deterministic")
    mode: Mapped[str] = mapped_column(String(30), default="fixture")
    status: Mapped[str] = mapped_column(String(30), default="running")
    contract_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


class AgentFinding(TenantOwned, AppendOnly, Base):
    __tablename__ = "agent_findings"
    __table_args__ = tenant("agent_findings", reference("agent_runs", "agent_run_id"), reference("claims", "claim_id"))
    agent_run_id: Mapped[str] = mapped_column(String(36))
    claim_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    packet_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Experiment(TenantOwned, Base):
    __tablename__ = "experiments"
    __table_args__ = tenant("experiments", reference("pages", "page_id"),
        CheckConstraint("predicted_confidence IS NULL OR predicted_confidence BETWEEN 0 AND 1", name="confidence_range"))
    page_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(300), default="SEO experiment")
    hypothesis: Mapped[str] = mapped_column(Text)
    mechanism: Mapped[str] = mapped_column(Text, default="")
    primary_outcome: Mapped[str] = mapped_column(String(200), default="qualified_organic_conversion_value")
    secondary_outcomes_json: Mapped[list] = mapped_column(JSON, default=list)
    baseline_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    baseline_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    deployed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    evaluation_windows_json: Mapped[list] = mapped_column(JSON, default=lambda: [7, 14, 28, 56])
    control_pages_json: Mapped[list] = mapped_column(JSON, default=list)
    predicted_effect: Mapped[float | None] = mapped_column(Float, nullable=True)
    predicted_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_effect: Mapped[float | None] = mapped_column(Float, nullable=True)
    alternative_explanations_json: Mapped[list] = mapped_column(JSON, default=list)
    verdict: Mapped[str] = mapped_column(String(50), default="pending")
    status: Mapped[str] = mapped_column(String(30), default="planned")
    analysis_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ExperimentMetric(TenantOwned, AppendOnly, Base):
    __tablename__ = "experiment_metrics"
    __table_args__ = tenant("experiment_metrics", reference("experiments", "experiment_id"))
    experiment_id: Mapped[str] = mapped_column(String(36))
    checkpoint_days: Mapped[int] = mapped_column(Integer)
    metric: Mapped[str] = mapped_column(String(200))
    observed_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    analysis_json: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Revision(TenantOwned, AppendOnly, Base):
    __tablename__ = "revisions"
    __table_args__ = tenant("revisions", reference("pages", "page_id"), reference("experiments", "experiment_id"),
        UniqueConstraint("id", "site_id", "revision_hash", name="uq_revision_binding"))
    page_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(100))
    changes_json: Mapped[dict] = mapped_column(JSON, default=dict)
    before_json: Mapped[dict] = mapped_column(JSON, default=dict)
    after_json: Mapped[dict] = mapped_column(JSON, default=dict)
    before_hash: Mapped[str] = mapped_column(String(64))
    revision_hash: Mapped[str] = mapped_column(String(64))
    evidence_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(200))
    experiment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class Verification(TenantOwned, AppendOnly, Base):
    __tablename__ = "verifications"
    __table_args__ = tenant("verifications",
        ForeignKeyConstraint(["revision_id", "site_id", "revision_hash"], ["revisions.id", "revisions.site_id", "revisions.revision_hash"], ondelete="RESTRICT"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_range"),
        CheckConstraint("verdict IN ('PASS', 'BLOCK', 'NEEDS_EVIDENCE')", name="verdict"))
    revision_id: Mapped[str] = mapped_column(String(36))
    revision_hash: Mapped[str] = mapped_column(String(64))
    verifier_id: Mapped[str] = mapped_column(String(200))
    verdict: Mapped[str] = mapped_column(String(30))
    independent: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float)
    action_safe: Mapped[bool] = mapped_column(Boolean, default=False)
    packet_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Approval(TenantOwned, AppendOnly, Base):
    __tablename__ = "approvals"
    __table_args__ = tenant("approvals",
        ForeignKeyConstraint(["revision_id", "site_id", "revision_hash"], ["revisions.id", "revisions.site_id", "revisions.revision_hash"], ondelete="RESTRICT"),
        CheckConstraint("decision IN ('APPROVE','REJECT','REVOKE')", name="decision"))
    revision_id: Mapped[str] = mapped_column(String(36))
    revision_hash: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str] = mapped_column(String(200))
    decision: Mapped[str] = mapped_column(String(20), default="APPROVE")
    reason: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class ActionBatch(TenantOwned, AppendOnly, Base):
    __tablename__ = "action_batches"
    __table_args__ = tenant("action_batches")
    actor: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text)
    manifest_json: Mapped[list] = mapped_column(JSON, default=list)


class Action(TenantOwned, AppendOnly, Base):
    __tablename__ = "actions"
    __table_args__ = tenant("actions", reference("revisions", "revision_id"), reference("experiments", "experiment_id"),
        reference("action_batches", "batch_id"), UniqueConstraint("site_id", "idempotency_key"),
        CheckConstraint("risk IN ('LOW','MEDIUM','HIGH','CRITICAL')", name="risk"))
    revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    batch_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(100))
    risk: Mapped[str] = mapped_column(String(20))
    actor: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text)
    experiment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ActionEvent(TenantOwned, AppendOnly, Base):
    __tablename__ = "action_events"
    __table_args__ = tenant("action_events", reference("actions", "action_id"), Index("ix_action_events_history", "action_id", "created_at"))
    action_id: Mapped[str] = mapped_column(String(36))
    event_type: Mapped[str] = mapped_column(String(100))
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)


class PageVersion(TenantOwned, AppendOnly, Base):
    __tablename__ = "page_versions"
    __table_args__ = tenant("page_versions", reference("pages", "page_id"), reference("actions", "action_id"),
        UniqueConstraint("page_id", "version_number"))
    page_id: Mapped[str] = mapped_column(String(36))
    action_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    version_number: Mapped[int] = mapped_column(Integer)
    content_json: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))


class RollbackEvent(TenantOwned, AppendOnly, Base):
    __tablename__ = "rollback_events"
    __table_args__ = tenant("rollback_events", reference("actions", "action_id"), reference("actions", "rollback_action_id"),
        reference("page_versions", "page_version_id"))
    action_id: Mapped[str] = mapped_column(String(36))
    rollback_action_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    page_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="proposed")
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ExecutionLease(TenantOwned, Base):
    """Non-expiring lock: ambiguous external writes require explicit reconciliation."""
    __tablename__ = "execution_leases"
    __table_args__ = tenant("execution_leases", reference("pages", "page_id"), reference("actions", "action_id"),
        UniqueConstraint("site_id", "page_id"))
    page_id: Mapped[str] = mapped_column(String(36))
    action_id: Mapped[str] = mapped_column(String(36))
    owner: Mapped[str] = mapped_column(String(200))
    token: Mapped[str] = mapped_column(String(36), default=new_id)


class FailureCase(TenantOwned, AppendOnly, Base):
    __tablename__ = "failure_cases"
    __table_args__ = tenant("failure_cases", reference("actions", "action_id"), reference("agent_runs", "agent_run_id"))
    action_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    category: Mapped[str] = mapped_column(String(100))
    predicted: Mapped[str] = mapped_column(Text)
    actual: Mapped[str] = mapped_column(Text)
    magnitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    root_cause: Mapped[str] = mapped_column(Text, default="unknown")
    incorrect_assumption: Mapped[str] = mapped_column(Text, default="")
    missing_evidence: Mapped[str] = mapped_column(Text, default="")
    agent_responsible: Mapped[str] = mapped_column(String(200), default="unknown")
    detection_method: Mapped[str] = mapped_column(Text, default="")
    preventative_change: Mapped[str] = mapped_column(Text, default="")
    guardrail_change_required: Mapped[bool] = mapped_column(Boolean, default=False)
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Policy(TenantOwned, AppendOnly, Base):
    __tablename__ = "policies"
    __table_args__ = tenant("policies", UniqueConstraint("site_id", "name", "version"))
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer)
    rules_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(200))
    effective_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class Guardrail(TenantOwned, AppendOnly, Base):
    __tablename__ = "guardrails"
    __table_args__ = tenant("guardrails", reference("actions", "action_id"))
    action_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    rule: Mapped[str] = mapped_column(String(200))
    outcome: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(Text)
    context_json: Mapped[dict] = mapped_column(JSON, default=dict)


class StrategyVersion(TenantOwned, AppendOnly, Base):
    __tablename__ = "strategy_versions"
    __table_args__ = tenant("strategy_versions", UniqueConstraint("site_id", "version"))
    version: Mapped[int] = mapped_column(Integer)
    objective: Mapped[str] = mapped_column(Text)
    strategy_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(200))
    evidence_ids_json: Mapped[list] = mapped_column(JSON, default=list)


class CalibrationRecord(TenantOwned, AppendOnly, Base):
    __tablename__ = "calibration_records"
    __table_args__ = tenant("calibration_records", reference("experiments", "experiment_id"),
        CheckConstraint("predicted_confidence BETWEEN 0 AND 1", name="confidence_range"))
    experiment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    agent_name: Mapped[str] = mapped_column(String(100))
    action_category: Mapped[str] = mapped_column(String(100))
    predicted_confidence: Mapped[float] = mapped_column(Float)
    succeeded: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    brier_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    evaluable: Mapped[bool] = mapped_column(Boolean, default=False)
    exclusion_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome_json: Mapped[dict] = mapped_column(JSON, default=dict)


class DecisionLog(TenantOwned, AppendOnly, Base):
    __tablename__ = "decision_logs"
    __table_args__ = tenant("decision_logs", reference("actions", "action_id"))
    action_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    decision: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    owner: Mapped[str] = mapped_column(String(200))
    evidence_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    alternatives_json: Mapped[list] = mapped_column(JSON, default=list)
    uncertainty_json: Mapped[list] = mapped_column(JSON, default=list)
    regret_json: Mapped[dict] = mapped_column(JSON, default=dict)


class MissionState(TenantOwned, Base):
    __tablename__ = "mission_states"
    __table_args__ = tenant("mission_states", UniqueConstraint("site_id"))
    objective: Mapped[str] = mapped_column(Text)
    success_criteria_json: Mapped[list] = mapped_column(JSON, default=list)
    constraints_json: Mapped[list] = mapped_column(JSON, default=list)
    available_resources_json: Mapped[dict] = mapped_column(JSON, default=dict)
    unknowns_json: Mapped[list] = mapped_column(JSON, default=list)
    blockers_json: Mapped[list] = mapped_column(JSON, default=list)
    critical_path_json: Mapped[list] = mapped_column(JSON, default=list)
    resource_budget_json: Mapped[dict] = mapped_column(JSON, default=dict)
    autonomy_level: Mapped[int] = mapped_column(Integer, default=1)
    phase: Mapped[str] = mapped_column(String(100), default="foundation")
    stop_condition: Mapped[str] = mapped_column(Text, default="Production activation requires owner configuration and approval")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class JobRun(TenantOwned, Base):
    __tablename__ = "job_runs"
    __table_args__ = tenant("job_runs", UniqueConstraint("site_id", "job_name", "idempotency_key"))
    job_name: Mapped[str] = mapped_column(String(200))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    owner: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="running")
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class JobLease(Base):
    __tablename__ = "job_leases"
    key: Mapped[str] = mapped_column(String(250), primary_key=True)
    site_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("sites.id", ondelete="RESTRICT"), nullable=True)
    owner: Mapped[str] = mapped_column(String(200))
    token: Mapped[str] = mapped_column(String(36))
    fencing_token: Mapped[int] = mapped_column(Integer, default=1)
    acquired_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class ImmutableRecordError(ValueError):
    pass


@event.listens_for(Session, "before_flush")
def _block_immutable_changes(session, flush_context, instances):
    for obj in session.deleted:
        if isinstance(obj, AppendOnly):
            raise ImmutableRecordError(f"{type(obj).__name__} records cannot be deleted; append a correction")
    for obj in session.dirty:
        if isinstance(obj, AppendOnly) and inspect(obj).persistent and session.is_modified(obj, include_collections=True):
            raise ImmutableRecordError(f"{type(obj).__name__} records cannot be updated; append a correction")


APPEND_ONLY_TABLES = tuple(sorted(mapper.local_table.name for mapper in Base.registry.mappers if issubclass(mapper.class_, AppendOnly)))


def install_append_only_triggers(connection) -> None:
    """DB enforcement protects bulk SQL too. A DBA can still alter schema/roles."""
    if connection.dialect.name == "postgresql":
        # The PL/pgSQL format marker is a literal percent sign, not a psycopg
        # placeholder. SQLAlchemy text compilation escapes it for the dialect;
        # raw driver SQL with an empty parameter mapping does not.
        connection.execute(text("""
            CREATE OR REPLACE FUNCTION seo_reject_audit_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$ BEGIN
                RAISE EXCEPTION 'append-only canonical record: %', TG_TABLE_NAME;
            END $$;
        """))
        for table in APPEND_ONLY_TABLES:
            connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{table}_immutable" ON "{table}"')
            connection.exec_driver_sql(f'CREATE TRIGGER "{table}_immutable" BEFORE UPDATE OR DELETE ON "{table}" FOR EACH ROW EXECUTE FUNCTION seo_reject_audit_mutation()')
            connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{table}_no_truncate" ON "{table}"')
            connection.exec_driver_sql(f'CREATE TRIGGER "{table}_no_truncate" BEFORE TRUNCATE ON "{table}" FOR EACH STATEMENT EXECUTE FUNCTION seo_reject_audit_mutation()')
    elif connection.dialect.name == "sqlite":
        for table in APPEND_ONLY_TABLES:
            for operation in ("UPDATE", "DELETE"):
                name = f"{table}_immutable_{operation.lower()}"
                connection.exec_driver_sql(f'CREATE TRIGGER IF NOT EXISTS "{name}" BEFORE {operation} ON "{table}" BEGIN SELECT RAISE(ABORT, \'append-only canonical record\'); END')


@event.listens_for(Base.metadata, "after_create")
def _install_triggers_after_create(target, connection, **kwargs):
    install_append_only_triggers(connection)
