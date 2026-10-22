"""What a draft needs to be found by search engines and AI answer engines:
key phrase, search title and description, slug, the questions it answers,
links to the site's own articles and the checks a reviewer reads.

Revision ID: 0019
Revises: 0018
"""

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE editor.drafts ADD COLUMN seo jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE editor.drafts DROP COLUMN seo")
