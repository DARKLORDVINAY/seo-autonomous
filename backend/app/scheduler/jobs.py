"""Durable, fenced observation jobs. Only the daily cycle may consult paid agents."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.config.settings import Settings
from backend.app.db import models as m
from backend.app.experiments.evaluation import CalibrationObservation, calibration_report
from backend.app.scheduler.locking import LeaseLost, fenced_site_work
from backend.app.services import control, measurement

log = logging.getLogger(__name__)
DAILY_CYCLE = "daily-cycle"
INTEGRITY_CRAWL = "integrity-crawl"
EVENING_MEASUREMENT = "evening-measurement"
WEEKLY_REVIEW = "weekly-review"
JOB_NAMES = (DAILY_CYCLE, INTEGRITY_CRAWL, EVENING_MEASUREMENT, WEEKLY_REVIEW)
# A restart/duplicate tick must not reset a provider's retry budget. This is a
# hard ceiling per site + job + scheduled period, counted from immutable audit
# attempts under the site lease, not from process memory or caller-supplied IDs.
MAX_OBSERVATION_ATTEMPTS = 3


def verify_worker_tick(factory: sessionmaker, settings: Settings) -> None:
    """Recheck the worker DB capability before every scheduled dispatch."""
    if settings.environment != "production":
        return
    if settings.service_role != "worker":
        raise ValueError("Production scheduled dispatch requires the forced worker service role")
    from backend.app.db.readiness import verify_database_readiness

    with factory() as session:
        verify_database_readiness(session.connection(), environment=settings.environment, profile="worker")


def period_key(job_name: str, now: datetime, timezone_name: str) -> str:
    if job_name not in JOB_NAMES:
        raise ValueError("Unknown scheduled job")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Scheduler clock must be timezone-aware")
    day = now.astimezone(ZoneInfo(timezone_name)).date()
    if job_name == WEEKLY_REVIEW:
        day -= timedelta(days=day.weekday())
    return f"scheduled:{job_name}:{day.isoformat()}"


def integrity_crawl(session: Session, site: m.Site, settings: Settings) -> dict:
    from backend.app.integrations.crawler.client import Crawler
    from backend.app.integrations.fixtures import fixture_crawler

    fixture = site.config_json.get("source_mode") == "fixture"
    if fixture:
        crawler = fixture_crawler(control.cms_for_site(session, site, settings).list_pages())
    else:
        crawler = Crawler(site.base_url, max_bytes=min(settings.crawl_max_bytes, 5_000_000))
    try:
        batch = crawler.crawl_site(max_pages=min(settings.max_crawl_pages, settings.max_pages_per_crawl, 50))
    finally:
        crawler.client.close()
    evidence_id = control.ingest_batch(session, site, "crawl", batch)
    return {"evidence_id": evidence_id, "rows": len(batch.rows), "complete": batch.complete,
            "quality_flags": batch.quality_flags, "source_mode": "fixture" if fixture else "live",
            "model_calls": 0, "production_mutations": 0}


def evening_measurement(session: Session, site: m.Site, settings: Settings) -> dict:
    return {
        **measurement.evaluate_due_experiments(session, site.id, authority_updates_allowed=False),
        "model_calls": 0,
    }


def weekly_review(session: Session, site: m.Site, settings: Settings) -> dict:
    """Review stored evidence; do not manufacture outcomes or issue new authority."""
    latest = session.scalar(select(m.StrategyVersion).where(m.StrategyVersion.site_id == site.id)
                            .order_by(m.StrategyVersion.version.desc()).limit(1))
    mission = session.scalar(select(m.MissionState).where(m.MissionState.site_id == site.id))
    rows = list(session.scalars(select(m.CalibrationRecord).where(m.CalibrationRecord.site_id == site.id)))
    calibration = calibration_report([CalibrationObservation(
        experiment_id=row.experiment_id or row.id, agent_id=row.agent_name, action_category=row.action_category,
        predicted_confidence=row.predicted_confidence, succeeded=row.succeeded,
        adjudicated=row.evaluable and row.outcome_json.get("independent") is True,
        is_primary_outcome=row.outcome_json.get("is_primary_outcome") is True,
        adjudication_source=row.outcome_json.get("adjudication_source"),
    ) for row in rows])
    recent_failures = session.scalar(select(func.count()).select_from(m.FailureCase).where(
        m.FailureCase.site_id == site.id, m.FailureCase.created_at >= m.utcnow() - timedelta(days=7))) or 0
    open_opportunities = session.scalar(select(func.count()).select_from(m.Opportunity).where(
        m.Opportunity.site_id == site.id, m.Opportunity.status == "open")) or 0
    reductions = [group for group in calibration["groups"] if group["autonomy_recommendation"] == "reduce"]
    unknowns = list(mission.unknowns_json or []) if mission else ["Mission state unavailable"]
    blockers = list(mission.blockers_json or []) if mission else []
    result = {"strategy_version": latest.version if latest else None, "calibration": calibration,
              "autonomy_reduction_recommendations": reductions, "recent_failures": recent_failures,
              "open_opportunities": open_opportunities, "unknowns": unknowns, "blockers": blockers,
              "requires_human_review": bool(reductions or recent_failures or unknowns or blockers),
              "autonomy_level": site.autonomy_level, "automatic_graduation": False,
              "model_calls": 0, "production_mutations": 0}
    decision = m.DecisionLog(site_id=site.id, owner="weekly-strategy-review",
        decision="Review calibration reductions and unresolved blockers" if result["requires_human_review"] else "Maintain current bounded strategy",
        rationale="Weekly review of existing canonical strategy, independent primary outcomes and failure records",
        evidence_ids_json=list(latest.evidence_ids_json or []) if latest else [],
        uncertainty_json=unknowns + blockers + calibration["quality_flags"],
        alternatives_json=["Collect more evidence", "Human review of a narrower scope", "Maintain current authority"],
        regret_json={"wrong_action_cost": "Promoting authority without independent outcome evidence",
                     "delayed_action_cost": "Useful changes can wait while evidence remains unresolved"})
    session.add(decision)
    session.flush()
    result["decision_log_id"] = decision.id
    return result


OPERATIONS = {INTEGRITY_CRAWL: integrity_crawl, EVENING_MEASUREMENT: evening_measurement, WEEKLY_REVIEW: weekly_review}


def run_observation_job(
    factory: sessionmaker, site_id: str, job_name: str, settings: Settings, *,
    scheduled_for: datetime | None = None, owner: str | None = None,
) -> dict:
    if job_name not in OPERATIONS:
        raise ValueError("This path only accepts observation/review jobs")
    key = period_key(job_name, scheduled_for or m.utcnow(), settings.scheduler_timezone)
    owner = owner or f"scheduler:{uuid4()}"
    with factory() as session:
        control.site_record(session, site_id)
        previous = session.scalar(select(m.JobRun).where(
            m.JobRun.site_id == site_id, m.JobRun.job_name == job_name, m.JobRun.idempotency_key == key))
        if previous and previous.status in {"completed", "retry_exhausted"}:
            return {"job_id": previous.id, "status": previous.status, "idempotent_replay": True, "result": previous.result_json}
        with fenced_site_work(session, site_id, owner=owner, ttl_seconds=settings.scheduler_lease_seconds,
                              session_factory=factory) as handle:
            if handle is None:
                return {"status": "lease_busy", "site_id": site_id, "job_name": job_name}
            site = control.site_record(session, site_id)
            # Re-check under the lease: another worker may have completed after
            # our optimistic read but before this acquisition. The session uses
            # expire_on_commit=False, so refresh an already-cached failed/run
            # row instead of accidentally retrying its stale in-memory state.
            job = session.scalar(select(m.JobRun).where(m.JobRun.site_id == site_id,
                m.JobRun.job_name == job_name, m.JobRun.idempotency_key == key)
                .execution_options(populate_existing=True))
            if job and job.status in {"completed", "retry_exhausted"}:
                return {"job_id": job.id, "status": job.status, "idempotent_replay": True, "result": job.result_json}
            recovered = job is not None
            if job is not None:
                attempts = list(session.scalars(select(m.Action).where(
                    m.Action.site_id == site_id, m.Action.kind == "scheduled_observation",
                    m.Action.idempotency_key.startswith(f"job:{job.id}:attempt:"))
                    .order_by(m.Action.created_at, m.Action.id)))
                if len(attempts) >= MAX_OBSERVATION_ATTEMPTS:
                    job.status, job.completed_at, job.error = "retry_exhausted", m.utcnow(), "RetryBudgetExhausted"
                    job.result_json = {**job.result_json, "retry_attempts": len(attempts),
                                       "retry_limit": MAX_OBSERVATION_ATTEMPTS, "requires_human_review": True}
                    session.add(m.ActionEvent(site_id=site_id, action_id=attempts[-1].id,
                        event_type="scheduler_retry_exhausted", details_json={"job_id": job.id,
                        "attempts": len(attempts), "production_write": False}))
                    session.add(m.FailureCase(site_id=site_id, action_id=attempts[-1].id,
                        category="scheduled_retry_exhausted", predicted=f"Complete bounded {job_name}",
                        actual="Scheduled period exhausted its durable attempt budget",
                        root_cause="Repeated failure or interrupted observation; inspect prior attempt events",
                        agent_responsible="backend-scheduler", detection_method="Durable per-period retry admission",
                        preventative_change="Operator diagnosis; no automatic reset or new key for this period",
                        details_json={"job_id": job.id, "attempts": len(attempts), "production_write": False}))
                    session.commit()
                    return {"job_id": job.id, "status": "retry_exhausted", "result": job.result_json}
                if job.status == "running":
                    # Reacquiring an expired/released site lease does not make
                    # the old attempt a success. Preserve ambiguity before an
                    # explicitly repeatable read/review is attempted again.
                    last_action_id = attempts[-1].id if attempts else None
                    if last_action_id:
                        session.add(m.ActionEvent(site_id=site_id, action_id=last_action_id,
                            event_type="scheduler_interrupted", details_json={"job_id": job.id,
                            "state": "unknown", "production_write": False}))
                    session.add(m.FailureCase(site_id=site_id, action_id=last_action_id,
                        category="scheduled_observation_interrupted", predicted=f"Complete {job_name}",
                        actual="Prior attempt has no durable terminal outcome",
                        root_cause="Worker termination, lease loss or unavailable audit storage; exact cause unknown",
                        agent_responsible="backend-scheduler", detection_method="Lease-fenced restart reconciliation",
                        preventative_change="Retain unknown outcome; only repeatable observations may retry within the cap",
                        details_json={"job_id": job.id, "state": "unknown", "production_write": False}))
            if job is None:
                job = m.JobRun(site_id=site_id, job_name=job_name, idempotency_key=key, owner=owner)
                session.add(job)
            else:
                job.owner, job.status, job.error, job.completed_at = owner, "running", None, None
            session.flush()
            action = m.Action(site_id=site_id, kind="scheduled_observation", risk="LOW", actor="backend-scheduler",
                reason=f"Scheduled {job_name}", idempotency_key=f"job:{job.id}:attempt:{handle.fencing_token}",
                payload_json={"job_id": job.id, "job_name": job_name, "recovered": recovered,
                              "fencing_token": handle.fencing_token, "production_write": False})
            session.add(action)
            session.flush()
            session.add(m.ActionEvent(site_id=site_id, action_id=action.id, event_type="scheduler_started",
                                      details_json={"job_id": job.id, "recovered": recovered}))
            job.result_json = {"audit_action_id": action.id, "fencing_token": handle.fencing_token}
            session.commit()
            job_id, action_id = job.id, action.id
            try:
                result = OPERATIONS[job_name](session, site, settings)
                job.status, job.completed_at = "completed", m.utcnow()
                job.result_json = {**result, "audit_action_id": action_id, "fencing_token": handle.fencing_token}
                session.add(m.ActionEvent(site_id=site_id, action_id=action_id, event_type="scheduler_completed",
                    details_json={"job_id": job_id, "model_calls": 0, "production_mutations": 0}))
                session.commit()
                return {"job_id": job_id, "status": "completed", "result": job.result_json}
            except LeaseLost:
                session.rollback()
                return {"job_id": job_id, "status": "lease_lost", "retry_safe": True}
            except Exception as error:
                session.rollback()
                job = session.get(m.JobRun, job_id)
                job.status, job.completed_at, job.error = "failed", m.utcnow(), type(error).__name__
                session.add(m.ActionEvent(site_id=site_id, action_id=action_id, event_type="scheduler_failed",
                    details_json={"job_id": job_id, "error_type": type(error).__name__, "state": "unknown"}))
                session.add(m.FailureCase(site_id=site_id, category="scheduled_observation",
                    predicted=f"Complete {job_name}", actual=f"Stopped: {type(error).__name__}",
                    root_cause="Requires operator diagnosis", agent_responsible="backend-scheduler",
                    detection_method="Scheduled job exception boundary",
                    preventative_change="Review provider availability and job evidence before retry",
                    details_json={"job_id": job_id, "job_name": job_name, "production_write": False}))
                try:
                    session.commit()
                except LeaseLost:
                    session.rollback()
                    return {"job_id": job_id, "status": "lease_lost", "retry_safe": True}
                return {"job_id": job_id, "status": "failed", "error_type": type(error).__name__}
def run_scheduled_job(
    factory: sessionmaker, settings: Settings, job_name: str, *,
    site_id: str | None = None, scheduled_for: datetime | None = None,
) -> list[dict]:
    if job_name not in JOB_NAMES:
        raise ValueError("Unknown scheduled job")
    verify_worker_tick(factory, settings)
    now = scheduled_for or m.utcnow()
    key = period_key(job_name, now, settings.scheduler_timezone)
    with factory() as session:
        if site_id:
            control.site_record(session, site_id)
            site_ids = [site_id]
        else:
            site_ids = list(session.scalars(select(m.Site.id).order_by(m.Site.id)))
    results = []
    for identifier in site_ids:
        try:
            if job_name == DAILY_CYCLE:
                with factory() as session:
                    # The core service owns this site's durable fenced lease and
                    # all paid-call/action admission; never wrap it in a second one.
                    result = control.run_cycle(session, identifier, settings, idempotency_key=key)
            else:
                result = run_observation_job(factory, identifier, job_name, settings, scheduled_for=now)
            results.append({"site_id": identifier, **result})
            log.info("scheduled_job job=%s site=%s status=%s", job_name, identifier, result.get("status"))
        except Exception as error:
            log.error("scheduled_job_failed job=%s site=%s error_type=%s", job_name, identifier, type(error).__name__)
            results.append({"site_id": identifier, "status": "failed", "error_type": type(error).__name__})
    return results
