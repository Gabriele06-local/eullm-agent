"""Why a proposal was discarded: too generic and unreliable source join the
reasons already known (already covered, off topic).

Revision ID: 0031
Revises: 0030
"""

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE editor.feedback DROP CONSTRAINT IF EXISTS feedback_verdict_check;
        ALTER TABLE editor.feedback ADD CONSTRAINT feedback_verdict_check CHECK (verdict IN
            ('up', 'down', 'already_covered', 'off_topic', 'too_generic', 'unreliable_source'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE editor.feedback SET verdict = 'down' WHERE verdict IN ('too_generic', 'unreliable_source');
        ALTER TABLE editor.feedback DROP CONSTRAINT IF EXISTS feedback_verdict_check;
        ALTER TABLE editor.feedback ADD CONSTRAINT feedback_verdict_check CHECK (verdict IN
            ('up', 'down', 'already_covered', 'off_topic'));
        """
    )
