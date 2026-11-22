from __future__ import annotations

import re

import feedparser
import httpx

from ..normalize import clean_text
from .base import FetchResult, Item, SourceSpec, from_struct_time

MAX_SUMMARY = 2000
# Reddit's feeds put only this in a link post's summary: nothing to read, the title tells it.
REDDIT_NOISE = re.compile(r"^submitted by /u/\S+ to r/\S+\s*(\[link\])?\s*(\[comments\])?$")


def parse_feed(content: bytes) -> list[Item]:
    feed = feedparser.parse(content)
    items = []
    for e in feed.entries:
        link = e.get("link")
        title = clean_text(e.get("title"))
        if not link or not title:
            continue
        items.append(
            Item(
                url=link,
                title=title,
                summary=(REDDIT_NOISE.sub("", clean_text(e.get("summary")))
                         or clean_text(" ".join(str(c.get("value") or "") for c in (e.get("content") or []))))[:MAX_SUMMARY],
                external_id=e.get("id") or link,
                author=e.get("author"),
                published_at=from_struct_time(e.get("published_parsed") or e.get("updated_parsed")),
            )
        )
    return items


class RssCollector:
    """RSS and Atom feeds, with conditional GET."""

    def fetch(self, client: httpx.Client, source: SourceSpec) -> FetchResult:
        headers = {}
        if source.etag:
            headers["If-None-Match"] = source.etag
        if source.last_modified:
            headers["If-Modified-Since"] = source.last_modified
        r = client.get(source.url, headers=headers)
        if r.status_code == 304:
            return FetchResult([], not_modified=True, etag=source.etag, last_modified=source.last_modified)
        r.raise_for_status()
        return FetchResult(
            parse_feed(r.content),
            etag=r.headers.get("etag"),
            last_modified=r.headers.get("last-modified"),
        )
