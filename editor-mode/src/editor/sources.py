"""Dynamic source registry: discover sources from what the site itself cites
and from its subtopics, rate them, and keep the registry current.

Nothing here comes from a fixed list. Candidates are (a) publications the
site links to in its own articles, (b) searches on Hacker News, GitHub,
Hugging Face and arXiv built from the approved (or draft) profile, and (c)
domains the model suggests. Every candidate is fetched and rated on what it
actually publishes before it is used; a suggestion that has no readable feed
is discarded.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import feedparser
import httpx
from sqlalchemy import select, update

from . import models as m
from . import quotas, repo, usage
from .collectors import COLLECTORS, Item, SourceSpec
from .core_client import CoreClient, CoreError
from .core_fetch import CoreTrouble
from .dedup import minhash, similarity
from .html_extract import parse_html, same_site
from .i18n import N_
from .progress import step
from .site import FEED_PATHS, NOT_SOURCES, RobotsCache
from .text import tokens

log = logging.getLogger(__name__)

FORMULA_VERSION = "source-v1"
WEIGHTS = {"relevance": 0.35, "frequency": 0.15, "freshness": 0.15, "originality": 0.10, "quality": 0.15, "reliability": 0.10}
ACTIVE, CANDIDATE = 0.5, 0.35
MIN_RELEVANCE = 0.1

# Search terms that point at arXiv categories and at Hugging Face.
ARXIV_CATEGORIES = {
    "cs.CL": {"llm", "language", "nlp", "translation", "chatbot", "transformer"},
    "cs.LG": {"learning", "training", "neural", "model", "models", "quantization", "distillation", "finetuning"},
    "cs.IR": {"retrieval", "search", "rag", "ranking", "recommendation", "embedding", "embeddings"},
    "cs.CV": {"vision", "image", "video", "ocr", "detection"},
    "cs.NI": {"network", "networking", "wifi", "5g", "6g", "routing", "latency", "wireless", "satellite", "fiber"},
    "cs.CR": {"security", "cybersecurity", "malware", "ransomware", "encryption", "privacy", "vulnerability"},
    "cs.DC": {"cloud", "distributed", "datacenter", "kubernetes", "gpu", "hpc", "serverless"},
    "cs.AR": {"hardware", "chip", "cpu", "arm", "risc", "accelerator", "npu"},
}
HF_TERMS = {"llm", "model", "models", "ai", "embedding", "embeddings", "diffusion", "transformer", "inference", "rag"}


@dataclass
class Candidate:
    kind: str
    name: str
    url: str
    config: dict = field(default_factory=dict)
    origin: str = "autodiscovery"
    evidence: dict = field(default_factory=dict)


# --- rating -------------------------------------------------------------------


def _sat(x: float, scale: float) -> float:
    return 1 - math.exp(-max(x, 0) / scale)


def profile_keywords(body: dict) -> set[str]:
    kws = set()
    for s in body.get("subtopics", []):
        for k in s.get("keywords", []) + [s.get("name", "")]:
            kws.update(tokens(k))
    for k in body.get("search_terms", []):
        kws.update(tokens(k))
    return kws


def relevance(items: list[Item], keywords: set[str]) -> float:
    if not items or not keywords:
        return 0.0
    hits = sum(1 for i in items if set(tokens(f"{i.title} {i.summary[:300]}")) & keywords)
    return hits / len(items)


def rate(
    items: list[Item],
    keywords: set[str],
    *,
    known: list[list[int]] = (),
    cited_by_site: int = 0,
    error_streak: int = 0,
    valid_feed: bool = True,
    https: bool = True,
    now: datetime | None = None,
) -> dict:
    """Score of a source from a sample of what it publishes."""
    now = now or datetime.now(UTC)
    comp = {}
    comp["relevance"] = round(relevance(items, keywords), 3)
    dates = [i.published_at for i in items if i.published_at]
    month = [d for d in dates if d >= now - timedelta(days=30)]
    comp["frequency"] = round(_sat(len(month) / 4.3, 5), 3) if dates else 0.0
    comp["freshness"] = round(min(1.0, math.exp(-(now - max(dates)).days / 14)), 3) if dates else 0.0
    if items and known:
        dup = sum(1 for i in items if any(similarity(minhash(f"{i.title} {i.summary}"), k) >= 0.8 for k in known))
        comp["originality"] = round(1 - dup / len(items), 3)
    else:
        comp["originality"] = 1.0 if items else 0.0
    if items:
        avg_summary = sum(len(i.summary) for i in items) / len(items)
        authored = sum(1 for i in items if i.author) / len(items)
        popular = [i.metrics.get("points") or i.metrics.get("stars") or i.metrics.get("likes") for i in items]
        popular = [p for p in popular if isinstance(p, (int, float))]
        q = 0.5 * min(avg_summary / 300, 1) + 0.2 * authored + 0.1 * https + 0.2 * valid_feed
        if popular:  # API sources: popularity stands in for editing
            q = max(q, _sat(sorted(popular)[len(popular) // 2], 100))
        comp["quality"] = round(q, 3)
    else:
        comp["quality"] = 0.0
    comp["reliability"] = round(max(0.0, min(1.0, 0.5 + 0.1 * cited_by_site) - 0.1 * error_streak), 3)
    score = sum(WEIGHTS[k] * v for k, v in comp.items())
    if comp["relevance"] < MIN_RELEVANCE:
        status = "rejected"
    elif score >= ACTIVE:
        status = "active"
    elif score >= CANDIDATE:
        status = "candidate"
    else:
        status = "rejected"
    return {"version": FORMULA_VERSION, "components": comp, "score": round(score, 3), "status": status,
            "sample": len(items)}


# --- discovery ----------------------------------------------------------------


class Discoverer:
    def __init__(self, client: httpx.Client, max_domains: int = 20):
        self.client = client
        self.max_domains = max_domains
        self._robots = RobotsCache(client)

    def _allowed(self, url: str) -> bool:
        return self._robots.allowed(url)

    def _get(self, url: str, accept: str) -> httpx.Response | None:
        if not self._allowed(url):
            return None
        try:
            r = self.client.get(url, headers={"Accept": accept})
        except httpx.HTTPError:
            return None
        return r if r.status_code == 200 else None

    def find_feed(self, domain: str) -> tuple[str, str] | None:
        """(feed url, publication name) of a domain, or None."""
        home = self._get(f"https://{domain}/", "text/html")
        name = domain
        feeds = []
        if home is not None:
            page = parse_html(home.text, f"https://{domain}/")
            name = page.site_name or page.title or domain
            # A feed on another site (a partner's, a network's) is not this
            # publication's: only the domain's own feeds are taken.
            feeds = [f for f in page.feeds if same_site(f, domain)]
        for url in feeds[:2] + [f"https://{domain}{p}" for p in FEED_PATHS]:
            r = self._get(url, "application/rss+xml, application/atom+xml, application/xml")
            if r is not None and feedparser.parse(r.content).entries:
                return r.headers.get("x-final-url") or url, name[:80]
        return None

    def from_outbound(self, outbound: dict[str, int], own_domain: str) -> list[Candidate]:
        out = []
        for domain, count in sorted(outbound.items(), key=lambda kv: -kv[1])[: self.max_domains]:
            if domain == own_domain or any(domain == d or domain.endswith("." + d) for d in NOT_SOURCES):
                continue
            found = self.find_feed(domain)
            if found:
                out.append(Candidate("rss", found[1], found[0], origin="site_outbound",
                                     evidence={"cited_by_site": count, "domain": domain}))
        return out

    def from_suggestions(self, domains: list[str], own_domain: str) -> list[Candidate]:
        out = []
        for domain in domains[: self.max_domains]:
            domain = domain.lower().removeprefix("https://").removeprefix("http://").split("/")[0].removeprefix("www.")
            if not domain or domain == own_domain or "." not in domain:
                continue
            found = self.find_feed(domain)
            if found:
                out.append(Candidate("rss", found[1], found[0], origin="model_suggestion",
                                     evidence={"suggested_by": "model", "domain": domain, "verified": "feed read"}))
        return out


# A search follows what is new: the plain search returns the most popular
# stories of all time, so a source built on it looked silent for months.
HN_SEARCH = "https://hn.algolia.com/api/v1/search"
HN_RECENT = HN_SEARCH + "_by_date"


# A new repository with a few stars is news; one with none is often just a
# namesake (searching "eullm" found someone else's empty "eullm").
GITHUB_MIN_STARS = 10
GITHUB_TRENDING = {"kind": "github", "name": "GitHub Trending", "url": "https://github.com/trending",
                   "config": {"trending": True, "since": "daily"}}
# Words of a profile for which the repositories developers star today are news.
CODE_TERMS = HF_TERMS | {"software", "code", "coding", "developer", "developers", "github", "open", "source",
                         "programming", "cloud", "devops", "security", "agent", "agents", "ml", "data",
                         "kubernetes", "python", "rust", "javascript", "framework", "api", "tech", "on-premise"}


def _recent_searches(s, now: datetime) -> None:
    """Searches found before HN_RECENT existed move to it, and those suspended
    as silent get another chance: their silence came from the wrong search."""
    src = m.sources
    for r in s.execute(select(src).where(src.c.kind == "hackernews", src.c.origin == "api_query",
                                         src.c.url == HN_SEARCH)).all():
        twin = s.execute(select(src.c.id).where(src.c.site_id == r.site_id, src.c.kind == r.kind,
                                                src.c.url == HN_RECENT, src.c.config == r.config)).first()
        if twin is not None:
            continue
        values = {"url": HN_RECENT, "last_item_at": None}
        if r.status == "suspended" and not r.status_set_by and (r.status_reason or "").startswith("nothing new"):
            values |= {"status": "active", "status_reason": None, "status_changed_at": now}
        s.execute(update(src).where(src.c.id == r.id).values(**values))
    for r in s.execute(select(src).where(src.c.kind == "github", src.c.origin == "api_query")).all():
        cfg = dict(r.config or {})
        if "min_stars" in cfg:
            continue
        cfg["min_stars"] = GITHUB_MIN_STARS
        twin = s.execute(select(src.c.id).where(src.c.site_id == r.site_id, src.c.kind == r.kind,
                                                src.c.url == r.url, src.c.config == cfg)).first()
        if twin is None:
            s.execute(update(src).where(src.c.id == r.id).values(config=cfg))
    # A word search on GitHub finds repositories nobody uses; the Trending
    # page says what developers star today. The newsroom's automatic
    # searches give way to it, site by site.
    searches = s.execute(select(src).where(
        src.c.kind == "github", src.c.origin == "api_query", src.c.shared.is_not(True),
        src.c.status.in_(("active", "candidate")), src.c.status_set_by.is_(None))).all()
    searches = [r for r in searches if not (r.config or {}).get("trending")]
    for site_id in {r.site_id for r in searches}:
        repo.upsert_source(s, s.info["tenant_id"], **GITHUB_TRENDING, site_id=site_id,
                           origin="api_query", evidence={"replaces": "GitHub searches"})
    for r in searches:
        repo.set_source_status(s, r.id, "suspended", "replaced by GitHub Trending")


def api_candidates(body: dict) -> list[Candidate]:
    """Searches on public APIs built from the profile's subtopics."""
    subs = [s for s in body.get("subtopics", []) if s.get("share", 0) >= 0.05][:6] or body.get("subtopics", [])[:3]
    own = [" ".join(s["keywords"][:2]) for s in subs if s.get("keywords")]
    # The model's search terms first, then one per topic of the profile.
    queries = list(dict.fromkeys((body.get("search_terms") or []) + own))
    out = []
    for q in queries[:8]:
        out.append(Candidate("hackernews", f"Hacker News: {q}", HN_RECENT,
                             {"query": q, "min_points": 20}, "api_query", {"query": q}))
    words = {t for q in queries for t in tokens(q)} | profile_keywords(body)
    if words & CODE_TERMS:
        out.append(Candidate("github", GITHUB_TRENDING["name"], GITHUB_TRENDING["url"], GITHUB_TRENDING["config"],
                             "api_query", {"matched": sorted(words & CODE_TERMS)}))
    cats = [c for c, terms in ARXIV_CATEGORIES.items() if words & terms]
    if cats:
        query = " OR ".join(f"cat:{c}" for c in cats[:3])
        out.append(Candidate("arxiv", f"arXiv: {', '.join(cats[:3])}", "https://export.arxiv.org/api/query",
                             {"query": query}, "api_query", {"categories": cats[:3]}))
    if words & HF_TERMS:
        out.append(Candidate("huggingface", "Hugging Face: trending models", "https://huggingface.co/api/models",
                             {"sort": "trendingScore"}, "api_query", {"matched": sorted(words & HF_TERMS)}))
    return out


