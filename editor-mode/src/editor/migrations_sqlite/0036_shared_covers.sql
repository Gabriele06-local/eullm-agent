-- Alembic revision 0048: a translation's cover points at its original's image (draft_images.same_as).
ALTER TABLE editor.draft_images ADD COLUMN same_as integer;
CREATE TRIGGER editor.draft_images_keep_copies BEFORE DELETE ON draft_images
BEGIN
    UPDATE draft_images SET content = OLD.content, same_as = NULL
     WHERE same_as = OLD.draft_id AND tenant_id = OLD.tenant_id;
END;
UPDATE editor.draft_images
   SET same_as = (SELECT o.draft_id FROM editor.draft_images o
                   WHERE o.tenant_id = draft_images.tenant_id AND o.id < draft_images.id
                     AND o.same_as IS NULL AND o.content = draft_images.content ORDER BY o.id LIMIT 1),
       content = X''
 WHERE same_as IS NULL AND length(content) > 0 AND EXISTS (
       SELECT 1 FROM editor.draft_images o WHERE o.tenant_id = draft_images.tenant_id AND o.id < draft_images.id
          AND o.same_as IS NULL AND o.content = draft_images.content);
