"""The plan a customer chose on the public site while on trial.

A signup from the site starts the trial whatever plan the visitor picked:
a paid plan starts only with its payment. The choice is kept here and
offered first in /account.

Revision ID: 0016
Revises: 0015
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE editor.subscriptions ADD COLUMN desired_plan text, ADD COLUMN desired_interval text")


def downgrade() -> None:
    op.execute("ALTER TABLE editor.subscriptions DROP COLUMN desired_plan, DROP COLUMN desired_interval")
