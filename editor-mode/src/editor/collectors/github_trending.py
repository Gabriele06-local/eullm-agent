from __future__ import annotations

import html
import re
from datetime import UTC, datetime

import httpx

from .base import FetchResult, Item, SourceSpec

DEFAULT_URL = "https://github.com/trending"

_ARTICLE = re.compile(r"<article\b[^>]*Box-row[^>]*>(.*?)</article>", re.S)
_REPO = re.compile(r"<h2\b.*?<a\b[^>]*href=\"/([\w.-]+/[\w.-]+)\"", re.S)
_DESC = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S)
_LANG = re.compile(r"itemprop=\"programmingLanguage\"[^>]*>(.*?)<", re.S)
_STARS = re.compile(r"href=\"/[\w.-]+/[\w.-]+/stargazers\"[^>]*>(.*?)</a>", re.S)
_TODAY = re.compile(r"([\d,.]+)\s+stars\s+(today|this week|this month)")
_TAG = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", fragment)).split())


def _num(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else 0


def parse(page: str, now: datetime | None = None) -> list[Item]:
    now = now or datetime.now(UTC)
    items = []
    for rank, block in enumerate(_ARTICLE.findall(page), start=1):
        name = _REPO.search(block)
        if not name:
            continue
        full = name.group(1)
        desc = _DESC.search(block)
        desc = _text(desc.group(1)) if desc else ""
        lang = _LANG.search(block)
        stars = _STARS.search(block)
        gained = _TODAY.search(_text(block))
        today = _num(gained.group(1)) if gained else 0
        total = _num(_text(stars.group(1))) if stars else 0
        period = gained.group(2) if gained else "today"
        trend = f"GitHub Trending #{rank}: +{today} stars {period}, {total} in total."
        items.append(Item(
            url=f"https://github.com/{full}",
            title=f"{full}: {desc}"[:160] if desc else full,
            summary=f"{desc} {trend}".strip(),
            external_id=f"ghtrend:{full}",
            author=full.split("/", 1)[0],
            # A repository in the trend is news today, whenever it was created.
            published_at=now,
            metrics={"stars": total, "stars_today": today, "rank": rank,
                     "language": _text(lang.group(1)) if lang else None},
        ))
    return items


class GitHubTrendingCollector:
    """The repositories on GitHub's Trending page: what developers are
    starring right now, not every repository that mentions a word. A
    "github" source with config trending: true.

    config: since ("daily", "weekly" or "monthly"; default "daily"),
    language (a GitHub language slug, optional), limit (default 25).
    """

    def fetch(self, client: httpx.Client, source: SourceSpec) -> FetchResult:
        cfg = source.config
        url = (source.url or DEFAULT_URL).rstrip("/")
        if cfg.get("language"):
            url += f"/{cfg['language']}"
        r = client.get(url, params={"since": cfg.get("since", "daily")}, headers={"Accept": "text/html"})
        r.raise_for_status()
        items = parse(r.text)
        if not items and "Box-row" not in r.text:
            raise ValueError("GitHub Trending page without repositories: its layout may have changed")
        return FetchResult(items[: int(cfg.get("limit", 25))])
