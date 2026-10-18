-- Alembic revision 0012: secrets of the tenants, encrypted (vault.py).
CREATE TABLE editor.secrets (
    tenant_id text NOT NULL REFERENCES tenants,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 40),
    ciphertext text NOT NULL,
    key_id text NOT NULL,
    updated_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    PRIMARY KEY (tenant_id, name)
);
