"""A trace of background work: what ran, when, and how it ended.

Queued jobs (queue.py) live in the same table. Every step of a tenant's
tick, and work started from the command line, runs inside `track`: a
failure is recorded with its error instead of leaving the user with
"started" forever, and the scheduler reads the last successful run of a
step to know whether it is due. A queued job that loses its worker is put
back in the queue by queue.requeue_stale, so only tracked steps are
reported as interrupted here.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, insert, select, update

from . import models as m

log = logging.getLogger(__name__)

# A job still "running" after this long was interrupted (restart, crash).
STALE_AFTER = timedelta(hours=2)


@contextmanager
def track(db, tenant_id: str, kind: str, subject: str | None = None, reraise: bool = False):
    """Record a job around the block. Errors are recorded and logged; they
    propagate only with ``reraise``."""
    with db.tenant(tenant_id) as s:
        job_id = s.execute(insert(m.jobs).values(tenant_id=tenant_id, kind=kind, subject=subject)
                           .returning(m.jobs.c.id)).scalar_one()
    try:
        yield job_id
    except Exception as e:
        log.exception("%s failed for tenant %s (%s)", kind, tenant_id, subject)
        _finish(db, tenant_id, job_id, "failed", f"{type(e).__name__}: {e}"[:500])
        if reraise:
            raise
    else:
        _finish(db, tenant_id, job_id, "done", None)


def _finish(db, tenant_id: str, job_id: int, status: str, error: str | None) -> None:
    with db.tenant(tenant_id) as s:
        s.execute(update(m.jobs).where(m.jobs.c.id == job_id).values(
            status=status, error=error, finished_at=datetime.now(UTC)))


def last_done(s, kind: str) -> datetime | None:
    """When the last successful job of this kind finished."""
    return s.execute(select(func.max(m.jobs.c.finished_at)).where(
        m.jobs.c.kind == kind, m.jobs.c.status == "done")).scalar()


def recent(s, limit: int = 20, now: datetime | None = None) -> list[dict]:
    """Latest jobs, newest first; long-running ones are reported as interrupted."""
    now = now or datetime.now(UTC)
    out = []
    for r in s.execute(select(m.jobs).order_by(m.jobs.c.id.desc()).limit(limit)).all():
        status, error = r.status, r.error
        if status == "running" and r.heartbeat_at is None and r.started_at < now - STALE_AFTER:
            status, error = "failed", "interrupted: no result after two hours"
        out.append({"id": r.id, "kind": r.kind, "subject": r.subject, "status": status, "error": error,
                    "started_at": r.started_at, "finished_at": r.finished_at})
    return out