SUGGEST_SYSTEM = (
    "You help an editor find sources. Given the editorial profile of a site, reply with JSON only: "
    '{"search_terms": ["<up to 6 short English search queries for the site\'s main subtopics>"], '
    '"domains": ["<up to 15 domains of established publications, institutions or projects that regularly '
    'publish on these subtopics, including publications in the site\'s languages>"]}. Domains are checked by '
    'reading their feeds; do not include the site itself.'
)


def _validate_suggestions(v) -> dict:
    if not isinstance(v, dict):
        raise ValueError("expected an object")
    terms = [t.strip() for t in v.get("search_terms", []) if isinstance(t, str) and 2 <= len(t.strip()) <= 60]
    domains = [d.strip() for d in v.get("domains", []) if isinstance(d, str) and "." in d and len(d) <= 100]
    return {"search_terms": terms[:6], "domains": domains[:15]}


# --- registry ------------------------------------------------------------------


def _profile_for(s, site_id: int):
    """The approved profile: a draft is only a proposal and never steers sources."""
    p = m.editorial_profiles
    return s.execute(
        select(p).where(p.c.site_id == site_id, p.c.status == "approved").order_by(p.c.version.desc())
    ).first()


def _known_signatures(s, limit: int = 2000) -> list[list[int]]:
    return [list(r) for r in s.execute(
        select(m.source_items.c.minhash).order_by(m.source_items.c.id.desc()).limit(limit)
    ).scalars()]


