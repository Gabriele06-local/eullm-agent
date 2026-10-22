"""What each kind of queued job does. The dashboard and the API only queue
work (queue.enqueue); the worker runs it with these handlers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from . import drafts, models as m, profile as prof, proposals, sources
from .i18n import N_
from .progress import step
from .queue import Handlers
from .site import SiteCrawler


def handlers(db, http_factory=None, core=None, publisher=None, runner=None) -> Handlers:
    """http_factory: () -> httpx.Client reading the web through the Core;
    core: CoreClient for the model; runner: schedule.Runner for the tick.
    Work that needs a missing piece fails with a message instead of
    pretending to have run."""

    def core_for(tenant: str):
        return core.for_tenant(db, tenant) if core is not None else None

    def need(what, name: str):
        if what is None:
            raise RuntimeError(f"this worker has no {name}")
        return what

    def found(rep) -> dict:
        return {"candidates": rep.candidates, "active": rep.active, "candidate": rep.candidate,
                "rejected": rep.rejected, "failed": rep.failed}

    def analysis(tenant: str, domain: str, args: dict) -> dict:
        with need(http_factory, "web access")() as client:
            res = prof.analyse_site(db, tenant, domain, SiteCrawler(client), core_for(tenant))
            out = {"site_id": res["site_id"], "domain": domain, "status": res["status"],
                   "profile_id": res["profile_id"], "changes": len(res["changes"]), "problems": res["problems"][:3]}
            if res["status"] != "failed":
                step(N_("Ricerca delle fonti per {domain}"), domain=domain)
                rep = sources.discover(db, tenant, res["site_id"], client, core_for(tenant))
                out["sources"] = found(rep)
            return out

    def discovery(tenant: str, subject: str, args: dict) -> dict:
        with need(http_factory, "web access")() as client:
            rep = sources.discover(db, tenant, args["site_id"], client, core_for(tenant))
        return {"site_id": args["site_id"], **found(rep), "no_profile": any("no approved profile" in n for n in rep.notes)}

    def draft(tenant: str, subject: str, args: dict) -> dict:
        need(core, "model (Core)")
        if http_factory is None:
            did = drafts.write_draft(db, tenant, args["proposal_id"], core_for(tenant), None)
        else:
            with http_factory() as client:
                did = drafts.write_draft(db, tenant, args["proposal_id"], core_for(tenant), client)
        with db.tenant(tenant) as s:
            d = s.execute(select(m.drafts.c.title, m.drafts.c.status, m.drafts.c.site_id)
                          .where(m.drafts.c.id == did)).first()
        return {"draft_id": did, "title": d.title, "status": d.status, "site_id": d.site_id}

    def propose(tenant: str, subject: str, args: dict) -> dict:
        step(N_("Scelgo i temi migliori e scrivo le proposte"))
        ids = proposals.top_up(db, tenant, args["site_id"], datetime.fromisoformat(args["since"]), core_for(tenant),
                               manual=bool(args.get("manual")))
        out = {"site_id": args["site_id"], "proposals": len(ids)}
        if not ids:
            out["best"] = best_topic(tenant, args["site_id"])
        return out

    def best_topic(tenant: str, site_id: int) -> dict | None:
        """Why nothing was proposed: the topic closest to a proposal, with
        its Opportunity and how many news and sources it has."""
        o = m.opportunity_scores
        with db.tenant(tenant) as s:
            last = s.execute(select(func.max(o.c.computed_at)).where(o.c.site_id == site_id)).scalar()
            if last is None:
                return None
            r = s.execute(select(o.c.topic_id, o.c.score, m.topics.c.label)
                          .join(m.topics, m.topics.c.id == o.c.topic_id)
                          .where(o.c.site_id == site_id, o.c.computed_at >= last - timedelta(minutes=30))
                          .order_by(o.c.score.desc()).limit(1)).first()
            if r is None:
                return None
            news = s.execute(select(m.source_items.c.source_id).join(
                m.topic_items, m.topic_items.c.item_id == m.source_items.c.id)
                .where(m.topic_items.c.topic_id == r.topic_id)).scalars().all()
        return {"label": r.label, "opportunity": round(r.score), "items": len(news), "sources": len(set(news))}

    def titles(tenant: str, subject: str, args: dict) -> dict:
        step(N_("Il modello scrive i titoli"))
        ids = proposals.propose_titles(db, tenant, args["site_id"], args["topic_id"], core_for(tenant))
        return {"site_id": args["site_id"], "proposals": len(ids)}

    def publish(tenant: str, subject: str, args: dict) -> dict:
        res = need(publisher, "publisher").run(db, tenant, args["publication_id"])
        if res.get("status") == "failed":
            raise RuntimeError(res.get("error") or "publication failed")
        return {"url": res.get("url"), "indexnow": res.get("indexnow")}

    def tick(tenant: str, subject: str, args: dict) -> None:
        done, failed = [], []
        need(runner, "scheduler").tenant_tick(tenant, datetime.now(UTC), done, failed)
        if failed:
            raise RuntimeError("; ".join(failed))

    def collect(tenant: str, subject: str, args: dict) -> dict:
        done, failed = [], []
        out = need(runner, "scheduler").collect_now(tenant, datetime.now(UTC), done, failed)
        if failed:
            raise RuntimeError("; ".join(failed))
        return out

    def archive(tenant: str, subject: str, args: dict) -> None:
        done, failed = [], []
        need(runner, "scheduler").archive_tick(datetime.now(UTC), done, failed)
        if failed:
            raise RuntimeError("; ".join(failed))

    return {"analysis": analysis, "discovery": discovery, "draft": draft, "publish": publish, "tick": tick,
            "proposals": propose, "titles": titles, "archive": archive,
            "collect": collect}
