"""Read interfaces require explicit tenant scope and return bounded state."""
from __future__ import annotations

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from backend.app.db.models import Action, ActionEvent, FailureCase, MissionState, Page, Site


def serialise(row) -> dict:
    return {c.key: getattr(row, c.key) for c in inspect(type(row)).columns}


def get_site(session: Session, site_id: str) -> Site:
    row = session.get(Site, site_id)
    if row is None:
        raise LookupError("Site not found")
    return row


def get_page(session: Session, site_id: str, page_id: str) -> Page:
    row = session.scalar(select(Page).where(Page.site_id == site_id, Page.id == page_id))
    if row is None:
        raise LookupError("Page not found for site")
    return row


def get_mission(session: Session, site_id: str) -> MissionState | None:
    get_site(session, site_id)
    return session.scalar(select(MissionState).where(MissionState.site_id == site_id))


def action_history(session: Session, site_id: str, action_id: str) -> list[ActionEvent]:
    action = session.scalar(select(Action).where(Action.site_id == site_id, Action.id == action_id))
    if action is None:
        raise LookupError("Action not found for site")
    return list(session.scalars(select(ActionEvent).where(
        ActionEvent.site_id == site_id, ActionEvent.action_id == action_id,
    ).order_by(ActionEvent.created_at, ActionEvent.id)))


def relevant_failures(session: Session, site_id: str, category: str | None = None, limit: int = 25) -> list[FailureCase]:
    if not 1 <= limit <= 100:
        raise ValueError("Limit must be between 1 and 100")
    stmt = select(FailureCase).where(FailureCase.site_id == site_id)
    if category:
        stmt = stmt.where(FailureCase.category == category)
    return list(session.scalars(stmt.order_by(FailureCase.created_at.desc()).limit(limit)))
