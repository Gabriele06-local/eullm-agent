-- Alembic revision 0030: after the trial, proposals only; plans with or
-- without the automatic newsroom; Solo and Agency in the catalogue.
ALTER TABLE editor.plans ADD COLUMN autopilot boolean NOT NULL DEFAULT 1;
ALTER TABLE editor.tenants ADD COLUMN autopilot boolean NOT NULL DEFAULT 1;
ALTER TABLE editor.tenants ADD COLUMN proposals_only text;
-- Solo goes before every paid plan, after the trial.
UPDATE editor.plans SET sort = sort + 1 WHERE code <> 'trial';
INSERT OR IGNORE INTO editor.plans (code, name, description, price_month_cents, price_year_cents, trial_days,
                                    max_sites, max_languages, max_active_sources, max_items_per_day,
                                    max_drafts_month, public, sort, autopilot, currency)
VALUES
    ('solo', 'Solo', 'Un sito, una lingua, senza redazione automatica', 1990, 19900, 0, 1, 1, 15, 300, 10, 1,
     (SELECT COALESCE(MIN(sort), 1) FROM editor.plans WHERE code <> 'trial') - 1, 0, 'EUR'),
    ('agency', 'Agency', 'Trenta siti, cinque lingue per sito, per agenzie e rivenditori', 99900, 999000, 0, 30, 5,
     1000, 20000, 1500, 1, (SELECT COALESCE(MAX(sort), 0) FROM editor.plans) + 1, 1, 'EUR');
