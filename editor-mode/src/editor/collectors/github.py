from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from .base import FetchResult, Item, SourceSpec, from_iso

DEFAULT_URL = "https://api.github.com/search/repositories"


class GitHubCollector:
    """Repositories from the GitHub search API.

    config: query (GitHub search syntax; "{since}" becomes today minus
    since_days), since_days (default 7), sort (default "stars"), per_page.
    A token in the client headers raises the rate limit.
    """

    def fetch(self, client: httpx.Client, source: SourceSpec) -> FetchResult:
        cfg = source.config
        since = (datetime.now(UTC) - timedelta(days=int(cfg.get("since_days", 7)))).date()
        query = cfg.get("query", "created:>{since}").replace("{since}", since.isoformat())
        if cfg.get("min_stars") and "stars:" not in query:
            query += f" stars:>={int(cfg['min_stars'])}"
        params = {
            "q": query,
            "sort": cfg.get("sort", "stars"),
            "order": "desc",
            "per_page": cfg.get("per_page", 30),
        }
        r = client.get(
            source.url or DEFAULT_URL,
            params=params,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError(f"unexpected GitHub response: {str(data)[:200]}")
        items = []
        for repo in data.get("items") or []:
            if not isinstance(repo, dict):
                continue
            items.append(
                Item(
                    url=repo["html_url"],
                    # The name alone ("someone/eullm") says nothing of what it is.
                    title=f"{repo['full_name']}: {repo['description']}"[:160] if repo.get("description") else repo["full_name"],
                    summary=repo.get("description") or "",
                    external_id=f"gh:{repo['id']}",
                    author=(repo.get("owner") or {}).get("login"),
                    published_at=from_iso(repo.get("created_at")),
                    metrics={
                        "stars": repo.get("stargazers_count", 0),
                        "forks": repo.get("forks_count", 0),
                        "language": repo.get("language"),
                        "topics": repo.get("topics", []),
                    },
                )
            )
        return FetchResult(items)
