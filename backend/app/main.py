from __future__ import annotations

import asyncio
from datetime import timedelta
from html import escape
from pathlib import Path
from typing import Annotated
from uuid import UUID

from bs4 import BeautifulSoup
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.api.auth import Principal, administrator, authenticate, reviewer
from backend.app.api import schemas as s
from backend.app.api.configuration import router as configuration_router
from backend.app.api.experiments import router as experiment_router
from backend.app.config.settings import Settings, get_settings
from backend.app.contracts import ActionKind, CMSPage, ProviderUnavailable, VerificationPacket, utcnow
from backend.app.db import models as m
from backend.app.db.session import get_session
from backend.app.db.readiness import verify_schema_revision
from backend.app.observability.logging import instrument
from backend.app.services import control

DB = Annotated[Session, Depends(get_session)]
User = Annotated[Principal, Depends(authenticate)]
Admin = Annotated[Principal, Depends(administrator)]
Reviewer = Annotated[Principal, Depends(reviewer)]
Config = Annotated[Settings, Depends(get_settings)]

app = FastAPI(title="Spiral Max SEO Control Plane", version="0.1.0", docs_url="/docs", redoc_url=None)
instrument(app)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=get_settings().allowed_hosts, www_redirect=False)
app.include_router(configuration_router)
app.include_router(experiment_router)


@app.exception_handler(LookupError)
async def not_found(request: Request, error: LookupError):
    return JSONResponse(status_code=404, content={"detail": "Requested record does not exist in this site"})


@app.exception_handler(ValueError)
async def invalid_operation(request: Request, error: ValueError):
    return JSONResponse(status_code=422, content={"detail": str(error)[:400]})


@app.exception_handler(ProviderUnavailable)
async def unavailable_provider(request: Request, error: ProviderUnavailable):
    return JSONResponse(status_code=503, content={"detail": "Provider not available; configure the documented scoped connection"})


@app.exception_handler(SQLAlchemyError)
async def database_error(request: Request, error: SQLAlchemyError):
    return JSONResponse(status_code=503, content={"detail": "Database operation failed; no new external action should be attempted", "request_id": getattr(request.state, "request_id", None)})


@app.get("/healthz")
def health():
    return {"status": "ok", "service": "spiral-max-seo", "version": "0.1.0"}


@app.get("/readyz")
def readiness(session: DB):
    try:
        session.execute(text("SELECT 1"))
        session.execute(select(m.Site.id).limit(1))
        verify_schema_revision(session)
        return {"status": "ready"}
    except (SQLAlchemyError, ValueError):
        raise HTTPException(503, "Database is not reachable or does not match this release's migration head")


@app.get("/api/sites")
def list_sites(session: DB, user: User):
    return {"items": [control.serialise(x) for x in session.scalars(select(m.Site).order_by(m.Site.created_at))]}


@app.post("/api/sites", status_code=201)
def register_site(body: s.SiteCreate, session: DB, user: Admin):
    site = control.create_site(session, name=body.name, base_url=body.base_url)
    return control.serialise(site)


def current_approval(session: Session, site_id: str, revision_id: str):
    latest = session.scalar(select(m.Approval).where(m.Approval.site_id == site_id, m.Approval.revision_id == revision_id)
                            .order_by(m.Approval.created_at.desc(), m.Approval.id.desc()))
    revision = session.get(m.Revision, revision_id)
    return latest if (latest and revision and latest.decision == "APPROVE" and latest.expires_at
                      and latest.expires_at > utcnow() and latest.revision_hash == revision.revision_hash) else None