@dataclass
class DiscoveryReport:
    candidates: int = 0
    active: int = 0
    candidate: int = 0
    rejected: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)


def discover(db, tenant_id: str, site_id: int, client: httpx.Client, core: CoreClient | None = None) -> DiscoveryReport:
    report = DiscoveryReport()
    with db.tenant(tenant_id) as s:
        prof = _profile_for(s, site_id)
        if prof is None:
            report.notes.append("no approved profile for this site: approve one first")
            return report
        body = dict(prof.body)
        domain = body.get("domain", "")
        snap = s.execute(
            select(m.site_analyses.c.snapshot).where(m.site_analyses.c.site_id == site_id)
            .order_by(m.site_analyses.c.id.desc()).limit(1)
        ).scalar() or {}
        known = _known_signatures(s)
        existing = {(r.kind, r.url, str(r.config)) for r in s.execute(
            select(m.sources.c.kind, m.sources.c.url, m.sources.c.config).where(m.sources.c.site_id == site_id))}

    suggestions = {"search_terms": [], "domains": []}
    if core is not None and body.get("subtopics"):
        step(N_("Il modello suggerisce dove cercare le fonti per {domain}"), domain=domain)
        try:
            prompt = "Profile:\n" + "\n".join(
                f"- {x['name']}: {', '.join(x.get('keywords', []))}" for x in body["subtopics"]
            ) + (f"\nSector: {body.get('sector', {}).get('value')}\nLanguage: {body.get('language')}"
                 f"\nLanguages of the site: {', '.join(body.get('languages') or {}) or body.get('language')}"
                 f"\nSummary: {body.get('summary') or '-'}\nSite: {domain}")
            with usage.tagged(db, tenant_id, "source_discovery", site_id=site_id):
                suggestions, calls = core.chat_json(SUGGEST_SYSTEM, prompt, _validate_suggestions)
        except CoreError as e:
            report.notes.append(f"model suggestions unavailable: {e}")
    body["search_terms"] = suggestions["search_terms"]

    finder = Discoverer(client)
    candidates = finder.from_outbound(snap.get("outbound_domains", {}), domain)
    candidates += api_candidates(body)
    candidates += finder.from_suggestions(suggestions["domains"], domain)
    keywords = profile_keywords(body)
    seen = set()
    total = len({(c.kind, c.url, str(c.config)) for c in candidates} - existing)
    for cand in candidates:
        key = (cand.kind, cand.url, str(cand.config))
        if key in seen or key in existing:
            continue
        seen.add(key)
        report.candidates += 1
        step(N_("Prova delle fonti candidate: {n} di {total} ({name})"), n=report.candidates, total=total, name=cand.name)
        try:
            result = COLLECTORS[cand.kind].fetch(client, SourceSpec(cand.kind, cand.url, cand.config))
        except (httpx.HTTPError, ValueError, KeyError) as e:
            report.failed += 1
            report.notes.append(f"{cand.name}: {type(e).__name__}")
            continue
        ev = rate(result.items, keywords, known=known, cited_by_site=cand.evidence.get("cited_by_site", 0),
                  https=cand.url.startswith("https://") or not cand.url)
        # What it was judged on, shown next to the source.
        ev["examples"] = [{"title": i.title[:200], "url": i.url} for i in result.items[:3] if i.title]
        known += [minhash(f"{i.title} {i.summary}") for i in result.items[:20]]
        with db.tenant(tenant_id) as s:
            if ev["status"] == "active" and quotas.remaining(s, "sources") == 0:
                ev["status"], ev["held_by_quota"] = "candidate", True
                report.notes.append(f"{cand.name}: kept as candidate, active sources limit reached")
        setattr(report, ev["status"], getattr(report, ev["status"]) + 1)
        with db.tenant(tenant_id) as s:
            sid = repo.upsert_source(s, tenant_id, kind=cand.kind, name=cand.name, url=cand.url, config=cand.config,
                                     site_id=site_id, status=ev["status"], origin=cand.origin, evidence=cand.evidence)
            s.execute(update(m.sources).where(m.sources.c.id == sid).values(
                evaluation=ev, score=ev["score"], evaluated_at=datetime.now(UTC)))
    return report


