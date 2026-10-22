"""A newsroom runs two jobs at a time instead of one.

Three sites added together were analysed one after the other, each
waiting minutes for the model while the person saw them stand still in
the queue. claim_job now lets a tenant run two jobs at once, never two
of the same work (same kind and subject) and never the hourly tick and a
collection on request together, which read the same sources. The tenant
served least recently still goes first, so one newsroom cannot hold every
worker slot.

Revision ID: 0021
Revises: 0020
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

FUNCTION = """
CREATE OR REPLACE FUNCTION editor.claim_job(p_worker text, p_stale_seconds integer, p_tenant text DEFAULT NULL)
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
           AND (SELECT count(*) FROM editor.jobs r
                 WHERE r.tenant_id = q.tenant_id AND r.status = 'running'
                   AND r.heartbeat_at > now() - make_interval(secs => p_stale_seconds)) < {per_tenant}
           AND NOT EXISTS (SELECT 1 FROM editor.jobs r
                            WHERE r.tenant_id = q.tenant_id AND r.status = 'running'
                              AND r.heartbeat_at > now() - make_interval(secs => p_stale_seconds)
                              AND ((r.kind = q.kind AND r.subject IS NOT DISTINCT FROM q.subject)
                                   OR (r.kind IN ('tick', 'collect') AND q.kind IN ('tick', 'collect'))))
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
           AND NOT EXISTS (SELECT 1 FROM editor.jobs r
                            WHERE r.tenant_id = q.tenant_id AND r.status = 'running'
                              AND r.heartbeat_at > now() - make_interval(secs => p_stale_seconds)
                              AND ((r.kind = q.kind AND r.subject IS NOT DISTINCT FROM q.subject)
                                   OR (r.kind IN ('tick', 'collect') AND q.kind IN ('tick', 'collect'))))
         ORDER BY q.priority DESC, q.id
         LIMIT 1;
        RETURN QUERY
            UPDATE editor.jobs j
               SET status = 'running', started_at = now(), heartbeat_at = now(),
                   worker = p_worker, attempts = j.attempts + 1, error = NULL, progress = NULL
             WHERE j.id = v_id
         RETURNING j.id, j.tenant_id, j.kind, j.subject, j.args, j.attempts;
    END $$;
"""


def upgrade() -> None:
    op.execute(FUNCTION.replace("{per_tenant}", "2"))


def downgrade() -> None:
    op.execute(FUNCTION.replace("{per_tenant}", "1").replace(", progress = NULL", ""))
