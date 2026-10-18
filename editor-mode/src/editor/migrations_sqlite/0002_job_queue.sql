-- Alembic revision 0011: the jobs table becomes a durable queue. SQLite
-- cannot change a CHECK constraint, so the table is rebuilt. The claim and
-- the requeue of stale jobs are in queue.py: there is one tenant and one
-- worker, so no function reads across tenants.

CREATE TABLE editor.jobs_new (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    kind text NOT NULL,
    subject text,
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    error text,
    started_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    finished_at text,
    args text,
    priority integer NOT NULL DEFAULT 0,
    run_after text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    attempts integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 3,
    heartbeat_at text,
    worker text
);
INSERT INTO editor.jobs_new (id, tenant_id, kind, subject, status, error, started_at, finished_at)
    SELECT id, tenant_id, kind, subject, status, error, started_at, finished_at FROM editor.jobs;
DROP TABLE editor.jobs;
ALTER TABLE editor.jobs_new RENAME TO jobs;
CREATE INDEX editor.jobs_recent ON jobs (tenant_id, kind, started_at DESC);
CREATE INDEX editor.jobs_queued ON jobs (run_after) WHERE status = 'queued';
