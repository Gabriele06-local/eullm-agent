-- Alembic revision 0024: the languages a site publishes in besides its own, and drafts translating another draft.
ALTER TABLE editor.sites ADD COLUMN languages text;
ALTER TABLE editor.tenants ADD COLUMN max_languages integer;
ALTER TABLE editor.drafts ADD COLUMN translation_of integer REFERENCES drafts ON DELETE CASCADE;
CREATE INDEX editor.drafts_translation_of ON drafts (translation_of);
