"""Publish targets of kind github: the article committed to the site's
repository as Markdown with front matter (and its cover image), for the
static sites deployed from it (Vercel, GitHub Pages). A draft-mode
publication opens a pull request instead of committing.

Revision ID: 0027
Revises: 0026
"""

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

KINDS = {
    True: "('wordpress', 'webhook', 'telegram_channel', 'github')",
    False: "('wordpress', 'webhook', 'telegram_channel')",
}


def _constraint(kinds: str) -> str:
    return (f"ALTER TABLE editor.publish_targets DROP CONSTRAINT publish_targets_kind_check;\n"
            f"ALTER TABLE editor.publish_targets ADD CONSTRAINT publish_targets_kind_check CHECK (kind IN {kinds});")


def upgrade() -> None:
    op.execute(_constraint(KINDS[True]))


def downgrade() -> None:
    # The publications of those targets go with them (ON DELETE CASCADE).
    op.execute("DELETE FROM editor.publish_targets WHERE kind = 'github';")
    op.execute(_constraint(KINDS[False]))
