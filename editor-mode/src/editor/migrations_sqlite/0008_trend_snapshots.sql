-- Alembic revision 0018: the trend of the day, eight snapshots a day.
CREATE TABLE editor.trend_snapshots (
    id integer PRIMARY KEY AUTOINCREMENT,
    tenant_id text NOT NULL REFERENCES tenants,
    taken_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    topics text NOT NULL
);
CREATE INDEX editor.trend_snapshots_taken ON trend_snapshots (tenant_id, taken_at DESC);
