"""Database-coordinated leases. Explicit commits belong to the caller.

Commit acquisition *before* beginning work; renew in a separate short transaction.
Fence every final write using owns_lease(). An expired worker must stop work.
These expiring locks are for repeatable observation jobs. External CMS changes use
the non-expiring ExecutionLease, which survives a crash until reconciliation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from backend.app.db.models import JobLease, utcnow


@dataclass(frozen=True)
class LeaseHandle:
    key: str
    owner: str
    token: str
    fencing_token: int
    site_id: str | None
    expires_at: datetime


def acquire_lease(
    session: Session, key: str, owner: str, *, site_id: str | None = None,
    ttl_seconds: int = 300, now: datetime | None = None,
) -> LeaseHandle | None:
    if not key or not owner or not 1 <= ttl_seconds <= 3600:
        raise ValueError("Lease key/owner required; TTL must be 1–3600 seconds")
    now = now or utcnow()
    if now.tzinfo is None:
        raise ValueError("Lease clock must be timezone-aware")
    expires = now + timedelta(seconds=ttl_seconds)
    token = str(uuid4())
    dialect = session.get_bind().dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert if dialect == "sqlite" else None
    if insert is None:
        raise ValueError("Lease storage supports PostgreSQL and SQLite only")
    inserted = session.execute(insert(JobLease).values(
        key=key, site_id=site_id, owner=owner, token=token, fencing_token=1,
        acquired_at=now, expires_at=expires,
    ).on_conflict_do_nothing(index_elements=["key"]).returning(JobLease.fencing_token)).scalar_one_or_none()
    if inserted is not None:
        return LeaseHandle(key, owner, token, inserted, site_id, expires)
    statement = update(JobLease).where(
        JobLease.key == key, JobLease.site_id == site_id, JobLease.expires_at <= now,
    ).values(owner=owner, token=token, fencing_token=JobLease.fencing_token + 1,
             acquired_at=now, expires_at=expires).returning(JobLease.fencing_token)
    fence = session.execute(statement).scalar_one_or_none()
    if fence is None:
        return None
    return LeaseHandle(key, owner, token, fence, site_id, expires)


def _ownership(handle: LeaseHandle, now: datetime):
    return (
        JobLease.key == handle.key, JobLease.site_id == handle.site_id,
        JobLease.owner == handle.owner, JobLease.token == handle.token,
        JobLease.fencing_token == handle.fencing_token, JobLease.expires_at > now,
    )


def owns_lease(session: Session, handle: LeaseHandle, *, now: datetime | None = None) -> bool:
    return session.execute(select(JobLease.key).where(*_ownership(handle, now or utcnow()))).scalar_one_or_none() is not None


def renew_lease(session: Session, handle: LeaseHandle, *, ttl_seconds: int = 300, now: datetime | None = None) -> LeaseHandle | None:
    if not 1 <= ttl_seconds <= 3600:
        raise ValueError("Lease TTL must be 1–3600 seconds")
    now = now or utcnow()
    expires = now + timedelta(seconds=ttl_seconds)
    result = session.execute(update(JobLease).where(*_ownership(handle, now)).values(expires_at=expires))
    if result.rowcount != 1:
        return None
    return LeaseHandle(handle.key, handle.owner, handle.token, handle.fencing_token, handle.site_id, expires)


def release_lease(session: Session, handle: LeaseHandle, *, now: datetime | None = None) -> bool:
    """Expire rather than delete: fencing tokens increase monotonically forever."""
    now = now or utcnow()
    result = session.execute(update(JobLease).where(*_ownership(handle, now)).values(expires_at=now))
    return result.rowcount == 1
