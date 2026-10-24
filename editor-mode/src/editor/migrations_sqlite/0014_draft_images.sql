-- Alembic revision 0025: cover images, one per draft.
CREATE TABLE editor.draft_images (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    draft_id integer NOT NULL UNIQUE REFERENCES drafts ON DELETE CASCADE,
    token text NOT NULL UNIQUE,
    prompt text NOT NULL,
    alt text NOT NULL DEFAULT '',
    model text NOT NULL,
    mime text NOT NULL,
    width integer,
    height integer,
    content blob NOT NULL,
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now'))
);
