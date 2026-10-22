"""What each kind of queued job does. The dashboard and the API only queue
work (queue.enqueue); the worker runs it with these handlers."""

from __future__ import annotations

from datetime import UTC, datetime

from . import drafts, profile as prof, proposals, sources
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

    def analysis(tenant: str, domain: str, args: dict) -> None:
        with need(http_factory, "web access")() as client:
            res = prof.analyse_site(db, tenant, domain, SiteCrawler(client), core_for(tenant))
            if res["status"] != "failed":
                sources.discover(db, tenant, res["site_id"], client, core_for(tenant))

    def discovery(tenant: str, subject: str, args: dict) -> None:
        with need(http_factory, "web access")() as client:
            sources.discover(db, tenant, args["site_id"], client, core_for(tenant))

    def draft(tenant: str, subject: str, args: dict) -> None:
        need(core, "model (Core)")
        if http_factory is None:
            drafts.write_draft(db, tenant, args["proposal_id"], core_for(tenant), None)
            return
        with http_factory() as client:
            drafts.write_draft(db, tenant, args["proposal_id"], core_for(tenant), client)

    def propose(tenant: str, subject: str, args: dict) -> None:
        proposals.top_up(db, tenant, args["site_id"], datetime.fromisoformat(args["since"]), core_for(tenant),
                         manual=bool(args.get("manual")))

    def titles(tenant: str, subject: str, args: dict) -> None:
        proposals.propose_titles(db, tenant, args["site_id"], args["topic_id"], core_for(tenant))

    def publish(tenant: str, subject: str, args: dict) -> None:
        res = need(publisher, "publisher").run(db, tenant, args["publication_id"])
        if res.get("status") == "failed":
            raise RuntimeError(res.get("error") or "publication failed")

    def tick(tenant: str, subject: str, args: dict) -> None:
        done, failed = [], []
        need(runner, "scheduler").tenant_tick(tenant, datetime.now(UTC), done, failed)
        if failed:
            raise RuntimeError("; ".join(failed))

    def collect(tenant: str, subject: str, args: dict) -> None:
        done, failed = [], []
        need(runner, "scheduler").collect_now(tenant, datetime.now(UTC), done, failed)
        if failed:
            raise RuntimeError("; ".join(failed))

    def archive(tenant: str, subject: str, args: dict) -> None:
        done, failed = [], []
        need(runner, "scheduler").archive_tick(datetime.now(UTC), done, failed)
        if failed:
            raise RuntimeError("; ".join(failed))

    return {"analysis": analysis, "discovery": discovery, "draft": draft, "publish": publish, "tick": tick,
            "proposals": propose, "titles": titles, "archive": archive,
            "collect": collect}
