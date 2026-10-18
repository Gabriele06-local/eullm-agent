"""The jobs table becomes a durable queue: work is written as 'queued'
before it runs, so a restart of the dashboard or of the worker loses
nothing, and the worker serves the tenants in turn.

- claim_job: the next job to run (of one tenant, if given), at most one
  running per tenant, the tenant served least recently first, and within
  a tenant the work a person asked for before the hourly tick. Claims are serialised by an
  advisory lock, so two workers never start two jobs of the same tenant.
- requeue_stale: a running job whose heartbeat stopped (worker killed) is
  queued again until max_attempts, then failed.

Both read across tenants, like active_tenants(), so they are SECURITY
DEFINER and return only what the worker needs.

Revision ID: 0011
Revises: 0010
"""

from alembic import op

from editor.migrations.rls import APP_ROLE

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE editor.jobs
            ADD COLUMN args jsonb,
            ADD COLUMN priority integer NOT NULL DEFAULT 0,
            ADD COLUMN run_after timestamptz NOT NULL DEFAULT now(),
            ADD COLUMN attempts integer NOT NULL DEFAULT 0,
            ADD COLUMN max_attempts integer NOT NULL DEFAULT 3,
            ADD COLUMN heartbeat_at timestamptz,
            ADD COLUMN worker text;
        ALTER TABLE editor.jobs DROP CONSTRAINT jobs_status_check;
        ALTER TABLE editor.jobs ADD CONSTRAINT jobs_status_check
            CHECK (status IN ('queued', 'running', 'done', 'failed'));
        CREATE INDEX jobs_queued ON editor.jobs (run_after) WHERE status = 'queued';
        CREATE INDEX jobs_served ON editor.jobs (tenant_id, started_at DESC) WHERE worker IS NOT NULL;

        CREATE FUNCTION editor.claim_job(p_worker text, p_stale_seconds integer, p_tenant text DEFAULT NULL)
            RETURNS TABLE (id bigint, tenant_id text, kind text, subject text, args jsonb, attempts integer)
            LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, editor
            AS $$
            #variable_conflict use_column
            DECLARE
                v_tenant text;
                v_id bigint;
            BEGIN
                PERFORM pg_advisory_xact_lock(hashtext('editor-claim'));
                SELECT q.tenant_id INTO v_tenant
                  FROM editor.jobs q JOIN editor.tenants t ON t.tenant_id = q.tenant_id
                 WHERE q.status = 'queued' AND q.run_after <= now() AND t.active
                   AND (p_tenant IS NULL OR q.tenant_id = p_tenant)
                   AND NOT EXISTS (SELECT 1 FROM editor.jobs r
                                    WHERE r.tenant_id = q.tenant_id AND r.status = 'running'
                                      AND r.heartbeat_at > now() - make_interval(secs => p_stale_seconds))
                 GROUP BY q.tenant_id
                 ORDER BY (SELECT max(r.started_at) FROM editor.jobs r
                            WHERE r.tenant_id = q.tenant_id AND r.worker IS NOT NULL) NULLS FIRST,
                          q.tenant_id
                 LIMIT 1;
                IF v_tenant IS NULL THEN
                    RETURN;
                END IF;
                SELECT q.id INTO v_id FROM editor.jobs q
                 WHERE q.tenant_id = v_tenant AND q.status = 'queued' AND q.run_after <= now()
                 ORDER BY q.priority DESC, q.id
                 LIMIT 1;
                RETURN QUERY
                    UPDATE editor.jobs j
                       SET status = 'running', started_at = now(), heartbeat_at = now(),
                           worker = p_worker, attempts = j.attempts + 1, error = NULL
                     WHERE j.id = v_id
                 RETURNING j.id, j.tenant_id, j.kind, j.subject, j.args, j.attempts;
            END $$;

        CREATE FUNCTION editor.requeue_stale(p_stale_seconds integer) RETURNS integer
            LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, editor
            AS $$
            WITH stale AS (
                UPDATE editor.jobs SET
                    status = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END,
                    error = CASE WHEN attempts < max_attempts THEN 'interrupted, queued again'
                                 ELSE 'interrupted: the worker stopped and no attempts are left' END,
                    finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE now() END,
                    run_after = now(), worker = NULL
                 WHERE status = 'running' AND heartbeat_at IS NOT NULL
                   AND heartbeat_at < now() - make_interval(secs => p_stale_seconds)
                RETURNING 1)
            SELECT count(*)::integer FROM stale $$;

        REVOKE ALL ON FUNCTION editor.claim_job(text, integer, text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION editor.requeue_stale(integer) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION editor.claim_job(text, integer, text) TO {APP_ROLE};
        GRANT EXECUTE ON FUNCTION editor.requeue_stale(integer) TO {APP_ROLE};
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION editor.requeue_stale(integer);
        DROP FUNCTION editor.claim_job(text, integer, text);
        DELETE FROM editor.jobs WHERE status = 'queued';
        DROP INDEX editor.jobs_served;
        DROP INDEX editor.jobs_queued;
        ALTER TABLE editor.jobs DROP CONSTRAINT jobs_status_check;
        ALTER TABLE editor.jobs ADD CONSTRAINT jobs_status_check
            CHECK (status IN ('running', 'done', 'failed'));
        ALTER TABLE editor.jobs
            DROP COLUMN worker,
            DROP COLUMN heartbeat_at,
            DROP COLUMN max_attempts,
            DROP COLUMN attempts,
            DROP COLUMN run_after,
            DROP COLUMN priority,
            DROP COLUMN args;
        """
    )
