-- PostgreSQL migration 0004: limits of a tenant set through the API.
CREATE TABLE tenant_limits (
    tenant text PRIMARY KEY,
    limits text NOT NULL,
    updated_at integer NOT NULL
);
