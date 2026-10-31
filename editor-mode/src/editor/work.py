"""What each kind of queued job does. The dashboard and the API only queue
work (queue.enqueue); the worker runs it with these handlers."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select

from . import autopilot, delivery, drafts, images, models as m, picks, profile as prof, proposals, sources, translate as tr
from .i18n import N_
from .progress import step
from .queue import Handlers, enqueue
from .site import SiteCrawler

log = logging.getLogger(__name__)


def handlers(db, http_factory=None, core=None, publisher=None, runner=None, imager=None, mail=None,
             public_url: str = "") -> Handlers:
    """http_factory: () -> httpx.Client reading the web through the Core;
    core: CoreClient for the model; runner: schedule.Runner for the tick;
    imager: images.Imager for the cover images (None: drafts have none);
    mail: delivery.smtp_sender for the approved articles (None: no SMTP);
    public_url: the dashboard's address, for the image links in the emails.
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
        if imager is not None:  # the cover image follows, as a job of its own
            enqueue(db, tenant, "image", f"draft {did}", {"draft_id": did, "site_id": d.site_id})
        return {"draft_id": did, "title": d.title, "status": d.status, "site_id": d.site_id}

    def auto(tenant: str, subject: str, args: dict) -> dict:
        """One article of the automatic newsroom, from the accepted proposal
        to the publication: draft, cover, editor-in-chief, then the site."""
        need(core, "model (Core)")
        if http_factory is None:
            did = drafts.write_draft(db, tenant, args["proposal_id"], core_for(tenant), None)
        else:
            with http_factory() as client:
                did = drafts.write_draft(db, tenant, args["proposal_id"], core_for(tenant), client)
        if imager is not None:
            step(N_("Immagine di copertina"))
            try:
                images.make(db, tenant, did, imager, core_for(tenant))
            except Exception as e:  # an article without a cover still goes out
                log.warning("cover for draft %s failed: %s", did, e)
        return autopilot.finish(db, tenant, did, core_for(tenant), publish_job(tenant, args["site_id"]),
                                lambda language: enqueue(db, tenant, "translate", f"draft {did} {language}", {
                                    "draft_id": did, "language": language, "site_id": args["site_id"], "auto": True}))

    def publish_job(tenant: str, site_id):
        return lambda pub_id: enqueue(db, tenant, "publish", f"publication {pub_id}",
                                      {"publication_id": pub_id, "site_id": site_id})

    def brief(tenant: str, subject: str, args: dict) -> dict:
        step(N_("Il modello ricava il profilo dal contesto"))
        version = autopilot.profile_from_brief(db, tenant, args["site_id"], args["language"],
                                               need(core_for(tenant), "model (Core)"), args.get("auto"))
        return {"site_id": args["site_id"], "version": version}

    def image(tenant: str, subject: str, args: dict) -> dict:
        images.make(db, tenant, args["draft_id"], need(imager, "image service (EDITOR_IMAGES_API_KEY)"),
                    core_for(tenant))
        with db.tenant(tenant) as s:
            d = s.execute(select(m.drafts.c.title, m.drafts.c.site_id).where(m.drafts.c.id == args["draft_id"])).first()
            # Translations written before the image take it too. Their old
            # alternative text described the old image: until it is translated
            # again, each one takes its own title, in its own language.
            for tid, title in s.execute(select(m.drafts.c.id, m.drafts.c.title)
                                        .where(m.drafts.c.translation_of == args["draft_id"])).all():
                images.copy(s, tenant, args["draft_id"], tid, title)
        return {"draft_id": args["draft_id"], "title": d.title if d else "", "site_id": d.site_id if d else None}

    def translation(tenant: str, subject: str, args: dict) -> dict:
        did = tr.translate(db, tenant, args["draft_id"], args["language"], need(core_for(tenant), "model (Core)"))
        if args.get("auto"):
            return autopilot.finish_translation(db, tenant, did, publish_job(tenant, args.get("site_id")))
        with db.tenant(tenant) as s:
            d = s.execute(select(m.drafts.c.title, m.drafts.c.status, m.drafts.c.site_id, m.drafts.c.language)
                          .where(m.drafts.c.id == did)).first()
        return {"draft_id": did, "title": d.title, "status": d.status, "site_id": d.site_id, "language": d.language}

    def deliver(tenant: str, subject: str, args: dict) -> dict:
        step(N_("Invio l'articolo approvato per email"))
        out = delivery.email(db, tenant, args["draft_id"], mail, public_url)
        return {"draft_id": args["draft_id"], **out}

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
            "collect": collect, "translate": translation, "image": image,
            "deliver": deliver, "auto": auto, "brief": brief}
