"""Durable, conservative model-call reservations shared by API and scheduler.

Reservations are upper-bound accounting, not a claim about provider billing.
They are committed before the SDK call and are never refunded after ambiguity.
"""
from __future__ import annotations

import math
from datetime import datetime, time, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config.settings import Settings
from backend.app.contracts import ProviderUnavailable, stable_hash, utcnow
from backend.app.db import models as m


def runtime_options(session: Session, site: m.Site, settings: Settings, *, task_id: str | None = None) -> dict:
    from backend.app.agents.runtime import RuntimeBudget
    live = settings.agent_mode == "openai"
    return {
        "mode": "live" if live else "fixture",
        "budget": RuntimeBudget(max_model_calls=min(5, settings.max_agent_calls_per_run)),
        "record_run": run_recorder(session, site.id, settings, task_id=task_id),
        "model": settings.openai_model if live else None,
        "api_key": settings.openai_api_key.get_secret_value() if live and settings.openai_api_key else None,
    }


def _price_bound(site: m.Site, model: str | None) -> float:
    bound = site.config_json.get("model_price_bound", {})
    rate = bound.get("usd_per_million_tokens")
    if (bound.get("verified") is not True or bound.get("model") != model
            or isinstance(rate, bool) or not isinstance(rate, (int, float))
            or not math.isfinite(rate) or rate <= 0 or not bound.get("source")):
        raise ProviderUnavailable("Live model calls require a verified price bound for the selected model")
    return float(rate)


def run_recorder(session: Session, site_id: str, settings: Settings, *, task_id: str | None = None):
    """Return the trusted callback; callers cannot provide run packets over HTTP."""
    def record(packet: dict) -> None:
        run_id = str(UUID(packet["id"]))
        # PostgreSQL serialises all per-site reservations, including API calls
        # outside the scheduled cycle. SQLite is for the single-worker demo.
        site = session.scalar(select(m.Site).where(m.Site.id == site_id).with_for_update()
                              .execution_options(populate_existing=True))
        if site is None:
            raise LookupError("Unknown site")
        row = session.get(m.AgentRun, run_id)
        if row is not None and row.site_id != site_id:
            raise ValueError("Agent run belongs to another site")
        previous = row.result_json if row is not None else {}
        binding_fields = ("id", "cycle_id", "role", "mode", "model", "started_at", "reserved_tokens", "contract", "trace_id")
        binding = stable_hash({field: packet.get(field) for field in binding_fields})
        if row is not None:
            if previous.get("status") == "budget_blocked":
                raise ProviderUnavailable("This invocation was denied; a replay cannot obtain spend authority")
            if binding != previous.get("invocation_binding"):
                raise ValueError("A stable invocation ID cannot be rebound to another paid request")
            if packet.get("status") == "started":
                # Late acknowledgements must not erase completion, and must
                # not charge the same run twice.
                return
            if previous.get("status") != "started":
                if packet.get("status") != previous.get("status"):
                    raise ValueError("A terminal invocation cannot change its verdict")
                return
        reservation = float(previous.get("reserved_cost_upper_bound_usd", 0))
        reserve_calls = int(previous.get("reserved_model_calls", 0))
        rejection = None
        if packet.get("status") == "started" and packet.get("mode") == "live" and row is None:
            try:
                rate = _price_bound(site, packet.get("model"))
                tokens = packet.get("reserved_tokens")
                if isinstance(tokens, bool) or not isinstance(tokens, int) or not 0 < tokens <= 500000:
                    raise ValueError("Invalid model reservation")
                reservation = tokens * rate / 1_000_000
                midnight = datetime.combine(utcnow().date(), time.min, tzinfo=timezone.utc)
                prior = list(session.scalars(select(m.AgentRun).where(
                    m.AgentRun.site_id == site_id, m.AgentRun.started_at >= midnight)))
                used = sum(float(p.result_json.get("reserved_cost_upper_bound_usd", 0)) for p in prior)
                calls = sum(int(p.result_json.get("reserved_model_calls", 0)) for p in prior)
                daily_calls = min(100, max(0, int(site.config_json.get("max_daily_model_calls", 20))))
                if site.config_json.get("automation_suspended") or used + reservation > settings.max_daily_cost_usd or calls >= daily_calls:
                    raise ProviderUnavailable("Live model budget exhausted or automation suspended")
                if task_id:
                    task_runs = session.scalars(select(m.AgentRun).where(m.AgentRun.site_id == site_id, m.AgentRun.task_id == task_id))
                    task_calls = sum(int(p.result_json.get("reserved_model_calls", 0)) for p in task_runs)
                    if task_calls >= settings.max_agent_calls_per_run:
                        raise ProviderUnavailable("This task exhausted its model allowance across all specialist stages")
                reserve_calls = 1
            except (ProviderUnavailable, ValueError, TypeError) as error:
                reservation, reserve_calls = 0.0, 0
                rejection = type(error).__name__
        # Terminal packets cannot overwrite a committed reservation or invent one.
        if packet.get("mode") == "live" and packet.get("llm_attempted") and not reserve_calls:
            raise ValueError("A model result has no durable start reservation")
        stored = {**packet, "reserved_model_calls": reserve_calls,
                  "reserved_cost_upper_bound_usd": reservation,
                  "invocation_binding": binding,
                  "billing_status": "unknown" if packet.get("mode") == "live" else "no_paid_call"}
        if rejection:
            stored.update(status="budget_blocked", rejection_type=rejection, reserved_tokens=0)
        if row is None:
            row = m.AgentRun(id=run_id, site_id=site_id, task_id=task_id,
                             agent_name=packet.get("role", "specialist"), mode=packet.get("mode", "fixture"))
            session.add(row)
        row.model, row.status = packet.get("model"), stored.get("status", "unknown")
        row.contract_json, row.result_json = packet.get("contract", {}), stored
        row.started_at = datetime.fromisoformat(packet["started_at"]) if packet.get("started_at") else utcnow()
        row.completed_at = datetime.fromisoformat(packet["completed_at"]) if packet.get("completed_at") else None
        for key in ("latency_ms", "input_tokens", "output_tokens", "cost_usd", "trace_id"):
            setattr(row, key, packet.get(key))
        row.error = rejection or packet.get("error_type")
        audit = m.Action(site_id=site_id, kind="agent_run", risk="LOW", actor="agent-runtime",
                         reason="Durable model invocation audit", idempotency_key=f"agent:{run_id}:{row.status}",
                         payload_json={"agent_run_id": run_id, "status": row.status,
                                       "reserved_cost_upper_bound_usd": reservation})
        # Stable start/terminal callbacks are idempotent even after transport replay.
        existing = session.scalar(select(m.Action).where(m.Action.site_id == site_id,
                                                          m.Action.idempotency_key == audit.idempotency_key))
        if existing is None:
            session.add(audit)
            session.flush()
            session.add(m.ActionEvent(site_id=site_id, action_id=audit.id, event_type="recorded",
                                      details_json={"scope": "canonical_state", "run": stored}))
        session.commit()
        if rejection:
            raise ProviderUnavailable("Model invocation blocked by durable budget policy")
    return record