@dataclass
class MaintenanceReport:
    suspended: list[tuple[int, str]] = field(default_factory=list)
    reactivated: list[int] = field(default_factory=list)
    reevaluated: int = 0


def maintain(db, tenant_id: str, client: httpx.Client | None = None, now: datetime | None = None,
             error_limit: int = 5, stale_days: int = 45, retry_days: int = 14, archive: bool = True) -> MaintenanceReport:
    """Suspend the newsroom's sources that drift off topic and give its
    suspended and candidate sources another look after a while; with
    `archive`, maintain the archive sources it reads too (the scheduler
    does that once for everyone)."""
    now = now or datetime.now(UTC)
    rep = maintain_archive(db, repo.archive_of(db, tenant_id), client, now, error_limit, stale_days, retry_days) \
        if archive else MaintenanceReport()
    with db.tenant(tenant_id) as s:
        _recent_searches(s, now)
        rows = s.execute(select(m.sources).where(repo.own())).all()
        profiles = {}
        for r in rows:
            if r.site_id and r.site_id not in profiles:
                p = _profile_for(s, r.site_id)
                profiles[r.site_id] = profile_keywords(p.body) if p else set()
        for r in rows:
            if r.status != "active" or not r.site_id or not profiles.get(r.site_id) or r.status_set_by:
                continue
            recent = _stored(s, r, 50)
            if len(recent) >= 20:
                rel = relevance(recent, profiles[r.site_id])
                if rel < MIN_RELEVANCE:
                    reason = f"off topic (relevance {rel:.2f} on the last {len(recent)} items)"
                    repo.set_source_status(s, r.id, "suspended", reason)
                    rep.suspended.append((r.id, reason))
        # A status a person decided stays as it is.
        due = [r for r in rows if r.status in ("suspended", "candidate") and not r.status_set_by
               and (r.evaluated_at or r.status_changed_at) < now - timedelta(days=retry_days)]
        # What the archive already read stands in for a fetch.
        stored = {r.id: _stored(s, r, 50, since=now - timedelta(days=30)) for r in due}
    for r in due:
        items = stored[r.id]
        if len(items) < 10:
            if client is None:
                continue
            try:
                items = COLLECTORS[r.kind].fetch(client, SourceSpec(r.kind, r.url, r.config or {})).items
            except CoreTrouble:
                continue  # the Core's trouble, not the source's: try again next time
            except (httpx.HTTPError, ValueError, KeyError):
                with db.tenant(tenant_id) as s:
                    s.execute(update(m.sources).where(m.sources.c.id == r.id).values(evaluated_at=now))
                continue
        ev = rate(items, profiles.get(r.site_id, set()), cited_by_site=(r.evidence or {}).get("cited_by_site", 0), now=now)
        rep.reevaluated += 1
        with db.tenant(tenant_id) as s:
            s.execute(update(m.sources).where(m.sources.c.id == r.id).values(
                evaluation=ev, score=ev["score"], evaluated_at=now, consecutive_errors=0))
            if ev["status"] == "active" and quotas.remaining(s, "sources") != 0:
                repo.set_source_status(s, r.id, "active", "re-evaluated")
                rep.reactivated.append(r.id)
            elif r.status == "candidate" and ev["status"] == "rejected":
                repo.set_source_status(s, r.id, "rejected", "re-evaluated")
    return rep


