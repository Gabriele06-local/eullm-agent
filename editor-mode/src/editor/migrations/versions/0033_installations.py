"""The installations that asked for the latest release (updates.py): a
random identifier, the installed version, first and last time seen.

Revision ID: 0033
Revises: 0032
"""

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE editor.installations (
            id text PRIMARY KEY CHECK (id ~ '^[0-9a-f]{24}$'),
            version text NOT NULL,
            first_seen timestamptz NOT NULL DEFAULT now(),
            last_seen timestamptz NOT NULL DEFAULT now(),
            checks integer NOT NULL DEFAULT 1
        );
        CREATE INDEX installations_last_seen ON editor.installations (last_seen);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE editor.installations;")
