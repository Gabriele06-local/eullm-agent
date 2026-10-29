-- Alembic revision 0029: the free trial, once. The sites already in the
-- newsroom are remembered by trials.check_domain the next time they are read.
CREATE TABLE editor.trial_marks (
    id integer PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('email', 'vat', 'tax_code', 'phone', 'domain')),
    value_hash text NOT NULL,
    newsroom text NOT NULL,
    customer_id integer,
    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX editor.trial_marks_value ON trial_marks (value_hash);
CREATE TABLE editor.trial_refusals (
    id integer PRIMARY KEY,
    newsroom text NOT NULL,
    customer_id integer,
    reasons text NOT NULL,
    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
);
