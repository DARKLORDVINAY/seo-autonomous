"""Shared site-work fencing for API cycles and scheduled observation jobs."""
from __future__ import annotations

from contextlib import contextmanager
import logging
import threading
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from backend.app.db.repositories.leases import LeaseHandle, acquire_lease, release_lease, renew_lease
from backend.app.db.session import make_session_factory

log = logging.getLogger(__name__)


def site_lease_key(site_id: str) -> str:
    return f"site-cycle:{site_id}"


class LeaseLost(RuntimeError):
    """A stale worker cannot commit even if a slow external read finally returns."""


class CommitFence:
    def __init__(self, session: Session, factory: sessionmaker, handle: LeaseHandle, ttl: int):
        self.session, self.factory, self.handle, self.ttl = session, factory, handle, ttl
        self.stopped, self.lost = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._heartbeat, name="seo-site-work-lease", daemon=True)

    def _before_commit(self, session: Session) -> None:
        if self.lost.is_set():
            raise LeaseLost("Site work lease was lost")
        # Conditional UPDATE locks the lease until commit; another acquirer
        # cannot slip between this validation and the protected transaction.
        if renew_lease(session, self.handle, ttl_seconds=self.ttl) is None:
            self.lost.set()
            raise LeaseLost("Site work lease expired before commit")

    def _heartbeat(self) -> None:
        while not self.stopped.wait(max(1, self.ttl / 3)):
            try:
                with self.factory() as session:
                    renewed = renew_lease(session, self.handle, ttl_seconds=self.ttl)
                    session.commit()
                if renewed is None:
                    self.lost.set()
                    return
            except Exception:
                self.lost.set()
                return

    def __enter__(self):
        event.listen(self.session, "before_commit", self._before_commit)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        event.remove(self.session, "before_commit", self._before_commit)
        self.thread.join(timeout=2)


@contextmanager
def fenced_site_work(
    session: Session, site_id: str, *, owner: str | None = None, ttl_seconds: int = 300,
    session_factory: sessionmaker | None = None,
):
    """Yield LeaseHandle, or None when busy, while protecting every session commit.

    This context owns transaction boundaries. Commit successful work explicitly
    inside it. Uncommitted residue is rolled back before the lease is released.
    External mutation uses the separate non-expiring execution-lease protocol.
    """
    factory = session_factory or make_session_factory(session.get_bind())
    handle = acquire_lease(session, site_lease_key(site_id), owner or f"site-worker:{uuid4()}",
                           site_id=site_id, ttl_seconds=ttl_seconds)
    session.commit()
    if handle is None:
        yield None
        return
    try:
        with CommitFence(session, factory, handle, ttl_seconds):
            yield handle
    finally:
        session.rollback()
        try:
            with factory() as cleanup:
                release_lease(cleanup, handle)
                cleanup.commit()
        except Exception as error:
            log.warning("site_lease_release_failed error_type=%s", type(error).__name__)
