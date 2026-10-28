"""Changing and removing what a person created in the newsroom: sites,
publishing targets, briefing recipients, drafts and the newsroom's own
preferences. Whatever can be created from the dashboard can be corrected
or deleted from it, without a command line."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update

from . import models as m
from .i18n import t
from .repo import ARCHIVE


class ManageError(ValueError):
    """What cannot be done, in words for the person who asked."""


def _site_jobs(s, site_id: int, domain: str) -> list:
    """The queued or running jobs about this site."""
    j = m.jobs.c
    rows = s.execute(select(j.id, j.status, j.subject, j.args).where(j.status.in_(("queued", "running")))).all()
    return [r for r in rows if r.subject == domain or (r.args or {}).get("site_id") == site_id]


def delete_site(db, tenant_id: str, site_id: int) -> str:
    """Delete a site with everything that belongs to it (profile versions,
    analyses, followed sources, proposals, drafts, publishing targets and
    their history). The shared archive is not touched. Work queued for the
    site is dropped; a site that is being read or written about right now
    is deleted when that work ends."""
    with db.tenant(tenant_id) as s:
        domain = s.execute(select(m.sites.c.domain).where(m.sites.c.id == site_id)).scalar()
        if domain is None:
            raise LookupError(site_id)
        jobs = _site_jobs(s, site_id, domain)
        if any(r.status == "running" for r in jobs):
            raise ManageError(t("{domain} ha un lavoro in corso: aspetta che finisca (lo vedi in Attività) e "
                                "riprova.", domain=domain))
        if jobs:
            s.execute(delete(m.jobs).where(m.jobs.c.id.in_([r.id for r in jobs])))
        secrets = set(s.execute(select(m.publish_targets.c.secret_env).where(
            m.publish_targets.c.site_id == site_id, m.publish_targets.c.secret_env.is_not(None))).scalars())
        s.execute(delete(m.sites).where(m.sites.c.id == site_id))
        _forget_unused_secrets(s, secrets)
    return domain


def rename_site(db, tenant_id: str, site_id: int, name: str) -> None:
    name = " ".join((name or "").split())[:100]
    if not name:
        raise ManageError(t("Il nome del sito non può essere vuoto."))
    with db.tenant(tenant_id) as s:
        if not s.execute(update(m.sites).where(m.sites.c.id == site_id).values(name=name)).rowcount:
            raise LookupError(site_id)


def can_change_domain(s, site_id: int) -> bool:
    """A domain can be corrected until a profile of the site is approved:
    before that nothing has been decided on it. After, the site is deleted
    and added again."""
    p = m.editorial_profiles
    return s.execute(select(func.count()).select_from(p).where(
        p.c.site_id == site_id, p.c.status == "approved")).scalar() == 0


# --- publishing targets -----------------------------------------------------------------

def _forget_unused_secrets(s, names: set[str]) -> None:
    """Credentials stored for targets that are gone, when no target uses them."""
    for name in names:
        used = s.execute(select(func.count()).select_from(m.publish_targets).where(
            m.publish_targets.c.secret_env == name)).scalar()
        if not used:
            s.execute(delete(m.secrets).where(m.secrets.c.name == name))


def update_target(db, tenant_id: str, target_id: int, *, name: str, address: str, user: str = "",
                  indexnow_key: str = "", jsonld: bool = False, enabled: bool = True,
                  branch: str = "", path: str = "", image_path: str = "", article_url: str = "",
                  verify: bool = True) -> None:
    """Change a target's name, address and options. Its credential is
    replaced on its own (vault). branch, path and image_path are the github
    target's places in the repository."""
    import re

    name = (name or "").strip()[:100]
    if not name:
        raise ManageError(t("La destinazione ha bisogno di un nome."))
    with db.tenant(tenant_id) as s:
        row = s.execute(select(m.publish_targets).where(m.publish_targets.c.id == target_id)).first()
        if row is None:
            raise LookupError(target_id)
        config = dict(row.config or {})
        if row.kind == "telegram_channel":
            config["chat_id"] = address.strip()
        elif row.kind == "github":
            config["repo"] = address.strip()
            config["verify"] = verify
            for key, value in (("branch", branch), ("path", path), ("image_path", image_path), ("url", article_url)):
                if value.strip():
                    config[key] = value.strip()
                else:
                    config.pop(key, None)
        else:
            config["url"] = address.strip()
        if row.kind == "wordpress":
            config["user"] = user.strip()
            if jsonld:
                config["jsonld"] = True
            else:
                config.pop("jsonld", None)
        key = indexnow_key.strip()
        if key and not re.fullmatch(r"[A-Za-z0-9-]{8,128}", key):
            raise ManageError(t("La chiave IndexNow va da 8 a 128 caratteri tra lettere, numeri e trattini."))
        if key:
            config["indexnow_key"] = key
        else:
            config.pop("indexnow_key", None)
        if not (config.get("url") or config.get("chat_id") or config.get("repo")):
            raise ManageError(t("Manca l'indirizzo della destinazione."))
        if row.kind == "wordpress" and not config.get("user"):
            raise ManageError(t("WordPress ha bisogno dell'utente della password applicativa."))
        if row.kind in ("wordpress", "webhook"):
            from . import publishing
            try:
                publishing.check_public_url(config["url"])
            except publishing.PublishError as e:
                raise ManageError(str(e)) from None
        if row.kind == "github":
            from . import publishing
            try:
                config = publishing.github_config(config)
            except publishing.PublishError as e:
                raise ManageError(str(e)) from None
        s.execute(update(m.publish_targets).where(m.publish_targets.c.id == target_id).values(
            name=name, config=config, enabled=enabled))


