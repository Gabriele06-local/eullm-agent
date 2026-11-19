"""A translation's cover points at its original's image instead of keeping
a copy of the bytes (2026-11-19: on the server the backups grew by 20 MB a
day, one copy of every cover per language). Copies already made become
pointers; deleting an image that translations point at hands them the
bytes first.

Revision ID: 0048
Revises: 0047
"""

from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE editor.draft_images ADD COLUMN same_as bigint;
        CREATE INDEX draft_images_same_as ON editor.draft_images (same_as) WHERE same_as IS NOT NULL;

        CREATE FUNCTION editor.draft_images_keep_copies() RETURNS trigger
            LANGUAGE plpgsql SET search_path = pg_catalog, editor
            AS $$
            BEGIN
                UPDATE editor.draft_images SET content = OLD.content, same_as = NULL
                 WHERE same_as = OLD.draft_id AND tenant_id = OLD.tenant_id;
                RETURN OLD;
            END $$;
        CREATE TRIGGER draft_images_keep_copies BEFORE DELETE ON editor.draft_images
            FOR EACH ROW EXECUTE FUNCTION editor.draft_images_keep_copies();

        -- The same bytes twice in a newsroom: the later rows point at the first.
        WITH g AS (
            SELECT id, tenant_id, md5(content) AS h,
                   first_value(id) OVER (PARTITION BY tenant_id, md5(content) ORDER BY id) AS first
              FROM editor.draft_images WHERE same_as IS NULL AND length(content) > 0
        )
        UPDATE editor.draft_images t SET same_as = r.draft_id, content = '\\x'::bytea
          FROM g JOIN editor.draft_images r ON r.id = g.first
         WHERE t.id = g.id AND g.id <> g.first AND t.content = r.content;

        CREATE OR REPLACE FUNCTION editor.image_by_token(t text) RETURNS TABLE (mime text, content bytea)
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, editor
            AS $$ SELECT i.mime, CASE WHEN i.same_as IS NOT NULL AND o.content IS NOT NULL THEN o.content
                                      ELSE i.content END
                    FROM editor.draft_images i
                    LEFT JOIN editor.draft_images o ON o.draft_id = i.same_as AND o.tenant_id = i.tenant_id
                   WHERE i.token = t $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE editor.draft_images t SET content = o.content, same_as = NULL
          FROM editor.draft_images o WHERE o.draft_id = t.same_as;
        DROP TRIGGER draft_images_keep_copies ON editor.draft_images;
        DROP FUNCTION editor.draft_images_keep_copies();
        CREATE OR REPLACE FUNCTION editor.image_by_token(t text) RETURNS TABLE (mime text, content bytea)
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, editor
            AS $$ SELECT mime, content FROM editor.draft_images WHERE token = t $$;
        ALTER TABLE editor.draft_images DROP COLUMN same_as;
        """
    )
