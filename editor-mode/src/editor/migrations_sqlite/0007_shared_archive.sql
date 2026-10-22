-- Alembic revision 0017: one shared archive of public sources. The local
-- installation has a single newsroom, which is its own archive: archive
-- sources are its rows with shared = 1, the sources its sites follow point
-- at them with archive_id.
ALTER TABLE editor.sources ADD COLUMN shared integer NOT NULL DEFAULT 0;
ALTER TABLE editor.sources ADD COLUMN archive_id integer REFERENCES sources(id) ON DELETE SET NULL;
DROP INDEX editor.sources_unique;
CREATE UNIQUE INDEX editor.sources_unique ON sources (tenant_id, coalesce(site_id, 0), kind, url, config, shared);
CREATE INDEX editor.sources_archive ON sources (archive_id);
INSERT INTO editor.sources (tenant_id, kind, name, url, config, weight, status, origin, evaluation, score,
                            evaluated_at, last_item_at, shared)
SELECT tenant_id, kind, min(name), url, config, 1.0, 'active', min(origin), min(evaluation), max(score),
       max(evaluated_at), max(last_item_at), 1
FROM editor.sources WHERE shared = 0
GROUP BY tenant_id, kind, url, config;
UPDATE editor.sources SET archive_id = (
    SELECT a.id FROM editor.sources a
    WHERE a.shared = 1 AND a.tenant_id = sources.tenant_id AND a.kind = sources.kind AND a.url = sources.url
      AND a.config = sources.config)
WHERE shared = 0;
