-- Alembic revision 0026: one feed per site, by token.
CREATE TABLE editor.feeds (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    site_id integer NOT NULL UNIQUE REFERENCES sites ON DELETE CASCADE,
    token text NOT NULL UNIQUE,
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now'))
);
