-- Alembic revision 0015: customers, resellers, plans, subscriptions, payments.
CREATE TABLE editor.resellers (
    id integer PRIMARY KEY,
    name text NOT NULL,
    email text NOT NULL,
    discount_pct real NOT NULL DEFAULT 0 CHECK (discount_pct BETWEEN 0 AND 100),
    notes text NOT NULL DEFAULT '',
    active boolean NOT NULL DEFAULT 1,
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now'))
);
CREATE TABLE editor.customers (
    id integer PRIMARY KEY,
    reseller_id integer REFERENCES resellers ON DELETE RESTRICT,
    external_ref text,
    kind text NOT NULL DEFAULT 'company' CHECK (kind IN ('company', 'person')),
    name text NOT NULL,
    email text NOT NULL,
    phone text NOT NULL DEFAULT '',
    vat_number text NOT NULL DEFAULT '',
    tax_code text NOT NULL DEFAULT '',
    address text NOT NULL DEFAULT '',
    postal_code text NOT NULL DEFAULT '',
    city text NOT NULL DEFAULT '',
    province text NOT NULL DEFAULT '',
    country text NOT NULL DEFAULT 'IT',
    sdi_code text NOT NULL DEFAULT '',
    pec text NOT NULL DEFAULT '',
    provider_customer_id text,
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    updated_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now'))
);
CREATE UNIQUE INDEX editor.customers_external_ref ON customers (coalesce(reseller_id, 0), external_ref)
    WHERE external_ref IS NOT NULL;
ALTER TABLE editor.tenants ADD COLUMN customer_id integer REFERENCES customers ON DELETE SET NULL;
ALTER TABLE editor.tenants ADD COLUMN hold text CHECK (hold IN ('billing'));
ALTER TABLE editor.operator_tokens ADD COLUMN reseller_id integer REFERENCES resellers ON DELETE CASCADE;
CREATE TABLE editor.plans (
    code text PRIMARY KEY,
    name text NOT NULL,
    description text NOT NULL DEFAULT '',
    price_month_cents integer,
    price_year_cents integer,
    currency text NOT NULL DEFAULT 'EUR',
    trial_days integer NOT NULL DEFAULT 0,
    max_sites integer,
    max_languages integer,
    max_active_sources integer,
    max_items_per_day integer,
    max_drafts_month integer,
    max_llm_cost_month real,
    public boolean NOT NULL DEFAULT 1,
    sort integer NOT NULL DEFAULT 0
);
INSERT INTO editor.plans (code, name, description, price_month_cents, price_year_cents, trial_days, max_sites,
                          max_languages, max_active_sources, max_items_per_day, max_drafts_month, public, sort)
VALUES
    ('trial', 'Prova', '7 giorni per provare la redazione su un sito', 0, 0, 7, 1, 1, 20, 300, 5, 0, 0),
    ('starter', 'Starter', 'Un sito, una lingua', 4900, 49000, 0, 1, 1, 30, 500, 30, 1, 1),
    ('pro', 'Pro', 'Tre siti, due lingue per sito', 14900, 149000, 0, 3, 2, 100, 2000, 120, 1, 2),
    ('business', 'Business', 'Dieci siti, cinque lingue per sito', 39900, 399000, 0, 10, 5, 300, 6000, 400, 1, 3);
CREATE TABLE editor.subscriptions (
    id integer PRIMARY KEY,
    tenant_id text NOT NULL UNIQUE REFERENCES tenants ON DELETE CASCADE,
    customer_id integer REFERENCES customers ON DELETE SET NULL,
    plan_code text NOT NULL REFERENCES plans ON UPDATE CASCADE,
    billing_interval text NOT NULL DEFAULT 'month' CHECK (billing_interval IN ('month', 'year')),
    status text NOT NULL CHECK (status IN ('trialing', 'active', 'past_due', 'canceled', 'expired')),
    provider text NOT NULL DEFAULT 'manual' CHECK (provider IN ('manual', 'mollie', 'reseller')),
    provider_subscription_id text,
    trial_ends_at text,
    current_period_end text,
    reminded_at text,
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    updated_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now'))
);
CREATE TABLE editor.payments (
    id integer PRIMARY KEY,
    tenant_id text REFERENCES tenants ON DELETE SET NULL,
    subscription_id integer REFERENCES subscriptions ON DELETE SET NULL,
    provider text NOT NULL,
    provider_payment_id text UNIQUE,
    kind text NOT NULL CHECK (kind IN ('first', 'recurring', 'manual')),
    amount_cents integer NOT NULL,
    currency text NOT NULL DEFAULT 'EUR',
    status text NOT NULL,
    note text NOT NULL DEFAULT '',
    created_at text NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f000', 'now')),
    paid_at text
);
