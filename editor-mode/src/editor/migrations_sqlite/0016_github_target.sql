-- Alembic revision 0027: publish targets of kind github, the article
-- committed to the site's repository for the static sites deployed from it.
-- SQLite cannot change a CHECK constraint, so the table is rebuilt. The
-- child table (publications) is rebuilt with it and dropped first: with
-- foreign keys on, dropping publish_targets while publications still
-- references it would cascade-delete the publications.

CREATE TABLE editor.publish_targets_new (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    site_id integer NOT NULL REFERENCES sites ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('wordpress', 'webhook', 'telegram_channel', 'github')),
    name text NOT NULL,
    -- Never secrets: credentials are read from the environment.
    config text NOT NULL DEFAULT '{}',
    secret_env text,
    enabled integer NOT NULL DEFAULT 1,
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now'))
);
INSERT INTO editor.publish_targets_new (id, tenant_id, site_id, kind, name, config, secret_env, enabled, created_at)
    SELECT id, tenant_id, site_id, kind, name, config, secret_env, enabled, created_at FROM editor.publish_targets;

CREATE TABLE editor.publications_new (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    draft_id integer NOT NULL REFERENCES drafts ON DELETE CASCADE,
    target_id integer NOT NULL REFERENCES publish_targets_new ON DELETE CASCADE,
    mode text NOT NULL DEFAULT 'draft' CHECK (mode IN ('draft', 'publish')),
    payload text NOT NULL,
    status text NOT NULL DEFAULT 'pending_approval'
        CHECK (status IN ('pending_approval', 'approved', 'sending', 'rejected', 'published', 'failed')),
    requested_by text NOT NULL,
    requested_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    decided_by text,
    decided_at text,
    decision_note text,
    published_at text,
    external_id text,
    external_url text,
    error text,
    attempts integer NOT NULL DEFAULT 0,
    -- Nothing goes out without a person's decision.
    CHECK (status IN ('pending_approval') OR decided_by IS NOT NULL)
);
INSERT INTO editor.publications_new (id, tenant_id, draft_id, target_id, mode, payload, status, requested_by,
                                     requested_at, decided_by, decided_at, decision_note, published_at, external_id,
                                     external_url, error, attempts)
    SELECT id, tenant_id, draft_id, target_id, mode, payload, status, requested_by, requested_at, decided_by,
           decided_at, decision_note, published_at, external_id, external_url, error, attempts
    FROM editor.publications;

DROP TABLE editor.publications;
DROP TABLE editor.publish_targets;
ALTER TABLE editor.publications_new RENAME TO publications;
ALTER TABLE editor.publish_targets_new RENAME TO publish_targets;
CREATE UNIQUE INDEX editor.publications_once ON publications (draft_id, target_id, mode)
    WHERE status IN ('pending_approval', 'approved', 'sending', 'published');
