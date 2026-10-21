"""Customers, resellers, plans, subscriptions and payments.

A customer is the billing record of whoever pays (company or person, with
the data an Italian invoice needs); it arrives from the public site through
the operator API and owns one or more newsrooms. A reseller sells
newsrooms to its own customers with an operator key bound to it, and sees
only those. A plan is the catalogue entry the public site shows; a
subscription ties a newsroom to a plan, with its trial and paid period.

Only the owner connection reads and writes these tables, as for accounts:
row level security is enabled, not forced, and the application role has
no grant on them.

Revision ID: 0015
Revises: 0014
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE editor.resellers (
            id bigserial PRIMARY KEY,
            name text NOT NULL,
            email text NOT NULL,
            discount_pct real NOT NULL DEFAULT 0 CHECK (discount_pct BETWEEN 0 AND 100),
            notes text NOT NULL DEFAULT '',
            active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE editor.customers (
            id bigserial PRIMARY KEY,
            reseller_id bigint REFERENCES editor.resellers ON DELETE RESTRICT,
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
            country text NOT NULL DEFAULT 'IT' CHECK (country ~ '^[A-Z]{2}$'),
            sdi_code text NOT NULL DEFAULT '',
            pec text NOT NULL DEFAULT '',
            provider_customer_id text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        -- The public site's (or the reseller's) id of a customer: asking twice
        -- with the same one gives the same customer.
        CREATE UNIQUE INDEX customers_external_ref ON editor.customers (coalesce(reseller_id, 0), external_ref)
            WHERE external_ref IS NOT NULL;
        ALTER TABLE editor.tenants ADD COLUMN customer_id bigint REFERENCES editor.customers ON DELETE SET NULL;
        -- Why a newsroom is suspended: 'billing' when its trial or paid period
        -- ended (its owner can still sign in to pay), null otherwise.
        ALTER TABLE editor.tenants ADD COLUMN hold text CHECK (hold IN ('billing'));
        ALTER TABLE editor.operator_tokens ADD COLUMN reseller_id bigint REFERENCES editor.resellers ON DELETE CASCADE;

        CREATE TABLE editor.plans (
            code text PRIMARY KEY CHECK (code ~ '^[a-z0-9-]{1,40}$'),
            name text NOT NULL,
            description text NOT NULL DEFAULT '',
            price_month_cents integer CHECK (price_month_cents >= 0),
            price_year_cents integer CHECK (price_year_cents >= 0),
            currency text NOT NULL DEFAULT 'EUR',
            trial_days integer NOT NULL DEFAULT 0 CHECK (trial_days >= 0),
            max_sites integer,
            max_languages integer,
            max_active_sources integer,
            max_items_per_day integer,
            max_drafts_month integer,
            max_llm_cost_month double precision,
            public boolean NOT NULL DEFAULT true,
            sort integer NOT NULL DEFAULT 0
        );
        -- Provisional catalogue (2026-10-21), changed from the console.
        INSERT INTO editor.plans (code, name, description, price_month_cents, price_year_cents, trial_days, max_sites,
                                  max_languages, max_active_sources, max_items_per_day, max_drafts_month, public, sort)
        VALUES
            ('trial', 'Prova', '7 giorni per provare la redazione su un sito', 0, 0, 7, 1, 1, 20, 300, 5, false, 0),
            ('starter', 'Starter', 'Un sito, una lingua', 4900, 49000, 0, 1, 1, 30, 500, 30, true, 1),
            ('pro', 'Pro', 'Tre siti, due lingue per sito', 14900, 149000, 0, 3, 2, 100, 2000, 120, true, 2),
            ('business', 'Business', 'Dieci siti, cinque lingue per sito', 39900, 399000, 0, 10, 5, 300, 6000, 400, true, 3);

        CREATE TABLE editor.subscriptions (
            id bigserial PRIMARY KEY,
            tenant_id text NOT NULL UNIQUE REFERENCES editor.tenants ON DELETE CASCADE,
            customer_id bigint REFERENCES editor.customers ON DELETE SET NULL,
            plan_code text NOT NULL REFERENCES editor.plans ON UPDATE CASCADE,
            billing_interval text NOT NULL DEFAULT 'month' CHECK (billing_interval IN ('month', 'year')),
            status text NOT NULL CHECK (status IN ('trialing', 'active', 'past_due', 'canceled', 'expired')),
            provider text NOT NULL DEFAULT 'manual' CHECK (provider IN ('manual', 'mollie', 'reseller')),
            provider_subscription_id text,
            trial_ends_at timestamptz,
            current_period_end timestamptz,
            reminded_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE editor.payments (
            id bigserial PRIMARY KEY,
            tenant_id text REFERENCES editor.tenants ON DELETE SET NULL,
            subscription_id bigint REFERENCES editor.subscriptions ON DELETE SET NULL,
            provider text NOT NULL,
            provider_payment_id text UNIQUE,
            kind text NOT NULL CHECK (kind IN ('first', 'recurring', 'manual')),
            amount_cents integer NOT NULL,
            currency text NOT NULL DEFAULT 'EUR',
            status text NOT NULL,
            note text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            paid_at timestamptz
        );
        ALTER TABLE editor.customers ENABLE ROW LEVEL SECURITY;
        ALTER TABLE editor.resellers ENABLE ROW LEVEL SECURITY;
        ALTER TABLE editor.subscriptions ENABLE ROW LEVEL SECURITY;
        ALTER TABLE editor.payments ENABLE ROW LEVEL SECURITY;

        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE editor.payments;
        DROP TABLE editor.subscriptions;
        DROP TABLE editor.plans;
        ALTER TABLE editor.operator_tokens DROP COLUMN reseller_id;
        ALTER TABLE editor.tenants DROP COLUMN hold;
        ALTER TABLE editor.tenants DROP COLUMN customer_id;
        DROP TABLE editor.customers;
        DROP TABLE editor.resellers;
        """
    )
