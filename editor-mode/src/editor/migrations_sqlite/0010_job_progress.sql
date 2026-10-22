-- Alembic revision 0020: the step a running job is on and what it produced.
ALTER TABLE editor.jobs ADD COLUMN progress text;
ALTER TABLE editor.jobs ADD COLUMN result text;
