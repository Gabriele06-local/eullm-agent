-- Alembic revision 0014: accounts, newsroom memberships, sessions, invites,
-- operator API keys, and the billing reference of a tenant.
CREATE TABLE editor.users (
    id integer PRIMARY KEY,
    email text NOT NULL UNIQUE CHECK (email = lower(email) AND length(email) BETWEEN 3 AND 254),
    name text NOT NULL DEFAULT '',
    password_hash text,
    operator boolean NOT NULL DEFAULT 0,
    failed_logins integer NOT NULL DEFAULT 0,
    locked_until text,
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    disabled_at text
);
CREATE TABLE editor.memberships (
    user_id integer NOT NULL REFERENCES users ON DELETE CASCADE,
    tenant_id text NOT NULL REFERENCES tenants ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    PRIMARY KEY (user_id, tenant_id)
);
CREATE TABLE editor.sessions (
    token_sha256 text PRIMARY KEY,
    user_id integer NOT NULL REFERENCES users ON DELETE CASCADE,
    tenant_id text REFERENCES tenants ON DELETE CASCADE,
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    expires_at text NOT NULL
);
CREATE TABLE editor.invites (
    token_sha256 text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('invite', 'reset')),
    email text NOT NULL,
    tenant_id text REFERENCES tenants ON DELETE CASCADE,
    role text CHECK (role IN ('owner', 'editor', 'viewer')),
    operator boolean NOT NULL DEFAULT 0,
    created_by text NOT NULL,
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    expires_at text NOT NULL,
    used_at text
);
CREATE TABLE editor.operator_tokens (
    id integer PRIMARY KEY,
    name text NOT NULL,
    token_sha256 text NOT NULL UNIQUE,
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    revoked_at text
);
ALTER TABLE editor.tenants ADD COLUMN external_ref text;
CREATE UNIQUE INDEX editor.tenants_external_ref ON tenants (external_ref);
