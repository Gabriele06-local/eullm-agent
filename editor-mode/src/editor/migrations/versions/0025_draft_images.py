"""Cover images: one per draft, generated for the article and kept here,
served to the dashboard and to the sites by an unguessable token.

Revision ID: 0025
Revises: 0024
"""

from alembic import op

from editor.migrations.rls import APP_ROLE, tenant_rls

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE editor.draft_images (
            id bigserial PRIMARY KEY,
            tenant_id text NOT NULL REFERENCES editor.tenants,
            draft_id bigint NOT NULL UNIQUE REFERENCES editor.drafts ON DELETE CASCADE,
            token text NOT NULL UNIQUE,
            prompt text NOT NULL,
            alt text NOT NULL DEFAULT '',
            model text NOT NULL,
            mime text NOT NULL,
            width integer,
            height integer,
            content bytea NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    op.execute(tenant_rls("draft_images"))
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA editor TO {APP_ROLE}")
    # The image is fetched by the sites (WordPress, webhooks) without a login:
    # by its token alone, which the function below looks up across tenants.
    op.execute(
        f"""
        CREATE FUNCTION editor.image_by_token(t text) RETURNS TABLE (mime text, content bytea)
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, editor
            AS $$ SELECT mime, content FROM editor.draft_images WHERE token = t $$;
        ALTER TABLE editor.draft_images NO FORCE ROW LEVEL SECURITY;
        REVOKE ALL ON FUNCTION editor.image_by_token(text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION editor.image_by_token(text) TO {APP_ROLE};
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION editor.image_by_token(text)")
    op.execute("DROP TABLE editor.draft_images")
