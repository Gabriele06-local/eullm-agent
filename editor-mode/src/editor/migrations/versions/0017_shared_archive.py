"""One shared archive of public sources, items, topics and Hype Scores.

Every public source is read once for all newsrooms: it lives in the
reserved tenant `_archive` (shared = true), with its items, their topics
and Hype Scores. A newsroom's own rows in editor.sources are the sources
its sites follow, each linked to the archive source it reads
(archive_id), with the status the newsroom decided and the score for its
site. Newsrooms read the archive (policy archive_read) and may add a
source to it (archive_add); only the collection, running as `_archive`,
changes it.

Existing sources move to the archive, without duplicates, and their
newsrooms keep following them. Items and topics collected so far stay
with their newsroom and age out; new ones go to the archive.

Revision ID: 0017
Revises: 0016
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

ARCHIVE = "_archive"
SHARED = ["sources", "source_items", "observations", "topics", "topic_items", "trend_scores"]
CITING = ["proposal_citations", "draft_claim_sources"]


def upgrade() -> None:
    op.execute(
        f"""
        -- Active, so that the worker runs its hourly collection; not a newsroom,
        -- so it gets no tick of its own.
        INSERT INTO editor.tenants (tenant_id, name, active) VALUES ('{ARCHIVE}', 'Shared archive', true)
            ON CONFLICT (tenant_id) DO NOTHING;
        CREATE OR REPLACE FUNCTION editor.active_tenants() RETURNS SETOF text
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, editor
            AS $$ SELECT tenant_id FROM editor.tenants WHERE active AND tenant_id <> '{ARCHIVE}' ORDER BY tenant_id $$;
        ALTER TABLE editor.sources
            ADD COLUMN shared boolean NOT NULL DEFAULT false,
            ADD COLUMN archive_id bigint REFERENCES editor.sources ON DELETE SET NULL;
        DROP INDEX editor.sources_unique;
        CREATE UNIQUE INDEX sources_unique ON editor.sources
            (tenant_id, coalesce(site_id, 0), kind, url, (config::text), shared);
        CREATE INDEX sources_archive ON editor.sources (archive_id);

        -- The move reads every newsroom's sources: row level security is
        -- lifted for the owner while it runs.
        ALTER TABLE editor.sources NO FORCE ROW LEVEL SECURITY;
        INSERT INTO editor.sources (tenant_id, kind, name, url, config, weight, status, origin, evaluation,
                                    score, evaluated_at, last_item_at, shared)
        SELECT DISTINCT ON (kind, url, config::text)
               '{ARCHIVE}', kind, name, url, config, 1.0, 'active', origin, evaluation, score, evaluated_at,
               last_item_at, true
        FROM editor.sources WHERE NOT shared
        ORDER BY kind, url, config::text, (status = 'active') DESC, score DESC NULLS LAST, id;
        UPDATE editor.sources f SET archive_id = a.id
        FROM editor.sources a
        WHERE a.shared AND NOT f.shared AND a.kind = f.kind AND a.url = f.url AND a.config::text = f.config::text;
        ALTER TABLE editor.sources FORCE ROW LEVEL SECURITY;

        CREATE POLICY archive_add ON editor.sources FOR INSERT
            WITH CHECK (tenant_id = '{ARCHIVE}' AND shared);
        """
    )
    for table in SHARED:
        op.execute(f"CREATE POLICY archive_read ON editor.{table} FOR SELECT USING (tenant_id = '{ARCHIVE}')")
    # The archive's pruning keeps every item a newsroom cites: it sees which
    # items are cited, and nothing else of the newsrooms.
    for table in CITING:
        op.execute(f"CREATE POLICY archive_cited ON editor.{table} FOR SELECT "
                   f"USING (current_setting('app.tenant_id', true) = '{ARCHIVE}')")


def downgrade() -> None:
    for table in CITING:
        op.execute(f"DROP POLICY IF EXISTS archive_cited ON editor.{table}")
    for table in SHARED:
        op.execute(f"DROP POLICY IF EXISTS archive_read ON editor.{table}")
    op.execute(
        f"""
        DROP POLICY IF EXISTS archive_add ON editor.sources;
        ALTER TABLE editor.sources NO FORCE ROW LEVEL SECURITY;
        UPDATE editor.sources SET archive_id = NULL;
        DELETE FROM editor.trend_scores WHERE tenant_id = '{ARCHIVE}';
        DELETE FROM editor.topic_items WHERE tenant_id = '{ARCHIVE}';
        DELETE FROM editor.topics WHERE tenant_id = '{ARCHIVE}';
        DELETE FROM editor.observations WHERE tenant_id = '{ARCHIVE}';
        DELETE FROM editor.source_items WHERE tenant_id = '{ARCHIVE}';
        DELETE FROM editor.sources WHERE tenant_id = '{ARCHIVE}';
        ALTER TABLE editor.sources FORCE ROW LEVEL SECURITY;
        DROP INDEX editor.sources_archive;
        DROP INDEX editor.sources_unique;
        ALTER TABLE editor.sources DROP COLUMN archive_id, DROP COLUMN shared;
        CREATE UNIQUE INDEX sources_unique ON editor.sources
            (tenant_id, coalesce(site_id, 0), kind, url, (config::text));
        DELETE FROM editor.jobs WHERE tenant_id = '{ARCHIVE}';
        DELETE FROM editor.llm_usage WHERE tenant_id = '{ARCHIVE}';
        DELETE FROM editor.tenants WHERE tenant_id = '{ARCHIVE}';
        CREATE OR REPLACE FUNCTION editor.active_tenants() RETURNS SETOF text
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, editor
            AS $$ SELECT tenant_id FROM editor.tenants WHERE active ORDER BY tenant_id $$;
        """
    )
