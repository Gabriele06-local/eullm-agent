"""A tenant the operator deactivates loses access at once: its tokens stop
resolving, not only its scheduled work.

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION editor.resolve_token(digest text)
            RETURNS TABLE (tenant_id text, name text, role text)
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, editor
            AS $$ SELECT a.tenant_id, a.name, a.role FROM editor.access_tokens a
                  JOIN editor.tenants t ON t.tenant_id = a.tenant_id
                  WHERE a.token_sha256 = digest AND a.revoked_at IS NULL AND t.active $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION editor.resolve_token(digest text)
            RETURNS TABLE (tenant_id text, name text, role text)
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, editor
            AS $$ SELECT tenant_id, name, role FROM editor.access_tokens
                  WHERE token_sha256 = digest AND revoked_at IS NULL $$;
        """
    )