@app.get("/api/sites/{site_id}/state")
def get_state(site_id: UUID, session: DB, user: User):
    sid = str(site_id)
    site = control.site_record(session, sid)
    mission = session.scalar(select(m.MissionState).where(m.MissionState.site_id == sid))
    ga4 = list(session.scalars(select(m.GA4Daily).where(m.GA4Daily.site_id == sid)))
    gsc = list(session.scalars(select(m.GSCDaily).where(m.GSCDaily.site_id == sid)))
    page_totals = [r for r in gsc if not r.query]
    visibility = page_totals or gsc
    impressions = sum(r.impressions for r in visibility) if visibility else None
    clicks = sum(r.clicks for r in visibility) if visibility else None
    mapping_verified = bool(site.conversion_definition.get("verified"))
    values_known = mapping_verified and bool(ga4) and all(r.conversion_value is not None for r in ga4)
    conversions_known = mapping_verified and bool(ga4) and all(r.qualified_conversions is not None for r in ga4)
    pending = [r for r in session.scalars(select(m.Revision).where(m.Revision.site_id == sid)) if not current_approval(session, sid, r.id)]
    metrics = {
        "qualified_organic_conversion_value": sum(r.conversion_value for r in ga4) if values_known else None,
        "qualified_organic_conversions": sum(r.qualified_conversions for r in ga4) if conversions_known else None,
        "organic_sessions": sum(r.sessions for r in ga4) if ga4 else None,
        "impressions": impressions, "clicks": clicks, "ctr": clicks / impressions if impressions else None,
        "non_brand_visibility": None, "ai_citations": None,
        "open_opportunities": session.scalar(select(func.count()).select_from(m.Opportunity).where(m.Opportunity.site_id == sid, m.Opportunity.status == "open")),
        "running_experiments": session.scalar(select(func.count()).select_from(m.Experiment).where(m.Experiment.site_id == sid, m.Experiment.status == "running")),
        "human_approvals_required": len(pending),
        "regressions": session.scalar(select(func.count()).select_from(m.FailureCase).where(m.FailureCase.site_id == sid)),
        "approved_actions": session.scalar(select(func.count()).select_from(m.Approval).where(m.Approval.site_id == sid, m.Approval.decision == "APPROVE")),
    }
    return {"site": control.serialise(site), "mission": control.serialise(mission) if mission else None,
            "metrics": metrics, "source_mode": site.config_json.get("source_mode"),
            "measurement_notes": ["Missing values mean unknown, not zero", "Search visibility may omit privacy-limited queries",
                                  "Crawl eligibility is not confirmed Google indexing", "AI citations need a separate supported provider",
                                  "Incremental value is an experiment estimate, not the total conversion-value sum"],
            "period": {"from": min((r.date for r in ga4), default=None), "to": max((r.date for r in ga4), default=None)}}


@app.get("/api/sites/{site_id}/pages/{page_id}")
def get_page(site_id: UUID, page_id: UUID, session: DB, user: User):
    return control.serialise(control.scoped_record(session, m.Page, str(site_id), str(page_id)))


@app.get("/api/sites/{site_id}/pages/{page_id}/history")
def page_history(site_id: UUID, page_id: UUID, session: DB, user: User):
    control.scoped_record(session, m.Page, str(site_id), str(page_id))
    return {"items": [control.serialise(v) for v in session.scalars(select(m.PageVersion).where(m.PageVersion.site_id == str(site_id),
        m.PageVersion.page_id == str(page_id)).order_by(m.PageVersion.version_number.desc()))]}


@app.get("/api/sites/{site_id}/pages/{page_id}/compare")
def compare_versions(site_id: UUID, page_id: UUID, before_id: UUID, after_id: UUID, session: DB, user: User):
    from backend.app.seo.analysis import compare_page_versions
    before = control.scoped_record(session, m.PageVersion, str(site_id), str(before_id))
    after = control.scoped_record(session, m.PageVersion, str(site_id), str(after_id))
    if before.page_id != str(page_id) or after.page_id != str(page_id):
        raise HTTPException(404, "Both versions must belong to this page")
    return compare_page_versions(before.content_json, after.content_json)


