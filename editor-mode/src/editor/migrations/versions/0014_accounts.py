"""Accounts: people sign in with email and password, belong to one or more
newsrooms with a role, and operators run the service from a console and
an API (a newsroom created when a payment goes through).

Only the owner connection reads and writes these tables: the application
role has no grant on them. Row level security is enabled, not forced,
so a missing grant cannot be worked around by a policy either.

Revision ID: 0014
Revises: 0013
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE editor.users (
            id bigserial PRIMARY KEY,
            email text NOT NULL UNIQUE CHECK (email = lower(email) AND length(email) BETWEEN 3 AND 254),
            name text NOT NULL DEFAULT '',
            password_hash text,
            operator boolean NOT NULL DEFAULT false,
            failed_logins integer NOT NULL DEFAULT 0,
            locked_until timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            disabled_at timestamptz
        );
        CREATE TABLE editor.memberships (
            user_id bigint NOT NULL REFERENCES editor.users ON DELETE CASCADE,
            tenant_id text NOT NULL REFERENCES editor.tenants ON DELETE CASCADE,
            role text NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, tenant_id)
        );
        CREATE TABLE editor.sessions (
            token_sha256 text PRIMARY KEY CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
            user_id bigint NOT NULL REFERENCES editor.users ON DELETE CASCADE,
            tenant_id text REFERENCES editor.tenants ON DELETE CASCADE,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL
        );
        CREATE TABLE editor.invites (
            token_sha256 text PRIMARY KEY CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
            kind text NOT NULL CHECK (kind IN ('invite', 'reset')),
            email text NOT NULL,
            tenant_id text REFERENCES editor.tenants ON DELETE CASCADE,
            role text CHECK (role IN ('owner', 'editor', 'viewer')),
            operator boolean NOT NULL DEFAULT false,
            created_by text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            used_at timestamptz
        );
        CREATE TABLE editor.operator_tokens (
            id bigserial PRIMARY KEY,
            name text NOT NULL,
            token_sha256 text NOT NULL UNIQUE CHECK (token_sha256 ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz
        );
        ALTER TABLE editor.memberships ENABLE ROW LEVEL SECURITY;
        ALTER TABLE editor.sessions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE editor.invites ENABLE ROW LEVEL SECURITY;

        -- The billing system's reference of a newsroom (e.g. a subscription):
        -- asking twice for the same one gives the same newsroom.
        ALTER TABLE editor.tenants ADD COLUMN external_ref text UNIQUE;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE editor.tenants DROP COLUMN external_ref;
        DROP TABLE editor.operator_tokens;
        DROP TABLE editor.invites;
        DROP TABLE editor.sessions;
        DROP TABLE editor.memberships;
        DROP TABLE editor.users;
        """
    )