def delete_target(db, tenant_id: str, target_id: int) -> str:
    """Delete a target with its publication history; articles already on
    the site stay there. Not while a publication to it is being sent."""
    with db.tenant(tenant_id) as s:
        row = s.execute(select(m.publish_targets.c.name, m.publish_targets.c.secret_env).where(
            m.publish_targets.c.id == target_id)).first()
        if row is None:
            raise LookupError(target_id)
        sending = s.execute(select(func.count()).select_from(m.publications).where(
            m.publications.c.target_id == target_id, m.publications.c.status.in_(("approved", "sending")))).scalar()
        if sending:
            raise ManageError(t("Una pubblicazione verso {name} è in invio: riprova quando è finita.", name=row.name))
        s.execute(delete(m.publish_targets).where(m.publish_targets.c.id == target_id))
        if row.secret_env:
            _forget_unused_secrets(s, {row.secret_env})
    return row.name


# --- briefing recipients ----------------------------------------------------------------

def update_recipient(db, tenant_id: str, rid: int, channel: str, address: str, enabled: bool) -> None:
    address = (address or "").strip()
    if channel not in ("email", "telegram") or not address or len(address) > 200 \
            or any(ch in address for ch in "\r\n\t,;<>"):
        raise ManageError(t("Destinatario non valido."))
    if channel == "email" and ("@" not in address or " " in address):
        raise ManageError(t("Indirizzo email non valido."))
    with db.tenant(tenant_id) as s:
        r = m.recipients.c
        twin = s.execute(select(r.id).where(r.channel == channel, r.address == address, r.id != rid)).scalar()
        if twin is not None:
            raise ManageError(t("Questo destinatario c'è già."))
        if not s.execute(update(m.recipients).where(r.id == rid).values(
                channel=channel, address=address, enabled=enabled)).rowcount:
            raise LookupError(rid)


# --- drafts ---------------------------------------------------------------------------

def delete_draft(db, tenant_id: str, draft_id: int) -> str:
    """Delete a draft and its publication requests; the article already
    sent to a site stays there. The proposal goes back to "accepted" when
    it has no other draft, so it can be written again."""
    with db.tenant(tenant_id) as s:
        d = s.execute(select(m.drafts.c.title, m.drafts.c.proposal_id).where(m.drafts.c.id == draft_id)).first()
        if d is None:
            raise LookupError(draft_id)
        sending = s.execute(select(func.count()).select_from(m.publications).where(
            m.publications.c.draft_id == draft_id, m.publications.c.status.in_(("approved", "sending")))).scalar()
        if sending:
            raise ManageError(t("Questa bozza è in invio a un sito: riprova quando l'invio è finito."))
        s.execute(delete(m.drafts).where(m.drafts.c.id == draft_id))
        others = s.execute(select(func.count()).select_from(m.drafts).where(
            m.drafts.c.proposal_id == d.proposal_id)).scalar()
        if not others:
            s.execute(update(m.proposals).where(m.proposals.c.id == d.proposal_id,
                                                m.proposals.c.status == "drafted").values(status="accepted"))
    return d.title


# --- the newsroom's preferences -----------------------------------------------------------

LANGUAGES = ("it", "en", "de", "fr", "es")


def preferences(admin, tenant_id: str, timezone: str, briefing_hour: str, language: str) -> dict:
    """Timezone, briefing hour and language of the newsroom, as its owner
    sets them (the plan and the limits stay with the operator)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from . import newsrooms

    tz = (timezone or "").strip()
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ManageError(t("Fuso orario sconosciuto: {tz}. Esempio: Europe/Rome.", tz=tz)) from None
    try:
        hour = int(briefing_hour)
    except (TypeError, ValueError):
        raise ManageError(t("L'ora del briefing è un numero da 0 a 23.")) from None
    if not 0 <= hour <= 23:
        raise ManageError(t("L'ora del briefing è un numero da 0 a 23."))
    if language not in LANGUAGES:
        raise ManageError(t("Lingua non disponibile."))
    if tenant_id == ARCHIVE:
        raise LookupError(tenant_id)
    newsrooms.update_newsroom(admin, tenant_id, {"timezone": tz, "briefing_hour": hour, "language": language})
    return {"timezone": tz, "briefing_hour": hour, "language": language, "at": datetime.now(UTC)}
