"""Secrets of the tenants, encrypted (vault.py), so an owner can enter a
publishing credential from the dashboard. The key is never in the
database.

Revision ID: 0012
Revises: 0011
"""

from alembic import op

from editor.migrations.rls import tenant_rls

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE editor.secrets (
            tenant_id text NOT NULL REFERENCES editor.tenants,
            name text NOT NULL CHECK (name ~ '^[A-Z0-9]+(_[A-Z0-9]+)*$' AND length(name) <= 40),
            ciphertext text NOT NULL,
            key_id text NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, name)
        );
        """
    )
    op.execute(tenant_rls("secrets"))


def downgrade() -> None:
    op.execute("DROP TABLE editor.secrets")
