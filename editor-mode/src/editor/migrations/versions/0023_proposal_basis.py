"""What a proposal stands on: a topic told by several sources, or one news
item picked for the site (by the model or by the formula), with its score.

Revision ID: 0023
Revises: 0022
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE editor.proposals ADD COLUMN basis jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE editor.proposals DROP COLUMN basis")
