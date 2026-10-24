"""Translations: the languages a site publishes in besides its own, how
many the newsroom's plan allows, and drafts that are the translation of
another draft.

Revision ID: 0024
Revises: 0023
"""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE editor.sites ADD COLUMN languages jsonb")
    op.execute("ALTER TABLE editor.tenants ADD COLUMN max_languages integer")
    # Newsrooms on a plan take its number of languages.
    op.execute("UPDATE editor.tenants t SET max_languages = p.max_languages FROM editor.plans p WHERE p.code = t.plan")
    op.execute("ALTER TABLE editor.drafts ADD COLUMN translation_of bigint REFERENCES editor.drafts ON DELETE CASCADE")
    op.execute("CREATE INDEX drafts_translation_of ON editor.drafts (translation_of) WHERE translation_of IS NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE editor.drafts DROP COLUMN translation_of")
    op.execute("ALTER TABLE editor.sites DROP COLUMN languages")
    op.execute("ALTER TABLE editor.tenants DROP COLUMN max_languages")