@app.get("/api/sites/{site_id}/strategy")
def strategy(site_id: UUID, session: DB, user: User):
    sid = str(site_id)
    control.site_record(session, sid)
    collections = {"strategy": m.StrategyVersion, "claims": m.Claim, "evidence": m.Evidence, "assumptions": m.Assumption,
                   "contradictions": m.Contradiction, "decisions": m.DecisionLog, "calibration": m.CalibrationRecord}
    result = {}
    for key, model in collections.items():
        rows = session.scalars(select(model).where(model.site_id == sid).order_by(model.created_at.desc()).limit(100))
        result[key] = [control.serialise(row) for row in rows]
    # Large raw ingestion evidence is available through a dedicated scoped ID read, not repeated in every strategy response.
    for evidence in result["evidence"]:
        evidence.pop("content", None)
    return result


@app.get("/api/sites/{site_id}/evidence/{evidence_id}")
def evidence_by_id(site_id: UUID, evidence_id: UUID, session: DB, user: User):
    return control.serialise(control.scoped_record(session, m.Evidence, str(site_id), str(evidence_id)))


@app.get("/api/sites/{site_id}/internal-links")
def internal_links(site_id: UUID, session: DB, user: User):
    control.site_record(session, str(site_id))
    return {"items": [{"url": p.url, "links": p.links, "fetched_at": p.fetched_at} for p in control.latest_crawls(session, str(site_id))]}


VIEWS = {"pages": m.Page, "queries": m.Query, "gsc": m.GSCDaily, "ga4": m.GA4Daily,
         "opportunities": m.Opportunity, "tasks": m.Task, "experiments": m.Experiment,
         "actions": m.Action, "action-events": m.ActionEvent, "agents": m.AgentRun,
         "failures": m.FailureCase, "technical": m.CrawlIssue, "serps": m.SERPSnapshot,
         "ai-search": m.AISearchSnapshot, "revisions": m.Revision, "approvals": m.Approval,
         "verifications": m.Verification, "jobs": m.JobRun, "rollback-events": m.RollbackEvent}


@app.get("/api/sites/{site_id}/{view}")
def list_view(site_id: UUID, view: str, session: DB, user: User,
              limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0, le=100000)):
    control.site_record(session, str(site_id))
    model = VIEWS.get(view)
    if model is None:
        raise HTTPException(404, "Unknown canonical view")
    rows = session.scalars(select(model).where(model.site_id == str(site_id)).order_by(model.created_at.desc()).offset(offset).limit(limit))
    total = session.scalar(select(func.count()).select_from(model).where(model.site_id == str(site_id)))
    return {"items": [control.serialise(r) for r in rows], "total": total, "offset": offset, "limit": limit, "has_more": offset + limit < total}


@app.post("/api/sites/{site_id}/cycle")
def cycle(site_id: UUID, body: s.CycleRequest, session: DB, user: User, settings: Config):
    return control.run_cycle(session, str(site_id), settings, idempotency_key=body.idempotency_key)


@app.post("/api/sites/{site_id}/pause")
def pause_automation(site_id: UUID, body: s.PauseRequest, session: DB, user: Admin):
    site = session.scalar(select(m.Site).where(m.Site.id == str(site_id)).with_for_update()
                          .execution_options(populate_existing=True))
    if not site:
        raise HTTPException(404, "Site not found")
    site.production_enabled = False
    site.config_json = {**site.config_json, "automation_suspended": True}
    control.local_audit(session, site.id, "pause_automation", user.actor, body.reason,
                        {"production_enabled": False, "automation_suspended": True})
    session.commit()
    return {"status": "paused", "production_enabled": False, "autonomy_level": site.autonomy_level}


