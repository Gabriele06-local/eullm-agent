"""Every model call, recorded when it happens: which newsroom, site and
article it was for, which step, which model answered, the tokens in and
out, how long it took and, for a call that failed, why.

Code that calls the model says what the call is for with `tagged`; the
Core client records each call it makes inside it (core_client.chat), so a
job that fails half-way still leaves the calls it paid for. Outside a
`tagged` block nothing is recorded (tests, the command line).

Costs: the Core gives the cost at the prices of core.yaml (0 for a free
model). The console also keeps a reference price per model, so the cost of
an article can be estimated whatever the model costs today (`estimate`).
"""

from __future__ import annotations

import csv
import io
import logging
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, insert, select

from . import models as m
from . import progress
from .i18n import N_

log = logging.getLogger(__name__)

_tag: ContextVar = ContextVar("editor_usage", default=None)

# The steps, as the console names them.
PURPOSES = {
    "site_profile": N_("Analisi del sito"),
    "source_discovery": N_("Ricerca delle fonti"),
    "topic_label": N_("Nomi dei temi"),
    "proposal": N_("Proposte di articoli"),
    "draft": N_("Scrittura delle bozze"),
    "translate": N_("Traduzioni"),
    "image": N_("Immagini di copertina"),
}


@contextmanager
def tagged(db, tenant_id: str, purpose: str, site_id: int | None = None, ref: str | None = None):
    """The model calls made inside are recorded for this tenant, step, site
    and article (ref: "proposal:12", "topic:7", ...)."""
    token = _tag.set((db, tenant_id, purpose, site_id, ref))
    try:
        yield
    finally:
        _tag.reset(token)


def _job_id():
    cur = progress._current.get()
    return cur[2] if cur else None


def record(model: str, usage: dict | None, cost, ms: int, error: str | None = None) -> None:
    """One call (core_client.chat); never fails the work."""
    tag = _tag.get()
    if tag is None:
        return
    db, tenant_id, purpose, site_id, ref = tag
    usage = usage or {}
    try:
        with db.tenant(tenant_id) as s:
            s.execute(insert(m.llm_usage).values(
                tenant_id=tenant_id, purpose=purpose, model=model or "?",
                input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
                cost=cost, site_id=site_id, ref=ref, job_id=_job_id(), duration_ms=ms,
                error=error[:300] if error else None))
    except Exception:
        log.exception("recording a model call failed")


def tokens(v) -> str:
    """1234567 -> 1.234.567"""
    return "–" if v is None else f"{v:,}".replace(",", ".")


# --- reference prices -------------------------------------------------------------------

def prices(admin) -> dict[str, dict]:
    p = m.model_prices
    with admin.engine.connect() as conn:
        return {r.model: {"input": r.input_per_mtok, "output": r.output_per_mtok, "note": r.note}
                for r in conn.execute(select(p).order_by(p.c.model))}


def set_price(admin, model: str, input_per_mtok: float, output_per_mtok: float, note: str = "") -> None:
    model = (model or "").strip()[:200]
    if not model:
        raise ValueError("model required")
    if input_per_mtok < 0 or output_per_mtok < 0:
        raise ValueError("negative price")
    p = m.model_prices
    with admin.engine.begin() as conn:
        conn.execute(delete(p).where(p.c.model == model))
        conn.execute(p.insert().values(model=model, input_per_mtok=input_per_mtok, output_per_mtok=output_per_mtok,
                                       note=note.strip()[:200], updated_at=datetime.now(UTC)))


def delete_price(admin, model: str) -> None:
    with admin.engine.begin() as conn:
        conn.execute(delete(m.model_prices).where(m.model_prices.c.model == model))


def estimate(price: dict | None, tokens_in: int, tokens_out: int) -> float | None:
    if price is None:
        return None
    return (tokens_in * price["input"] + tokens_out * price["output"]) / 1_000_000


# --- the report ---------------------------------------------------------------------------

def _rows(db, tenants: list[str], since: datetime) -> list[dict]:
    u = m.llm_usage
    out = []
    for tenant in tenants:
        with db.tenant(tenant) as s:
            sites = dict(s.execute(select(m.sites.c.id, m.sites.c.domain)).all())
            for r in s.execute(select(u).where(u.c.at >= since).order_by(u.c.id)):
                at = r.at if r.at is None or r.at.tzinfo else r.at.replace(tzinfo=UTC)
                out.append({"tenant": tenant, "site": sites.get(r.site_id) or "", "site_id": r.site_id,
                            "purpose": r.purpose, "model": r.model, "ref": r.ref or "", "job": r.job_id,
                            "in": r.input_tokens or 0, "out": r.output_tokens or 0, "cost": r.cost or 0.0,
                            "ms": r.duration_ms, "error": r.error or "", "at": at})
    return out


