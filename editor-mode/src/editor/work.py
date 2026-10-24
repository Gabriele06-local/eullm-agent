"""What each kind of queued job does. The dashboard and the API only queue
work (queue.enqueue); the worker runs it with these handlers."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from . import drafts, models as m, picks, profile as prof, proposals, sources
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
                "rejected": rep.rejected, "failed": rep.failed,
                "no_profile": any("no approved profile" in n for n in rep.notes)}

    def analysis(tenant: str, domain: str, args: dict) -> dict:
        with need(http_factory, "web access")() as client:
            res = prof.analyse_site(db, tenant, domain, SiteCrawler(client), core_for(tenant))
            out = {"site_id": res["site_id"], "domain": domain, "status": res["status"],
                   "profile_id": res["profile_id"], "changes": len(res["changes"]), "problems": res["problems"][:3]}
            if res.get("model_failed") and not res["profile_id"]:
                out["incomplete"] = res["model_failed"]
            if res["profile_id"]:
                with db.tenant(tenant) as s:
                    body = s.execute(select(m.editorial_profiles.c.body)
                                     .where(m.editorial_profiles.c.id == res["profile_id"])).scalar() or {}
                if not (body.get("sector") or {}).get("value"):
                    # What the model could not say, and why (its error, if any).
                    out["incomplete"] = next((g for g in body.get("gaps", []) if "model" in g),
                                             (body.get("gaps") or [""])[0])
            if res["status"] != "failed":
                step(N_("Ricerca delle fonti per {domain}"), domain=domain)
                rep = sources.discover(db, tenant, res["site_id"], client, core_for(tenant))
                out["sources"] = found(rep)
            return out

    def discovery(tenant: str, subject: str, args: dict) -> dict:
        with need(http_factory, "web access")() as client:
            rep = sources.discover(db, tenant, args["site_id"], client, core_for(tenant))
        return {"site_id": args["site_id"], **found(rep)}

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
            out["why"] = picks.why_none(db, tenant, args["site_id"])
        return out

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
