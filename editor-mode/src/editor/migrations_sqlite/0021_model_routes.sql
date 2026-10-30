-- Alembic revision 0032: the model each step uses, chosen in the console.
CREATE TABLE editor.model_routes (
    purpose text PRIMARY KEY,
    model text NOT NULL CHECK (model <> ''),
    updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
);
