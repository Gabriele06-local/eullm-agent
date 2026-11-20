-- Alembic revision 0049: settings of the installation entered from the console (appsettings.py).
CREATE TABLE IF NOT EXISTS editor.app_settings (
    name text PRIMARY KEY,
    value text NOT NULL,
    secret boolean NOT NULL DEFAULT 0,
    key_id text,
    updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by text
);
