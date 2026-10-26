-- Alembic revision 0028: six proposals a day instead of three, for the
-- sites saved with the old default; a number someone chose stays.
UPDATE editor.editorial_profiles
    SET body = json_set(body, '$.settings.proposals_per_day', 6)
    WHERE status IN ('approved', 'draft') AND json_extract(body, '$.settings.proposals_per_day') = 3;
