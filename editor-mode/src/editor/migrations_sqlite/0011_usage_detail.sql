-- Alembic revision 0022: model calls with site, article, job, duration and error; reference prices.
ALTER TABLE editor.llm_usage ADD COLUMN site_id integer;
ALTER TABLE editor.llm_usage ADD COLUMN ref text;
ALTER TABLE editor.llm_usage ADD COLUMN job_id integer;
ALTER TABLE editor.llm_usage ADD COLUMN duration_ms integer;
ALTER TABLE editor.llm_usage ADD COLUMN error text;
CREATE INDEX editor.llm_usage_at ON llm_usage (tenant_id, at);
CREATE TABLE editor.model_prices (
    model text PRIMARY KEY,
    input_per_mtok real NOT NULL CHECK (input_per_mtok >= 0),
    output_per_mtok real NOT NULL CHECK (output_per_mtok >= 0),
    note text NOT NULL DEFAULT '',
    updated_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now'))
);