@app.post("/api/sites/{site_id}/analysis/{operation}")
def analytical_operation(site_id: UUID, operation: str, session: DB, user: User):
    from backend.app.seo.analysis import compare_serps
    valid = {"all", "cluster_queries", "content_decay", "ctr_anomaly", "cannibalisation", "orphan_pages",
             "broken_links", "redirect_chains", "duplicate_metadata", "compare_serps"}
    if operation not in valid:
        raise HTTPException(404, "Unknown deterministic analysis")
    if operation == "compare_serps":
        control.site_record(session, str(site_id))
        snapshots = list(session.scalars(select(m.SERPSnapshot).where(m.SERPSnapshot.site_id == str(site_id)).order_by(m.SERPSnapshot.observed_at.desc()).limit(20)))
        for latest in snapshots:
            earlier = next((x for x in snapshots if x.id != latest.id and x.query == latest.query and x.location == latest.location
                            and x.device == latest.device and x.provider == latest.provider and x.observed_at < latest.observed_at), None)
            if earlier:
                return compare_serps(earlier.results_json, latest.results_json)
        return {"status": "insufficient_data", "items": [], "reason": "Two compatible provider snapshots required"}
    result = control.analyze_site(session, str(site_id), persist=False)
    if operation == "cluster_queries":
        return {"items": result["query_clusters"]}
    if operation == "all":
        return result
    aliases = {"orphan_pages": ["orphan"], "broken_links": ["broken"], "redirect_chains": ["redirect"],
               "duplicate_metadata": ["duplicate"], "ctr_anomaly": ["ctr"], "content_decay": ["decay"], "cannibalisation": ["cannibal"]}
    return {"items": [x for x in result["opportunities"] if any(a in x["kind"] for a in aliases[operation])], "quality": result["quality"]}


@app.get("/api/action-risk")
def action_risk(kind: ActionKind, user: User):
    from backend.app.guardrails.policy import classify_risk
    return {"kind": kind, "risk": classify_risk(kind), "production_authority": False}


def require_proposals(site: m.Site):
    if site.autonomy_level < 1:
        raise HTTPException(403, "Observer autonomy does not permit proposal mutations")


@app.post("/api/sites/{site_id}/tasks", status_code=201)
def create_task(site_id: UUID, body: s.TaskCreate, session: DB, user: User):
    site = control.site_record(session, str(site_id))
    require_proposals(site)
    task = m.Task(site_id=site.id, title=body.title, objective=body.objective,
        contract_json={"objective": body.objective, "scope": [site.base_url], "non_goals": ["unapproved production modification"]})
    session.add(task)
    control.local_audit(session, site.id, "create_task", user.actor, body.objective, {"title": body.title})
    session.commit()
    return control.serialise(task)


def experiment_record(session: Session, site: m.Site, page: m.Page, hypothesis: str, mechanism: str) -> m.Experiment:
    latest = session.scalar(select(func.max(m.GA4Daily.date)).where(m.GA4Daily.site_id == site.id, m.GA4Daily.page_id == page.id))
    experiment = m.Experiment(site_id=site.id, page_id=page.id, hypothesis=hypothesis, mechanism=mechanism,
        primary_outcome="qualified_organic_conversion_value", secondary_outcomes_json=["qualified_organic_conversions", "organic_sessions", "CTR"],
        baseline_start=latest - timedelta(days=27) if latest else None, baseline_end=latest,
        evaluation_windows_json=[7, 14, 28, 56], alternative_explanations_json=["seasonality", "demand shifts", "tracking changes", "SERP changes"])
    session.add(experiment)
    session.flush()
    return experiment


@app.post("/api/sites/{site_id}/experiments", status_code=201)
def create_experiment(site_id: UUID, body: s.ExperimentCreate, session: DB, user: User):
    site = control.site_record(session, str(site_id))
    require_proposals(site)
    page = control.scoped_record(session, m.Page, site.id, str(body.page_id))
    experiment = experiment_record(session, site, page, body.hypothesis, body.mechanism)
    control.local_audit(session, site.id, "record_experiment", user.actor, body.hypothesis, {"experiment_id": experiment.id})
    session.commit()
    return control.serialise(experiment)


