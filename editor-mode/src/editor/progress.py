"""Where a queued job is: the step it is on, written to its row so the
dashboard can show it while it runs ("Lettura degli articoli: 7 di 25").

The worker sets the job being run (queue.run); code anywhere below calls
`step` with a marked text and its values, and nothing happens when no job
is running (tests, the command line). Call it outside an open session: on
SQLite a second writer would wait for the first.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from sqlalchemy import update

from . import models as m

log = logging.getLogger(__name__)

_current: ContextVar = ContextVar("editor_job", default=None)


@contextmanager
def running(db, tenant_id: str, job_id: int):
    token = _current.set((db, tenant_id, job_id))
    try:
        yield
    finally:
        _current.reset(token)


def step(text: str, **values) -> None:
    """Record the step the running job is on; never fails the work."""
    cur = _current.get()
    if cur is None:
        return
    db, tenant_id, job_id = cur
    try:
        with db.tenant(tenant_id) as s:
            s.execute(update(m.jobs).where(m.jobs.c.id == job_id).values(
                progress={"text": text, "values": values, "at": datetime.now(UTC).isoformat()}))
    except Exception:
        log.exception("progress of job %s not recorded", job_id)
