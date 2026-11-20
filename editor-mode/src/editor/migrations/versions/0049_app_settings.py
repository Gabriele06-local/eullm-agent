"""Settings of the installation entered from the console (/admin ›
Impostazioni) instead of the .env file: email, Mollie, covers, Search
Console, Telegram, Turnstile, time zone, retention. The secret ones are
encrypted with EDITOR_SECRET_KEY (appsettings.py). Only the owner
connection reads them: the application role has no grant.

Revision ID: 0049
Revises: 0048
"""

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS editor.app_settings (
            name text PRIMARY KEY CHECK (name ~ '^EDITOR_[A-Z0-9_]{1,60}$'),
            value text NOT NULL,
            secret boolean NOT NULL DEFAULT false,
            key_id text,
            updated_at timestamptz NOT NULL DEFAULT now(),
            updated_by text
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE editor.app_settings;")