@app.post("/api/sites/{site_id}/hypotheses", status_code=201)
def add_hypothesis(site_id: UUID, body: s.HypothesisCreate, session: DB, user: User):
    site = control.site_record(session, str(site_id))
    require_proposals(site)
    ids = [str(x) for x in body.evidence_ids]
    for evidence_id in ids:
        control.scoped_record(session, m.Evidence, site.id, evidence_id)
    claim = m.Claim(site_id=site.id, claim=body.hypothesis, claim_type="HYPOTHESIS", source="control-plane-proposal",
                    evidence_ids_json=ids, confidence=0, owner=user.actor, status="proposed")
    session.add(claim)
    control.local_audit(session, site.id, "append_hypothesis", user.actor, "Record a hypothesis without changing policy", {"hypothesis": body.hypothesis})
    session.commit()
    return control.serialise(claim)


def prepare_draft(session: Session, site_id: str, page_id: str, kind: ActionKind, changes: dict, reason: str, evidence_ids: list[str], actor: str):
    from backend.app.services.execution import propose_revision
    site = control.site_record(session, site_id)
    require_proposals(site)
    page = control.scoped_record(session, m.Page, site_id, page_id)
    if "cms_snapshot" not in page.metadata_json:
        raise HTTPException(409, "A current CMS snapshot is required before drafting")
    for evidence_id in evidence_ids:
        control.scoped_record(session, m.Evidence, site_id, evidence_id)
    before = CMSPage.model_validate(page.metadata_json["cms_snapshot"])
    after = CMSPage.model_validate(before.model_dump() | changes)
    experiment = experiment_record(session, site, page, reason, "Improve the supported user need while preserving conversion and site integrity")
    return propose_revision(session, site_id=site_id, page_id=page_id, kind=kind, after=after,
        created_by=actor, reason=reason, evidence_ids=evidence_ids, experiment_id=experiment.id)


@app.post("/api/sites/{site_id}/drafts/metadata", status_code=201)
def metadata_draft(site_id: UUID, body: s.MetadataDraft, session: DB, user: User):
    return prepare_draft(session, str(site_id), str(body.page_id), ActionKind.UPDATE_TITLE, {"title": body.title}, body.reason,
                         [str(x) for x in body.evidence_ids], user.actor)


@app.post("/api/sites/{site_id}/drafts/description", status_code=201)
def description_draft(site_id: UUID, body: s.DescriptionDraft, session: DB, user: User):
    return prepare_draft(session, str(site_id), str(body.page_id), ActionKind.UPDATE_META_DESCRIPTION,
                         {"meta_description": body.meta_description}, body.reason, [str(x) for x in body.evidence_ids], user.actor)


@app.post("/api/sites/{site_id}/drafts/content", status_code=201)
def content_draft(site_id: UUID, body: s.ContentDraft, session: DB, user: User):
    content = "".join(f"<p>{escape(part)}</p>" for part in body.proposed_text.split("\n\n") if part.strip())
    return prepare_draft(session, str(site_id), str(body.page_id), ActionKind.UPDATE_EXISTING_COPY, {"content": content}, body.reason,
                         [str(x) for x in body.evidence_ids], user.actor)


@app.post("/api/sites/{site_id}/drafts/internal-link", status_code=201)
def internal_link_draft(site_id: UUID, body: s.LinkDraft, session: DB, user: User):
    page = control.scoped_record(session, m.Page, str(site_id), str(body.page_id))
    target = control.scoped_record(session, m.Page, str(site_id), str(body.target_page_id))
    if page.id == target.id:
        raise HTTPException(422, "A link must point to another page")
    soup = BeautifulSoup(page.content_html, "html.parser")
    candidates = [node for node in soup.find_all(string=True) if node.parent.name not in {"a", "script", "style", "title"} and body.anchor_text in str(node)]
    if len(candidates) != 1 or str(candidates[0]).count(body.anchor_text) != 1:
        raise HTTPException(422, "Anchor text must occur exactly once in an unlinked text node")
    node = candidates[0]
    prefix, suffix = str(node).split(body.anchor_text, 1)
    link = soup.new_tag("a", href=target.url)
    link.string = body.anchor_text
    node.replace_with(prefix, link, suffix)
    return prepare_draft(session, str(site_id), str(body.page_id), ActionKind.ADD_INTERNAL_LINK,
                         {"content": str(soup)}, body.reason, [str(x) for x in body.evidence_ids], user.actor)


