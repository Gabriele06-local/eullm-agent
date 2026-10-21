-- Alembic revision 0016: the plan chosen on the public site during the trial.
ALTER TABLE editor.subscriptions ADD COLUMN desired_plan text;
ALTER TABLE editor.subscriptions ADD COLUMN desired_interval text;
