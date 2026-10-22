"""The trend of the day: eight times a day (07:00 first) the archive keeps
the topics that are rising most, with their news. A day's snapshots are
the catalogue of what the sector talked about, hour by hour, and the
newest one is the trend each newsroom is offered.

Revision ID: 0018
Revises: 0017
"""

from alembic import op

from editor.migrations.rls import APP_ROLE, tenant_rls

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE editor.trend_snapshots (
            id bigserial PRIMARY KEY,
            tenant_id text NOT NULL REFERENCES editor.tenants,
            taken_at timestamptz NOT NULL DEFAULT now(),
            topics jsonb NOT NULL
        );
        CREATE INDEX trend_snapshots_taken ON editor.trend_snapshots (tenant_id, taken_at DESC);
        """
    )
    op.execute(tenant_rls("trend_snapshots"))
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA editor TO {APP_ROLE}")
    op.execute("CREATE POLICY archive_read ON editor.trend_snapshots FOR SELECT USING (tenant_id = '_archive')")


def downgrade() -> None:
    op.execute("DROP TABLE editor.trend_snapshots")
