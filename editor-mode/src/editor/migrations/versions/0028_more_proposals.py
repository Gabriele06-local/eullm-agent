"""Six proposals a day instead of three: the sites saved with the old
default (3) get the new one, a number someone chose stays as it is.

Revision ID: 0028
Revises: 0027
"""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def _set(old: int, new: int) -> None:
    op.execute(f"""
        ALTER TABLE editor.editorial_profiles NO FORCE ROW LEVEL SECURITY;
        UPDATE editor.editorial_profiles
            SET body = jsonb_set(body, '{{settings,proposals_per_day}}', '{new}')
            WHERE status IN ('approved', 'draft') AND body->'settings'->'proposals_per_day' = '{old}'::jsonb;
        ALTER TABLE editor.editorial_profiles FORCE ROW LEVEL SECURITY;
        """)


def upgrade() -> None:
    _set(3, 6)


def downgrade() -> None:
    _set(6, 3)