@app.post("/api/sites/{site_id}/revisions/{revision_id}/verify")
def verify_revision(site_id: UUID, revision_id: UUID, session: DB, user: User, settings: Config):
    from backend.app.agents.runtime import verify_proposal
    from backend.app.guardrails.policy import classify_risk
    from backend.app.services.agent_audit import runtime_options
    from backend.app.services.execution import record_verification
    revision = control.scoped_record(session, m.Revision, str(site_id), str(revision_id))
    site = control.site_record(session, str(site_id))
    require_proposals(site)
    problem = {"kind": revision.kind, "page_url": revision.after_json["url"], "scope": [revision.page_id],
               "baseline": revision.before_json, "business_objective": control.OBJECTIVE}
    proposal = {"finding": revision.reason, "confidence": 0, "supporting_evidence": revision.evidence_ids_json,
                "recommended_action": revision.kind, "expected_impact": "Unverified qualified-conversion effect", "risk": classify_risk(ActionKind(revision.kind)),
                "reversibility": 1, "uncertainty": ["Proposer confidence is unmeasured; zero denotes no supported confidence", "Production outcome not yet observed"], "needs_human_review": True}
    result = asyncio.run(verify_proposal(problem, proposal, control.agent_evidence(session, str(site_id), revision.evidence_ids_json),
        proposer_id=revision.created_by,
        revision_target={"before": revision.before_json, "after": revision.after_json, "revision_hash": revision.revision_hash},
        prior_failures=control.prior_failures(session, site.id), **runtime_options(session, site, settings)))
    packet = VerificationPacket.model_validate(result["verification"])
    stored = record_verification(session, revision_id=revision.id, packet=packet)
    return {"verification": stored, "runtime": result}


@app.post("/api/sites/{site_id}/revisions/{revision_id}/human-review")
def human_review(site_id: UUID, revision_id: UUID, body: s.HumanReview, session: DB, user: Reviewer):
    from backend.app.services.execution import record_verification
    revision = control.scoped_record(session, m.Revision, str(site_id), str(revision_id))
    checks = {name: getattr(body, name) for name in ("factual_accuracy", "policy_compliance", "conversion_guard",
               "source_independence", "alternatives_considered", "tracking_quality")}
    packet = VerificationPacket(verdict=body.verdict, verifier_id="human-reviewer", independent=revision.created_by != user.actor,
        confidence=body.confidence, reasons=body.reasons, evidence_ids=revision.evidence_ids_json,
        alternative_explanations=body.alternative_explanations, checks=checks, action_safe=body.verdict == "PASS" and all(checks.values()))
    return record_verification(session, revision_id=revision.id, packet=packet)


@app.post("/api/sites/{site_id}/revisions/{revision_id}/approve")
def approve_revision(site_id: UUID, revision_id: UUID, body: s.ApprovalRequest, session: DB, user: Reviewer):
    from backend.app.services.execution import approve_revision as approve
    revision = control.scoped_record(session, m.Revision, str(site_id), str(revision_id))
    return approve(session, revision_id=revision.id, approved_by=user.actor, reason=body.reason)


@app.post("/api/sites/{site_id}/revisions/{revision_id}/veto")
def veto_revision(site_id: UUID, revision_id: UUID, body: s.VetoRequest, session: DB, user: Reviewer):
    revision = control.scoped_record(session, m.Revision, str(site_id), str(revision_id))
    decision = m.Approval(site_id=str(site_id), revision_id=revision.id, revision_hash=revision.revision_hash,
                         approved_by=user.actor, decision=body.decision, reason=body.reason,
                         expires_at=utcnow() + timedelta(days=365))
    session.add(decision)
    control.local_audit(session, str(site_id), "veto_revision", user.actor, body.reason,
                        {"revision_id": revision.id, "revision_hash": revision.revision_hash, "decision": body.decision})
    session.commit()
    return control.serialise(decision)


