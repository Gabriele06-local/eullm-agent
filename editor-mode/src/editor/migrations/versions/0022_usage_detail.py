"""Every model call with the site, article, job, duration and error it
belongs to, and a reference price per model set in the console.

model_prices has no tenant: only the owner connection reads and writes it,
as for plans (row level security enabled, not forced, no grant to the
application role).

Revision ID: 0022
Revises: 0021
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE editor.llm_usage
            ADD COLUMN site_id bigint,
            ADD COLUMN ref text,
            ADD COLUMN job_id bigint,
            ADD COLUMN duration_ms integer,
            ADD COLUMN error text;
        CREATE INDEX llm_usage_at ON editor.llm_usage (tenant_id, at);
        CREATE TABLE editor.model_prices (
            model text PRIMARY KEY,
            input_per_mtok double precision NOT NULL CHECK (input_per_mtok >= 0),
            output_per_mtok double precision NOT NULL CHECK (output_per_mtok >= 0),
            note text NOT NULL DEFAULT '',
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        ALTER TABLE editor.model_prices ENABLE ROW LEVEL SECURITY;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE editor.model_prices;
        DROP INDEX editor.llm_usage_at;
        ALTER TABLE editor.llm_usage DROP COLUMN site_id, DROP COLUMN ref, DROP COLUMN job_id,
            DROP COLUMN duration_ms, DROP COLUMN error;
        """
    )
