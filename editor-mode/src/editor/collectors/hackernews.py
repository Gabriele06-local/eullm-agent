from __future__ import annotations

from datetime import UTC, datetime

import httpx

from ..normalize import clean_text
from .base import FetchResult, Item, SourceSpec

DEFAULT_URL = "https://hn.algolia.com/api/v1/search"


class HackerNewsCollector:
    """Hacker News stories through the Algolia API.

    config: query (text), tags (default "story"), min_points, hits (default 50).
    The source url is the API endpoint (search or search_by_date).
    """

    def fetch(self, client: httpx.Client, source: SourceSpec) -> FetchResult:
        cfg = source.config
        params = {"tags": cfg.get("tags", "story"), "hitsPerPage": cfg.get("hits", 50)}
        if cfg.get("query"):
            params["query"] = cfg["query"]
        if cfg.get("min_points"):
            params["numericFilters"] = f"points>={int(cfg['min_points'])}"
        r = client.get(source.url or DEFAULT_URL, params=params)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError(f"unexpected Hacker News response: {str(data)[:200]}")
        items = []
        for hit in data.get("hits") or []:
            if not isinstance(hit, dict):
                continue
            oid = hit.get("objectID")
            title = clean_text(hit.get("title") or hit.get("story_title"))
            if not oid or not title:
                continue
            discussion = f"https://news.ycombinator.com/item?id={oid}"
            ts = hit.get("created_at_i")
            items.append(
                Item(
                    url=hit.get("url") or discussion,
                    title=title,
                    summary=clean_text(hit.get("story_text") or hit.get("comment_text"))[:2000],
                    external_id=f"hn:{oid}",
                    author=hit.get("author"),
                    published_at=datetime.fromtimestamp(ts, tz=UTC) if ts else None,
                    metrics={
                        "points": hit.get("points") or 0,
                        "comments": hit.get("num_comments") or 0,
                        "discussion_url": discussion,
                    },
                )
            )
        return FetchResult(items)
