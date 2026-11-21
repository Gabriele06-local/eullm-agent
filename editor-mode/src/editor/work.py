"""What each kind of queued job does. The dashboard and the API only queue
work (queue.enqueue); the worker runs it with these handlers."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select

from . import autopilot, delivery, drafts, evening as ev, images, menus, models as m, picks, profile as prof, proposals, quotas, sources, translate as tr
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
                "rejected": rep.rejected, "failed": rep.failed, "notes": [n[:160] for n in rep.notes[:3]],
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
            rep = sources.discover(db, tenant, args["site_id"], client, core_for(tenant),
                                   urls=args.get("urls"), by=args.get("by") or "")
        return {"site_id": args["site_id"], **found(rep)}

    def draft(tenant: str, subject: str, args: dict) -> dict:
        need(core, "model (Core)")
        notes = args.get("notes")  # written again: what the director wants changed
        if http_factory is None:
            did = drafts.write_draft(db, tenant, args["proposal_id"], core_for(tenant), None, notes=notes)
        else:
            with http_factory() as client:
                did = drafts.write_draft(db, tenant, args["proposal_id"], core_for(tenant), client, notes=notes)
        if args.get("replaces"):
            drafts.mark_replacement(db, tenant, did, args["replaces"])
        with db.tenant(tenant) as s:
            d = s.execute(select(m.drafts.c.title, m.drafts.c.status, m.drafts.c.site_id)
                          .where(m.drafts.c.id == did)).first()
        if imager is not None and not args.get("compare"):  # the cover follows, as a job of its own
            with db.tenant(tenant) as s:
                recipes = menus.children(s, did)  # a menu's new recipes have their own photograph
            for x in [did, *recipes]:
                enqueue(db, tenant, "image", f"draft {x}", {"draft_id": x, "site_id": d.site_id})
        return {"draft_id": did, "title": d.title, "status": d.status, "site_id": d.site_id}

    def auto(tenant: str, subject: str, args: dict) -> dict:
        """One article of the automatic newsroom, from the accepted proposal
        to the publication: draft, checks and editor-in-chief (written again
        or discarded when it fails), then the cover and the site."""
        need(core, "model (Core)")
        # The automatic newsroom stays within the plan's articles: credits are spent only on what
        # a person asks for, and on the extras the newsroom was allowed to pay (args["paid"]).
        pay = bool(args.get("paid"))
        with db.tenant(tenant) as s:  # the outlets the article must cite, asked from the first writing
            outlets = autopilot.settings_of(s.execute(select(m.editorial_profiles.c.body).where(
                m.editorial_profiles.c.site_id == args["site_id"],
                m.editorial_profiles.c.status == "approved")).scalar())["min_sources"]
            # As many as the news has: one outlet's story is written from that one.
            outlets = min(outlets, max(1, autopilot.source_count(s, args["proposal_id"], outlets)))

        def write(purpose="draft", notes=None):
            if http_factory is None:
                return drafts.write_draft(db, tenant, args["proposal_id"], core_for(tenant), None, pay=pay,
                                          purpose=purpose, notes=notes, outlets=outlets, min_words=drafts.THIN_WORDS)
            with http_factory() as client:
                return drafts.write_draft(db, tenant, args["proposal_id"], core_for(tenant), client, pay=pay,
                                          purpose=purpose, notes=notes, outlets=outlets, min_words=drafts.THIN_WORDS)

        def cover(did):  # drawn only for an article that passed every check
            if imager is not None:
                images.make(db, tenant, did, imager, core_for(tenant))

        # A story told yesterday, a column, a round-up: closed before the writing is paid for.
        skipped = autopilot.worth_writing(db, tenant, args["proposal_id"], core_for(tenant))
        if skipped:
            return skipped
        try:
            did = write()
        except drafts.Thin as e:  # nothing paid: another proposal takes its place
            return autopilot.too_thin(db, tenant, args["proposal_id"], e.words)
        return autopilot.finish(db, tenant, did, core_for(tenant), publish_job(tenant, args["site_id"]),
                                lambda language, final: enqueue(db, tenant, "translate", f"draft {final} {language}", {
                                    "draft_id": final, "language": language, "site_id": args["site_id"], "auto": True}),
                                rewrite=lambda _old, notes: write("auto_fix", notes), cover=cover,
                                translate_now=translate_now(tenant))

    def wpmenu(tenant: str, subject: str, args: dict) -> dict:
        """The WordPress menu with the site's sections."""
        site_id = args["site_id"]
        with db.tenant(tenant) as s:
            # Those just confirmed (the profile from the context may not be approved yet), or the profile's.
            sections = args.get("sections") or autopilot.settings_of(s.execute(select(m.editorial_profiles.c.body).where(
                m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")).scalar())["sections"]
            targets = s.execute(select(m.publish_targets).where(
                m.publish_targets.c.site_id == site_id, m.publish_targets.c.kind == "wordpress",
                m.publish_targets.c.enabled.is_not(False))
                .order_by(m.publish_targets.c.id)).all()
        if not sections:
            return {"site_id": site_id, "error": "no_sections"}
        if not targets:
            return {"site_id": site_id, "error": "no_wordpress"}
        out = {"site_id": site_id, "sites": []}
        for target in targets:  # one per language, on one WordPress or more
            res = need(publisher, "publisher").wordpress_menu(db, target, sections)
            out["sites"].append({"name": target.name, **res})
        return out

    def request(tenant: str, subject: str, args: dict) -> dict:
        """An article asked for in the newsroom's own words: its links read,
        the archive searched, the draft written, or why there is none."""
        from . import requested

        need(core, "model (Core)")
        try:
            if http_factory is None:
                out = requested.ask(db, tenant, args["site_id"], args["text"], args.get("links") or [],
                                    args.get("by") or "persona", core_for(tenant), None,
                                    kind=args.get("kind") or "article")
            else:
                with http_factory() as client:
                    out = requested.ask(db, tenant, args["site_id"], args["text"], args.get("links") or [],
                                        args.get("by") or "persona", core_for(tenant), client,
                                    kind=args.get("kind") or "article")
        except quotas.QuotaExceeded as e:
            return {"site_id": args["site_id"], "skipped": str(e)}
        if out.get("draft_id") and imager is not None:  # the cover follows, as a job of its own
            enqueue(db, tenant, "image", f"draft {out['draft_id']}", {"draft_id": out["draft_id"], "site_id": out["site_id"]})
        return out

    def evening(tenant: str, subject: str, args: dict) -> dict:
        """Il Punto della Sera: the director's analysis of the site's day.
        With the automatic newsroom on it goes through the same checks and
        out by itself; otherwise it waits in Drafts for the director."""
        need(core, "model (Core)")
        site_id, tz = args["site_id"], args.get("tz") or "UTC"
        now = datetime.now(UTC)
        try:
            did = ev.write(db, tenant, site_id, core_for(tenant), now, tz)
        except quotas.QuotaExceeded as e:
            return {"site_id": site_id, "skipped": str(e)}
        if did is None:
            return {"site_id": site_id, "skipped": "few articles"}
        with db.tenant(tenant) as s:
            d = s.execute(select(m.drafts.c.title, m.drafts.c.status, m.drafts.c.proposal_id)
                          .where(m.drafts.c.id == did)).first()
            auto = autopilot.settings_of(s.execute(select(m.editorial_profiles.c.body).where(
                m.editorial_profiles.c.site_id == site_id,
                m.editorial_profiles.c.status == "approved")).scalar())["enabled"] and quotas.autopilot_allowed(s)
        if not auto:
            if imager is not None:
                enqueue(db, tenant, "image", f"draft {did}", {"draft_id": did, "site_id": site_id})
            return {"draft_id": did, "title": d.title, "status": d.status, "site_id": site_id}

        def cover(draft_id):
            if imager is not None:
                images.make(db, tenant, draft_id, imager, core_for(tenant))

        return autopilot.finish(db, tenant, did, core_for(tenant), publish_job(tenant, site_id),
                                lambda language, final: enqueue(db, tenant, "translate", f"draft {final} {language}", {
                                    "draft_id": final, "language": language, "site_id": site_id, "auto": True}),
                                rewrite=lambda _old, notes: ev.write(db, tenant, site_id, core_for(tenant), now, tz,
                                                                     notes=notes, proposal_id=d.proposal_id),
                                cover=cover, translate_now=translate_now(tenant))

    def publish_job(tenant: str, site_id):
        # `together`: the article's translations, in the same commit when they go to the same repository.
        return lambda pub_id, together=(): enqueue(db, tenant, "publish", f"publication {pub_id}", {
            "publication_id": pub_id, "site_id": site_id, **({"together": list(together)} if together else {})})

    def translate_now(tenant: str):
        return lambda language, draft_id: tr.translate(db, tenant, draft_id, language,
                                                       need(core_for(tenant), "model (Core)"), pay=False)

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
            family = [args["draft_id"]]
            for tid, title in s.execute(select(m.drafts.c.id, m.drafts.c.title)
                                        .where(m.drafts.c.translation_of == args["draft_id"])).all():
                images.copy(s, tenant, args["draft_id"], tid, title)
                family.append(tid)
            # Already online: the new cover is not there until «Aggiorna sul sito».
            p, tg = m.publications.c, m.publish_targets.c
            online = s.execute(select(func.count()).select_from(m.publications.join(
                m.publish_targets, tg.id == p.target_id)).where(
                p.draft_id.in_(family), p.status == "published", p.mode == "publish", p.withdrawn_at.is_(None),
                tg.kind != "telegram_channel")).scalar() or 0
        return {"draft_id": args["draft_id"], "title": d.title if d else "", "site_id": d.site_id if d else None,
                "online": online}

    def translation(tenant: str, subject: str, args: dict) -> dict:
        did = tr.translate(db, tenant, args["draft_id"], args["language"], need(core_for(tenant), "model (Core)"),
                           pay=not args.get("auto"))
        if args.get("auto") or autopilot.owns(db, tenant, did):
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
        together = args.get("together") or []
        res = need(publisher, "publisher").run(db, tenant, args["publication_id"], together)
        if res.get("status") == "failed":
            raise RuntimeError(res.get("error") or "publication failed")
        # The last new recipe of a menu online: the menu waiting for it goes out.
        for pid in (args["publication_id"], *together):
            menus.after_publication(db, tenant, pid, publish_job(tenant, args.get("site_id")))
        return {"url": res.get("url"), "indexnow": res.get("indexnow")}

    def change(tenant: str, subject: str, args: dict) -> dict:
        """The director's change to a published article: update or withdraw."""
        pub = need(publisher, "publisher")
        if args["op"] == "withdraw":
            step(N_("Ritiro l'articolo dal sito"))
            res = pub.withdraw(db, tenant, args["publication_id"], args.get("by") or "director")
        else:
            step(N_("Aggiorno l'articolo sul sito"))
            res = pub.update(db, tenant, args["publication_id"], args.get("by") or "director", args.get("draft_id"))
        if res.get("status") == "failed":
            raise RuntimeError(res.get("error") or "change failed")
        with db.tenant(tenant) as s:
            row = s.execute(select(m.drafts.c.id, m.drafts.c.title, m.drafts.c.site_id)
                            .join(m.publications, m.publications.c.draft_id == m.drafts.c.id)
                            .where(m.publications.c.id == args["publication_id"])).first()
        return {"op": args["op"], "url": res.get("url"), "draft_id": row.id if row else None,
                "title": row.title if row else "", "site_id": row.site_id if row else None}

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

    return {"analysis": analysis, "discovery": discovery, "draft": draft, "publish": publish, "change": change, "tick": tick,
            "proposals": propose, "titles": titles, "archive": archive,
            "collect": collect, "translate": translation, "image": image,
            "deliver": deliver, "auto": auto, "brief": brief, "evening": evening,
            "request": request, "wpmenu": wpmenu}
