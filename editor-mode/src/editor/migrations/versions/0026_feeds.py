"""Feeds: one per site, the approved articles for the automation tools
(Zapier, Make, n8n) and the readers that fetch them, by an unguessable token.

Revision ID: 0026
Revises: 0025
"""

from alembic import op

from editor.migrations.rls import APP_ROLE, tenant_rls

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE editor.feeds (
            id bigserial PRIMARY KEY,
            tenant_id text NOT NULL REFERENCES editor.tenants,
            site_id bigint NOT NULL UNIQUE REFERENCES editor.sites ON DELETE CASCADE,
            token text NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(tenant_rls("feeds"))
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA editor TO {APP_ROLE}")
    # The tools fetch the feed without a login: the function below finds the
    # newsroom and the site of a token, the articles are then read as that newsroom.
    op.execute(
        f"""
        CREATE FUNCTION editor.feed_by_token(t text) RETURNS TABLE (tenant_id text, site_id bigint)
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, editor
            AS $$ SELECT tenant_id, site_id FROM editor.feeds WHERE token = t $$;
        ALTER TABLE editor.feeds NO FORCE ROW LEVEL SECURITY;
        REVOKE ALL ON FUNCTION editor.feed_by_token(text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION editor.feed_by_token(text) TO {APP_ROLE};
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION editor.feed_by_token(text)")
    op.execute("DROP TABLE editor.feeds")
