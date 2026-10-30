-- Alembic revision 0031: two more reasons to discard a proposal. SQLite
-- cannot change a CHECK: the table is made again with the new one.
CREATE TABLE editor.feedback_new (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    site_id integer REFERENCES sites ON DELETE CASCADE,
    target text NOT NULL CHECK (target IN ('proposal', 'topic', 'source', 'draft')),
    target_id integer NOT NULL,
    verdict text NOT NULL CHECK (verdict IN ('up', 'down', 'already_covered', 'off_topic', 'too_generic',
                                             'unreliable_source')),
    note text,
    given_by text NOT NULL,
    given_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now'))
);
INSERT INTO editor.feedback_new SELECT id, tenant_id, site_id, target, target_id, verdict, note, given_by, given_at
    FROM editor.feedback;
DROP TABLE editor.feedback;
ALTER TABLE editor.feedback_new RENAME TO feedback;