def _sum(rows: list[dict], prices_: dict) -> dict:
    ok = [r for r in rows if not r["error"]]
    est = [estimate(prices_.get(r["model"]), r["in"], r["out"]) for r in ok]
    times = [r["ms"] for r in ok if r["ms"]]
    return {"calls": len(ok), "errors": len(rows) - len(ok), "in": sum(r["in"] for r in ok),
            "out": sum(r["out"] for r in ok), "cost": sum(r["cost"] for r in ok),
            "estimate": sum(e for e in est if e is not None) if any(e is not None for e in est) else None,
            "unpriced": len({r["model"] for r, e in zip(ok, est) if e is None}),
            "avg_s": round(sum(times) / len(times) / 1000, 1) if times else None}


def _group(rows: list[dict], key, prices_: dict) -> list[dict]:
    groups = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    return sorted(({"key": k, **_sum(v, prices_)} for k, v in groups.items()),
                  key=lambda g: (-(g["in"] + g["out"]), str(g["key"])))


def report(admin, db, tenants: list[str], days: int = 30) -> dict:
    """Totals per newsroom, site, step and model, and the cost of each
    article written in the period (its proposal and every writing attempt)."""
    since = datetime.now(UTC) - timedelta(days=days)
    rows = _rows(db, tenants, since)
    prices_ = prices(admin)
    articles = []
    for tenant in tenants:
        with db.tenant(tenant) as s:
            d, p = m.drafts.c, m.proposals.c
            for r in s.execute(select(d.id, d.proposal_id, d.title, d.created_at, d.site_id, p.topic_id)
                               .join(m.proposals, m.proposals.c.id == d.proposal_id)
                               .where(d.created_at >= since).order_by(d.id.desc())):
                articles.append({"tenant": tenant, "draft_id": r.id, "proposal_id": r.proposal_id, "title": r.title,
                                 "topic_id": r.topic_id, "site_id": r.site_id})
    by_ref = defaultdict(list)
    for r in rows:
        if r["ref"]:
            by_ref[(r["tenant"], r["ref"])].append(r)
    seen_writing = set()
    for a in articles:
        # Every version of a draft of the same proposal shares its writing calls:
        # they are counted on the newest one only.
        key = (a["tenant"], f"proposal:{a['proposal_id']}")
        writing = [] if key in seen_writing else by_ref.get(key, [])
        seen_writing.add(key)
        proposing = by_ref.get((a["tenant"], f"topic:{a['topic_id']}"), [])
        a["writing"], a["proposing"] = _sum(writing, prices_), _sum(proposing, prices_)
        a["total"] = _sum(writing + proposing, prices_)
    written = [a for a in articles if a["writing"]["calls"]]
    n = len(written)
    average = None
    if n:
        tot = _sum([r for a in written for r in by_ref.get((a["tenant"], f"proposal:{a['proposal_id']}"), [])
                    + by_ref.get((a["tenant"], f"topic:{a['topic_id']}"), [])], prices_)
        average = {"in": tot["in"] // n, "out": tot["out"] // n, "calls": round(tot["calls"] / n, 1),
                   "estimate": tot["estimate"] / n if tot["estimate"] is not None else None, "articles": n}
    return {"days": days, "since": since, "total": _sum(rows, prices_),
            "by_tenant": _group(rows, lambda r: r["tenant"], prices_),
            "by_site": _group([r for r in rows if r["site"]], lambda r: (r["tenant"], r["site"]), prices_),
            "by_purpose": _group(rows, lambda r: r["purpose"], prices_),
            "by_model": _group(rows, lambda r: r["model"], prices_),
            "articles": articles[:100], "average": average, "prices": prices_,
            "models": sorted({r["model"] for r in rows} | set(prices_))}


def csv_rows(db, tenants: list[str], days: int = 30) -> str:
    """Every call of the period, one line each."""
    rows = _rows(db, tenants, datetime.now(UTC) - timedelta(days=days))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["at", "newsroom", "site", "step", "ref", "job", "model", "input_tokens", "output_tokens",
                "cost", "duration_ms", "error"])
    for r in rows:
        w.writerow([r["at"].isoformat() if r["at"] else "", r["tenant"], r["site"], r["purpose"], r["ref"],
                    r["job"] or "", r["model"], r["in"], r["out"], r["cost"], r["ms"] or "", r["error"]])
    return buf.getvalue()
