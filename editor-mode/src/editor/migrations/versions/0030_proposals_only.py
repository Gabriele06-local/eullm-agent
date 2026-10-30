"""After the trial: proposals only. A newsroom whose trial (or paid period)
ends keeps receiving a couple of proposals a day for a while, with no
drafts and no publishing, before it stops. Plans say whether they include
the automatic newsroom; two plans join the catalogue (Solo and Agency).

Revision ID: 0030
Revises: 0029
"""

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE editor.plans ADD COLUMN autopilot boolean NOT NULL DEFAULT true;
        ALTER TABLE editor.tenants ADD COLUMN autopilot boolean NOT NULL DEFAULT true;
        ALTER TABLE editor.tenants ADD COLUMN proposals_only timestamptz;
        -- Solo goes before every paid plan, after the trial.
        UPDATE editor.plans SET sort = sort + 1 WHERE code <> 'trial';
        INSERT INTO editor.plans (code, name, description, price_month_cents, price_year_cents, trial_days, max_sites,
                                  max_languages, max_active_sources, max_items_per_day, max_drafts_month, public, sort,
                                  autopilot, currency)
        VALUES
            ('solo', 'Solo', 'Un sito, una lingua, senza redazione automatica', 1990, 19900, 0, 1, 1, 15, 300, 10,
             true, (SELECT COALESCE(MIN(sort), 1) FROM editor.plans WHERE code <> 'trial') - 1, false, 'EUR'),
            ('agency', 'Agency', 'Trenta siti, cinque lingue per sito, per agenzie e rivenditori', 99900, 999000, 0,
             30, 5, 1000, 20000, 1500, true, (SELECT COALESCE(MAX(sort), 0) FROM editor.plans) + 1, true, 'EUR')
        ON CONFLICT (code) DO NOTHING;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE editor.subscriptions SET plan_code = 'starter' WHERE plan_code IN ('solo', 'agency');
        UPDATE editor.subscriptions SET desired_plan = NULL WHERE desired_plan IN ('solo', 'agency');
        DELETE FROM editor.plans WHERE code IN ('solo', 'agency');
        ALTER TABLE editor.tenants DROP COLUMN proposals_only;
        ALTER TABLE editor.tenants DROP COLUMN autopilot;
        ALTER TABLE editor.plans DROP COLUMN autopilot;
        """
    )
