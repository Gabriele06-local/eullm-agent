"""The step a running job is on and what it produced, for the dashboard.

Revision ID: 0020
Revises: 0019
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE editor.jobs ADD COLUMN progress jsonb, ADD COLUMN result jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE editor.jobs DROP COLUMN progress, DROP COLUMN result")