@app.post("/api/sites/{site_id}/revisions/{revision_id}/execute")
def execute_revision(site_id: UUID, revision_id: UUID, body: s.ExecuteRequest, session: DB, user: User, settings: Config):
    from backend.app.services.execution import execute_revision as execute
    site = control.site_record(session, str(site_id))
    revision = control.scoped_record(session, m.Revision, site.id, str(revision_id))
    return execute(session, control.cms_for_site(session, site, settings), revision_id=revision.id,
                   actor=user.actor, idempotency_key=body.idempotency_key,
                   production_enabled=settings.production_enabled and not settings.shadow_mode,
                   max_daily_actions=settings.max_daily_actions, max_autonomy_level=settings.autonomy_level)


@app.post("/api/sites/{site_id}/actions/{action_id}/rollback")
def rollback_action(site_id: UUID, action_id: UUID, body: s.ExecuteRequest, session: DB, user: User, settings: Config):
    from backend.app.services.execution import rollback_action as rollback
    site = control.site_record(session, str(site_id))
    action = control.scoped_record(session, m.Action, site.id, str(action_id))
    return rollback(session, control.cms_for_site(session, site, settings), action_id=action.id, actor=user.actor,
                    idempotency_key=body.idempotency_key, production_enabled=settings.production_enabled and not settings.shadow_mode,
                    max_daily_actions=settings.max_daily_actions, max_autonomy_level=settings.autonomy_level)


@app.post("/api/sites/{site_id}/actions/{action_id}/reconcile")
def reconcile_action(site_id: UUID, action_id: UUID, session: DB, user: User, settings: Config):
    from backend.app.services.execution import reconcile_action as reconcile
    site = control.site_record(session, str(site_id))
    action = control.scoped_record(session, m.Action, site.id, str(action_id))
    return reconcile(session, control.cms_for_site(session, site, settings), action_id=action.id, actor=user.actor)


@app.post("/api/sites/{site_id}/crawl")
def crawl(site_id: UUID, body: s.CrawlRequest, session: DB, user: User, settings: Config):
    from backend.app.integrations.common import ObservationBatch
    from backend.app.integrations.crawler.client import Crawler
    from backend.app.integrations.fixtures import fixture_crawler
    site = control.site_record(session, str(site_id))
    fixture = site.config_json.get("source_mode") == "fixture"
    crawler = fixture_crawler(control.cms_for_site(session, site, settings).list_pages()) if fixture else Crawler(site.base_url)
    if body.page_id:
        page = control.scoped_record(session, m.Page, site.id, str(body.page_id))
        batch = ObservationBatch(rows=[crawler.crawl_url(page.url)], source="fixture:crawl" if fixture else page.url)
    else:
        batch = crawler.crawl_site(max_pages=min(settings.max_crawl_pages, 50))
    evidence_id = control.ingest_batch(session, site, "crawl", batch)
    control.local_audit(session, site.id, "crawl_site", user.actor, "Observe current technical state", {"evidence_id": evidence_id, "rows": len(batch.rows)})
    session.commit()
    return {"evidence_id": evidence_id, "rows": len(batch.rows), "quality_flags": batch.quality_flags}


@app.post("/api/sites/{site_id}/measure")
def measure(site_id: UUID, session: DB, user: User):
    control.site_record(session, str(site_id))
    return control.evaluate_due_experiments(session, str(site_id))


DASHBOARD = Path(__file__).resolve().parents[2] / "dashboard" / "app"
if DASHBOARD.is_dir():
    app.mount("/assets", StaticFiles(directory=DASHBOARD), name="dashboard-assets")

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(DASHBOARD / "index.html")
