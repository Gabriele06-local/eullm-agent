"""The free trial, once: hashes of the email, VAT number, tax code, phone
and sites a trial was given to, and the second trials that were refused.
Every site already in a newsroom is remembered from now on.

Revision ID: 0029
Revises: 0028
"""

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE editor.trial_marks (
            id bigserial PRIMARY KEY,
            kind text NOT NULL CHECK (kind IN ('email', 'vat', 'tax_code', 'phone', 'domain')),
            value_hash text NOT NULL,
            newsroom text NOT NULL,
            customer_id bigint,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX trial_marks_value ON editor.trial_marks (value_hash);
        CREATE TABLE editor.trial_refusals (
            id bigserial PRIMARY KEY,
            newsroom text NOT NULL,
            customer_id bigint,
            reasons jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX trial_refusals_created ON editor.trial_refusals (created_at DESC);
        ALTER TABLE editor.trial_marks ENABLE ROW LEVEL SECURITY;
        ALTER TABLE editor.trial_refusals ENABLE ROW LEVEL SECURITY;

        ALTER TABLE editor.sites NO FORCE ROW LEVEL SECURITY;
        INSERT INTO editor.trial_marks (kind, value_hash, newsroom)
            SELECT DISTINCT 'domain',
                   encode(sha256(convert_to('domain:' || regexp_replace(lower(domain), '^www\\.', ''), 'UTF8')), 'hex'),
                   tenant_id
            FROM editor.sites;
        ALTER TABLE editor.sites FORCE ROW LEVEL SECURITY;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE editor.trial_refusals; DROP TABLE editor.trial_marks;")
