"""Durable queue of background work, kept in editor.jobs on PostgreSQL and
on SQLite alike.

Work asked from the dashboard (site analysis, source discovery, drafts,
publications) and the hourly tick of every tenant is written as a 'queued'
row before anything runs, so a restart of the dashboard or of the worker
loses nothing: the worker picks it up when it is back.

Fairness: a tenant runs one job at a time and, among the tenants with work
waiting, the one served least recently goes first; within a tenant, work a
person asked for comes before the hourly tick. A tenant with fifty drafts
waiting takes one worker slot and the others keep going.

A running job refreshes its heartbeat. One whose heartbeat stops (worker
killed, machine restarted) is queued again until max_attempts, then failed.
An error raised by the work itself is not retried: a draft that failed on
the model is not paid for twice, a person asks for it again.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, select, text, update

from . import models as m
from .quotas import lock

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 30
STALE_SECONDS = 300  # no heartbeat for this long: the worker is gone
POLL_SECONDS = 2.0
PRIORITY_PERSON = 10  # asked from the dashboard or the API
PRIORITY_TICK = 0

# kind -> fn(tenant, subject, args); raises on failure.
Handlers = dict[str, Callable[[str, str, dict], None]]


@dataclass(frozen=True)
class Job:
    id: int
    tenant_id: str
    kind: str
    subject: str
    args: dict
    attempts: int


def enqueue(db, tenant_id: str, kind: str, subject: str, args: dict | None = None,
            priority: int = PRIORITY_PERSON) -> int:
    """Queue a job and return its id. The same work already waiting (same
    kind and subject) is not queued twice: its id is returned instead."""
    with db.tenant(tenant_id) as s:
        lock(s, f"editor-queue:{tenant_id}")
        j = m.jobs.c
        waiting = s.execute(select(j.id).where(j.tenant_id == tenant_id, j.kind == kind, j.subject == subject,
                                               j.status == "queued")).scalar()
        if waiting is not None:
            return waiting
        now = datetime.now(UTC)
        return s.execute(insert(m.jobs).values(
            tenant_id=tenant_id, kind=kind, subject=subject, args=args or {}, status="queued", priority=priority,
            started_at=now, run_after=now, attempts=0, max_attempts=3,
        ).returning(j.id)).scalar_one()


def claim(db, worker: str, tenant_id: str | None = None) -> Job | None:
    """The next job to run, marked running; only of `tenant_id` if given."""
    if not db.sqlite:
        with db.engine.begin() as conn:
            r = conn.execute(text("SELECT * FROM editor.claim_job(:w, :stale, :t)"),
                             {"w": worker, "stale": STALE_SECONDS, "t": tenant_id}).first()
        return None if r is None else Job(r.id, r.tenant_id, r.kind, r.subject, r.args or {}, r.attempts)
    # SQLite: one tenant, and BEGIN IMMEDIATE already serialises the claim.
    with db.engine.begin() as conn:
        j = m.jobs.c
        now = datetime.now(UTC)
        busy = conn.execute(select(j.id).where(j.status == "running", j.heartbeat_at.is_not(None),
                                               j.heartbeat_at > now - timedelta(seconds=STALE_SECONDS))).first()
        if busy is not None:
            return None
        q = select(m.jobs).where(j.status == "queued", j.run_after <= now)
        if tenant_id is not None:
            q = q.where(j.tenant_id == tenant_id)
        r = conn.execute(q.order_by(j.priority.desc(), j.id).limit(1)).first()
        if r is None:
            return None
        conn.execute(update(m.jobs).where(j.id == r.id).values(
            status="running", started_at=now, heartbeat_at=now, worker=worker, attempts=r.attempts + 1, error=None))
        return Job(r.id, r.tenant_id, r.kind, r.subject, r.args or {}, r.attempts + 1)


def heartbeat(db, job: Job) -> None:
    with db.tenant(job.tenant_id) as s:
        s.execute(update(m.jobs).where(m.jobs.c.id == job.id, m.jobs.c.status == "running")
                  .values(heartbeat_at=datetime.now(UTC)))


def finish(db, job: Job, error: str | None = None) -> None:
    with db.tenant(job.tenant_id) as s:
        s.execute(update(m.jobs).where(m.jobs.c.id == job.id).values(
            status="failed" if error else "done", error=error, finished_at=datetime.now(UTC)))


def requeue_stale(db, stale_seconds: int = STALE_SECONDS) -> int:
    """Queue again (or fail, when no attempts are left) the jobs whose worker
    stopped sending heartbeats."""
    if not db.sqlite:
        with db.engine.begin() as conn:
            return conn.execute(text("SELECT editor.requeue_stale(:s)"), {"s": stale_seconds}).scalar_one()
    with db.engine.begin() as conn:
        j = m.jobs.c
        now = datetime.now(UTC)
        stale = conn.execute(select(j.id, j.attempts, j.max_attempts).where(
            j.status == "running", j.heartbeat_at.is_not(None),
            j.heartbeat_at < now - timedelta(seconds=stale_seconds))).all()
        for r in stale:
            again = r.attempts < r.max_attempts
            conn.execute(update(m.jobs).where(j.id == r.id).values(
                status="queued" if again else "failed", run_after=now, worker=None,
                finished_at=None if again else now,
                error="interrupted, queued again" if again else "interrupted: the worker stopped and no attempts are left"))
        return len(stale)


def run(db, job: Job, handlers: Handlers) -> bool:
    """Run one claimed job, refreshing its heartbeat meanwhile; True if it succeeded."""
    fn = handlers.get(job.kind)
    if fn is None:
        finish(db, job, f"no handler for job kind {job.kind!r}")
        return False
    stop = threading.Event()

    def beat():
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                heartbeat(db, job)
            except Exception:
                log.exception("heartbeat of job %s failed", job.id)

    beater = threading.Thread(target=beat, name=f"heartbeat-{job.id}", daemon=True)
    beater.start()
    try:
        fn(job.tenant_id, job.subject, job.args)
    except Exception as e:
        log.exception("%s failed for tenant %s (%s)", job.kind, job.tenant_id, job.subject)
        finish(db, job, f"{type(e).__name__}: {e}"[:500])
        return False
    finally:
        stop.set()
        beater.join()
    finish(db, job)
    return True


def run_next(db, handlers: Handlers, worker: str = "inline", tenant_id: str | None = None) -> bool:
    """Claim and run one job; False when nothing is ready."""
    job = claim(db, worker, tenant_id)
    if job is None:
        return False
    run(db, job, handlers)
    return True


def drain(db, handlers: Handlers, tenant_id: str | None = None, limit: int = 1000) -> int:
    """Run queued jobs, of one tenant if given, until none is ready (tests, `editor tick`)."""
    n = 0
    while n < limit and run_next(db, handlers, tenant_id=tenant_id):
        n += 1
    return n


def active_tenants(db) -> list[str]:
    # On PostgreSQL only active_tenants() reads across tenants.
    q = ("SELECT tenant_id FROM editor.tenants WHERE active ORDER BY tenant_id" if db.sqlite
         else "SELECT * FROM editor.active_tenants()")
    with db.engine.connect() as conn:
        return list(conn.execute(text(q)).scalars())


def enqueue_ticks(db, now: datetime | None = None) -> int:
    """Queue the hourly tick of every active tenant. A tick still waiting from
    the previous hour is not queued twice."""
    hour = (now or datetime.now(UTC)).replace(minute=0, second=0, microsecond=0)
    tenants = active_tenants(db)
    for tenant in tenants:
        enqueue(db, tenant, "tick", "hourly", {"queued_for": hour.isoformat()}, priority=PRIORITY_TICK)
    return len(tenants)


class Worker:
    """`concurrency` threads that claim and run jobs, plus the clock that
    queues every tenant's tick at the start of each hour. SIGTERM and SIGINT
    stop the claims; the running jobs finish before the process exits (a job
    cut short by a hard kill is queued again by requeue_stale)."""

    def __init__(self, db, handlers: Handlers, concurrency: int = 1, name: str | None = None):
        self.db = db
        self.handlers = handlers
        self.concurrency = max(1, concurrency)
        self.name = name or f"{socket.gethostname()}:{os.getpid()}"
        self.stopping = threading.Event()

    def stop(self, *_):
        if not self.stopping.is_set():
            log.info("stopping: no new jobs, waiting for the running ones")
        self.stopping.set()

    def _loop(self, n: int):
        while not self.stopping.is_set():
            try:
                busy = run_next(self.db, self.handlers, f"{self.name}/{n}")
            except Exception:
                log.exception("worker loop error")
                busy = False
            if not busy:
                self.stopping.wait(POLL_SECONDS)

    def _clock(self):
        last_hour = None
        while not self.stopping.is_set():
            try:
                requeue_stale(self.db)
                hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
                if hour != last_hour:
                    log.info("queued the tick of %d tenants", enqueue_ticks(self.db, hour))
                    last_hour = hour
            except Exception:
                log.exception("clock error")
            self.stopping.wait(60)

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        if self.db.sqlite:
            # The only worker of the file: what is "running" was cut short by its last stop.
            requeue_stale(self.db, 0)
        threads = [threading.Thread(target=self._clock, name="clock", daemon=True)]
        threads += [threading.Thread(target=self._loop, args=(n,), name=f"worker-{n}") for n in range(self.concurrency)]
        for t in threads:
            t.start()
        log.info("worker %s running with %d slots", self.name, self.concurrency)
        for t in threads[1:]:
            t.join()
