"""The durable job queue: nothing is lost on a restart, a tenant runs one job
at a time, tenants are served in turn, a dead worker's job comes back."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from editor import models as m, queue, repo


def status(db, tenant, job_id):
    with db.tenant(tenant) as s:
        return s.execute(select(m.jobs.c.status, m.jobs.c.error, m.jobs.c.attempts)
                         .where(m.jobs.c.id == job_id)).first()


def recorder(calls, fail=()):
    def handle(kind):
        def fn(tenant, subject, args):
            calls.append((tenant, kind, subject))
            if kind in fail:
                raise RuntimeError(f"{kind} broke")
        return fn
    return {k: handle(k) for k in ("analysis", "discovery", "draft", "publish", "tick")}


def test_queued_work_waits_for_the_worker_and_is_not_queued_twice(db, tenant):
    a = queue.enqueue(db, tenant, "analysis", "blog.example")
    assert queue.enqueue(db, tenant, "analysis", "blog.example") == a  # same work already waiting
    d = queue.enqueue(db, tenant, "draft", "proposal 1", {"proposal_id": 1})
    assert status(db, tenant, a).status == "queued"
    # A new process (the worker after a restart) finds the work in the database.
    calls = []
    assert queue.drain(db, recorder(calls), tenant) == 2
    assert [c[1] for c in calls] == ["analysis", "draft"]
    assert status(db, tenant, a).status == "done" and status(db, tenant, d).status == "done"
    # Done, so the same work can be asked again.
    assert queue.enqueue(db, tenant, "analysis", "blog.example") != a


def test_work_asked_by_a_person_comes_before_the_tick(db, tenant):
    queue.enqueue(db, tenant, "tick", "hourly", priority=queue.PRIORITY_TICK)
    queue.enqueue(db, tenant, "draft", "proposal 7", {"proposal_id": 7})
    calls = []
    queue.drain(db, recorder(calls), tenant)
    assert [c[1] for c in calls] == ["draft", "tick"]


def test_a_failure_is_recorded_and_not_retried(db, tenant):
    j = queue.enqueue(db, tenant, "draft", "proposal 3", {"proposal_id": 3})
    calls = []
    assert queue.drain(db, recorder(calls, fail={"draft"}), tenant) == 1
    row = status(db, tenant, j)
    assert row.status == "failed" and "draft broke" in row.error and row.attempts == 1
    assert queue.drain(db, recorder(calls), tenant) == 0


def test_unknown_kind_fails_instead_of_staying_queued(db, tenant):
    j = queue.enqueue(db, tenant, "nonsense", "x")
    queue.drain(db, recorder([]), tenant)
    assert status(db, tenant, j).status == "failed"


def test_one_job_at_a_time_per_tenant(db, tenant):
    queue.enqueue(db, tenant, "draft", "proposal 1", {"proposal_id": 1})
    queue.enqueue(db, tenant, "draft", "proposal 2", {"proposal_id": 2})
    first = queue.claim(db, "w1", tenant)
    assert first is not None and first.args == {"proposal_id": 1}
    assert queue.claim(db, "w2", tenant) is None  # the tenant is busy
    queue.finish(db, first)
    assert queue.claim(db, "w2", tenant).args == {"proposal_id": 2}


def test_a_dead_workers_job_is_queued_again_then_failed(db, tenant):
    j = queue.enqueue(db, tenant, "analysis", "blog.example")

    def die():
        job = queue.claim(db, "w", tenant)
        assert job is not None and job.id == j
        with db.tenant(tenant) as s:  # no heartbeat for ten minutes
            s.execute(update(m.jobs).where(m.jobs.c.id == j).values(
                heartbeat_at=datetime.now(UTC) - timedelta(minutes=10)))
        return job

    for attempt in (1, 2):
        die()
        assert queue.requeue_stale(db) >= 1
        row = status(db, tenant, j)
        assert row.status == "queued" and row.attempts == attempt and "queued again" in row.error
    die()
    queue.requeue_stale(db)
    row = status(db, tenant, j)
    assert row.status == "failed" and row.attempts == 3


def test_jobs_tracked_inline_do_not_block_the_queue(db, tenant):
    from editor import jobs

    with db.tenant(tenant) as s:  # a CLI run that crashed long ago, still "running"
        s.execute(m.jobs.insert().values(tenant_id=tenant, kind="analysis", subject="old", status="running",
                                         started_at=datetime.now(UTC) - timedelta(days=1)))
    queue.enqueue(db, tenant, "draft", "proposal 1", {"proposal_id": 1})
    assert queue.claim(db, "w", tenant) is not None
    with db.tenant(tenant) as s:
        assert {j["status"] for j in jobs.recent(s)} >= {"running", "failed"}


@pytest.mark.postgres
def test_tenants_are_served_in_turn(db, tenant):
    other = f"{tenant}-b"
    with db.tenant(other) as s:
        repo.ensure_tenant(s, other, other)
    for n in range(5):
        queue.enqueue(db, tenant, "draft", f"proposal {n}", {"proposal_id": n})
    queue.enqueue(db, other, "draft", "proposal 99", {"proposal_id": 99})
    served = []
    for _ in range(3):
        job = queue.claim(db, "w", None)
        # Leftovers of other tests may share the database: look at ours only.
        while job is not None and job.tenant_id not in (tenant, other):
            queue.finish(db, job)
            job = queue.claim(db, "w", None)
        served.append(job.tenant_id)
        queue.finish(db, job)
    # The tenant with five drafts does not hold the queue: the other one is
    # served right after its first job.
    assert served[:2] in ([tenant, other], [other, tenant])


@pytest.mark.postgres
def test_two_workers_never_run_two_jobs_of_one_tenant(db, tenant):
    from concurrent.futures import ThreadPoolExecutor

    for n in range(6):
        queue.enqueue(db, tenant, "draft", f"proposal {n}", {"proposal_id": n})
    with ThreadPoolExecutor(6) as pool:
        got = [j for j in pool.map(lambda i: queue.claim(db, f"w{i}", tenant), range(6)) if j is not None]
    assert len(got) == 1


@pytest.mark.postgres
def test_an_inactive_tenants_work_waits(db, tenant):
    from conftest import set_plan

    queue.enqueue(db, tenant, "draft", "proposal 1", {"proposal_id": 1})
    set_plan(db, tenant, active=False)
    assert queue.claim(db, "w", tenant) is None
    set_plan(db, tenant, active=True)
    assert queue.claim(db, "w", tenant) is not None


def test_ticks_are_queued_once_per_tenant(db, tenant):
    queue.enqueue_ticks(db)
    queue.enqueue_ticks(db)
    with db.tenant(tenant) as s:
        n = s.execute(select(m.jobs.c.id).where(m.jobs.c.kind == "tick", m.jobs.c.status == "queued")).all()
    assert len(n) == 1


def test_the_worker_runs_queued_work_and_stops_cleanly(db, tenant):
    import threading
    import time

    calls = []
    j = queue.enqueue(db, tenant, "analysis", "blog.example")
    w = queue.Worker(db, recorder(calls), concurrency=2)
    threads = [threading.Thread(target=w._loop, args=(n,)) for n in range(2)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 20
    while status(db, tenant, j).status != "done" and time.monotonic() < deadline:
        time.sleep(0.1)
    w.stop()
    for t in threads:
        t.join(10)
    assert status(db, tenant, j).status == "done"
    assert not any(t.is_alive() for t in threads)


def test_the_worker_log_says_what_runs(db, tenant, caplog):
    import logging

    queue.enqueue(db, tenant, "analysis", "blog.example")
    with caplog.at_level(logging.INFO, logger="editor.queue"):
        queue.drain(db, recorder([]), tenant)
    text_ = caplog.text
    assert f"analysis started for tenant {tenant} (blog.example), attempt 1" in text_
    assert f"analysis done for tenant {tenant} (blog.example)" in text_


@pytest.mark.postgres
def test_the_queue_works_with_an_owner_that_is_not_a_superuser(db, pg_urls, tenant):
    # Tests migrate as a superuser, which row level security never applies
    # to; on a server the owner is an ordinary role. Run the queue functions
    # as such an owner.
    import psycopg

    j = queue.enqueue(db, tenant, "analysis", "owner.example")
    role = "editor_queue_owner_test"
    with psycopg.connect(pg_urls[0], autocommit=True) as conn:
        conn.execute(f"DROP ROLE IF EXISTS {role}")
        conn.execute(f"CREATE ROLE {role} NOSUPERUSER NOBYPASSRLS")
        conn.execute(f"GRANT USAGE ON SCHEMA editor TO {role}")
        conn.execute(f"GRANT ALL ON ALL TABLES IN SCHEMA editor TO {role}")
        for fn in ("claim_job(text, integer, text)", "requeue_stale(integer)"):
            conn.execute(f"ALTER FUNCTION editor.{fn} OWNER TO {role}")
        for table in ("jobs", "tenants"):
            conn.execute(f"ALTER TABLE editor.{table} OWNER TO {role}")
        try:
            job = queue.claim(db, "w", tenant)
            assert job is not None and job.id == j
            queue.finish(db, job)
        finally:
            for fn in ("claim_job(text, integer, text)", "requeue_stale(integer)"):
                conn.execute(f"ALTER FUNCTION editor.{fn} OWNER TO CURRENT_USER")
            for table in ("jobs", "tenants"):
                conn.execute(f"ALTER TABLE editor.{table} OWNER TO CURRENT_USER")
            conn.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA editor FROM {role}")
            conn.execute(f"REVOKE USAGE ON SCHEMA editor FROM {role}")
            conn.execute(f"DROP ROLE {role}")
