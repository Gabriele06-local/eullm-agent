-- Limits of a tenant set through the API (PUT /v1/tenants/{name}/limits)
-- by the application that serves it, so a new customer needs no change to
-- the configuration and no restart. Limits in api.tenants take precedence.
CREATE TABLE core.tenant_limits (
    tenant text PRIMARY KEY,
    limits jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
