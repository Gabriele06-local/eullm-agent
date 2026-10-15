-- EuLLM Agent Core on SQLite (local installation, one user): the same
-- tables as the PostgreSQL schema `core`, times in milliseconds since the
-- epoch, JSON and lists stored as text.

CREATE TABLE runs (
    id            TEXT PRIMARY KEY,
    tenant        TEXT NOT NULL,
    profile       TEXT NOT NULL,
    source        TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('queued', 'running', 'waiting_approval', 'succeeded', 'failed')),
    input         TEXT NOT NULL,
    output        TEXT,
    error         TEXT,
    iterations    INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost          REAL,
    tainted       INTEGER NOT NULL DEFAULT 0,
    instance      TEXT,
    created_at    INTEGER NOT NULL,
    finished_at   INTEGER
);
CREATE INDEX runs_tenant_created ON runs (tenant, created_at DESC);

CREATE TABLE llm_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant        TEXT NOT NULL,
    run_id        TEXT REFERENCES runs (id) ON DELETE CASCADE,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    duration_ms   INTEGER NOT NULL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost          REAL,
    error         TEXT,
    created_at    INTEGER NOT NULL
);
CREATE INDEX llm_calls_run ON llm_calls (run_id);
CREATE INDEX llm_calls_tenant_created ON llm_calls (tenant, created_at);

CREATE TABLE tool_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant          TEXT NOT NULL,
    run_id          TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    tool            TEXT NOT NULL,
    argument_keys   TEXT NOT NULL,
    decision        TEXT NOT NULL,
    decision_reason TEXT,
    approval_id     TEXT,
    duration_ms     INTEGER NOT NULL,
    output_bytes    INTEGER,
    error           TEXT,
    created_at      INTEGER NOT NULL
);
CREATE INDEX tool_calls_run ON tool_calls (run_id);

CREATE TABLE approvals (
    id            TEXT PRIMARY KEY,
    tenant        TEXT NOT NULL,
    run_id        TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    tool          TEXT NOT NULL,
    arguments     TEXT NOT NULL,
    reason        TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'denied', 'expired')),
    decided_by    TEXT,
    decision_note TEXT,
    created_at    INTEGER NOT NULL,
    decided_at    INTEGER
);
CREATE INDEX approvals_tenant_status ON approvals (tenant, status, created_at DESC);

CREATE TABLE fetches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant      TEXT NOT NULL,
    url         TEXT NOT NULL,
    status      INTEGER,
    bytes       INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    error       TEXT,
    created_at  INTEGER NOT NULL
);
CREATE INDEX fetches_tenant_created ON fetches (tenant, created_at);

-- Append-only record of everything above. Updates and deletes are refused.
CREATE TABLE audit_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant     TEXT NOT NULL,
    run_id     TEXT,
    event      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX audit_events_tenant_created ON audit_events (tenant, created_at);

CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only');
END;

-- Live processes: each one refreshes `seen_at` while it runs. At startup a
-- process closes only the runs of processes that stopped refreshing.
CREATE TABLE instances (
    id      TEXT PRIMARY KEY,
    seen_at INTEGER NOT NULL
);
