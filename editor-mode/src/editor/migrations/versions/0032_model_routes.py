"""The model each step uses, chosen in the console: a short step (proposals,
topic names) can go to a fast model and the drafts to a stronger one, among
the models of core.yaml. No row: the Core's default.

Revision ID: 0032
Revises: 0031
"""

from alembic import op

from editor.migrations.rls import APP_ROLE

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE editor.model_routes (
            purpose text PRIMARY KEY,
            model text NOT NULL CHECK (model <> ''),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        GRANT SELECT ON editor.model_routes TO {APP_ROLE};
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE editor.model_routes;")
