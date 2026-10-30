-- Alembic revision 0033: the installations that asked for the latest release.
CREATE TABLE editor.installations (
    id text PRIMARY KEY,
    version text NOT NULL,
    first_seen text NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen text NOT NULL DEFAULT CURRENT_TIMESTAMP,
    checks integer NOT NULL DEFAULT 1
);
CREATE INDEX editor.installations_last_seen ON installations (last_seen);
