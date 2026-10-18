"""The queue functions see the queue.

claim_job and requeue_stale run as the schema owner (SECURITY DEFINER) and
must see the jobs of every tenant, but editor.jobs forced row level
security on its owner too: on a server, where the owner is not a
superuser, they found nothing and queued work never started. As for
editor.tenants and editor.access_tokens, row level security stays on for
the application role and no longer applies to the owner.

Revision ID: 0013
Revises: 0012
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE editor.jobs NO FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("ALTER TABLE editor.jobs FORCE ROW LEVEL SECURITY")