def _stored(s, r, limit: int, since: datetime | None = None) -> list[Item]:
    """The latest items read for a source: in the archive, or by itself before it."""
    si = m.source_items
    ids = [r.id] + ([r.archive_id] if r.archive_id else [])
    q = select(si.c.url, si.c.title, si.c.summary, si.c.author, si.c.published_at, si.c.metrics).where(
        si.c.source_id.in_(ids))
    if since is not None:
        q = q.where(si.c.fetched_at >= since)
    return [Item(url=x.url, title=x.title, summary=x.summary or "", author=x.author, published_at=x.published_at,
                 metrics=x.metrics or {}) for x in s.execute(q.order_by(si.c.id.desc()).limit(limit))]


def maintain_archive(db, archive: str, client: httpx.Client | None = None, now: datetime | None = None,
                     error_limit: int = 5, stale_days: int = 45, retry_days: int = 14) -> MaintenanceReport:
    """Suspend archive sources that fail or go silent, for every newsroom;
    read the suspended ones again after a while and bring back those that
    answer with news."""
    now = now or datetime.now(UTC)
    rep = MaintenanceReport()
    with db.tenant(archive) as s:
        _recent_searches(s, now)
        rows = s.execute(select(m.sources).where(m.sources.c.shared.is_(True))).all()
        for r in rows:
            if r.status != "active":
                continue
            reason = None
            if r.consecutive_errors >= error_limit:
                reason = f"{r.consecutive_errors} failed fetches in a row"
            elif r.last_item_at and r.last_item_at < now - timedelta(days=stale_days):
                reason = f"nothing new for more than {stale_days} days"
            if reason:
                repo.set_source_status(s, r.id, "suspended", reason)
                rep.suspended.append((r.id, reason))
        due = [r for r in rows if r.status == "suspended" and not r.status_set_by
               and (r.evaluated_at or r.status_changed_at) < now - timedelta(days=retry_days)]
    if client is None:
        return rep
    for r in due:
        try:
            result = COLLECTORS[r.kind].fetch(client, SourceSpec(r.kind, r.url, r.config or {}))
        except CoreTrouble:
            continue
        except (httpx.HTTPError, ValueError, KeyError):
            with db.tenant(archive) as s:
                s.execute(update(m.sources).where(m.sources.c.id == r.id).values(evaluated_at=now))
            continue
        dates = [i.published_at for i in result.items if i.published_at]
        fresh = not dates or max(dates) >= now - timedelta(days=stale_days)
        with db.tenant(archive) as s:
            s.execute(update(m.sources).where(m.sources.c.id == r.id).values(evaluated_at=now, consecutive_errors=0))
            if result.items and fresh:
                repo.set_source_status(s, r.id, "active", "re-evaluated")
                rep.reactivated.append(r.id)
        rep.reevaluated += 1
    return rep
