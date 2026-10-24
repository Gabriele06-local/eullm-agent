"""How the approved articles reach the people who publish them, when the
site is not connected to a destination (WordPress, webhook, Telegram).

Three ways, the same article in each:

- the package: a ZIP to download from the draft, with the page ready to open
  (article.html), the text alone to paste into any CMS (body.html), Markdown
  with front matter for the static sites (article.md: Hugo, Jekyll, Astro),
  the cover image and every search field (meta.json);
- the email: at each approval the newsroom's email recipients get the
  article in the message and the package attached;
- the feed: one per site, JSON Feed 1.1 and RSS 2.0, with the approved
  articles complete, for Zapier, Make, n8n or a site that imports a feed.
  Its address holds an unguessable token and needs no login.
"""

from __future__ import annotations

import html
import io
import json
import re
import secrets
import smtplib
import ssl
import zipfile
from datetime import UTC
from email.message import EmailMessage
from email.utils import format_datetime

from sqlalchemy import delete, func, insert, select, text

from . import models as m
from .i18n import t
from .markdown import to_html
from .publishing import search_fields

FEED_ITEMS = 50


def body_md(md: str) -> str:
    """The article without its title and subtitle, which the CMS has fields for."""
    blocks = re.split(r"\n\s*\n", md.strip())
    if blocks and blocks[0].startswith("# "):
        blocks = blocks[1:]
        if blocks and re.fullmatch(r"_[^\n]+_", blocks[0].strip()):
            blocks = blocks[1:]
    return "\n\n".join(blocks)


def article(s, draft_id: int, public_url: str = "") -> dict | None:
    """Everything a site needs to publish the draft, image included (bytes in
    `image["content"]`, its address in `image["url"]` when the dashboard has a
    public address)."""
    d = s.execute(select(m.drafts).where(m.drafts.c.id == draft_id)).first()
    if d is None:
        return None
    di, si, dc, dcs = m.draft_images.c, m.source_items, m.draft_claims, m.draft_claim_sources
    img = s.execute(select(di.mime, di.content, di.alt, di.token).where(di.draft_id == draft_id)).first()
    sources = s.execute(select(si.c.title, si.c.url).join(dcs, dcs.c.item_id == si.c.id)
                        .join(dc, dc.c.id == dcs.c.claim_id).where(dc.c.draft_id == draft_id)
                        .group_by(si.c.id, si.c.title, si.c.url).order_by(func.min(dc.c.ordinal))).all()
    site = s.execute(select(m.sites.c.name, m.sites.c.domain).where(m.sites.c.id == d.site_id)).first()
    body = body_md(d.body_md)
    out = {
        "id": d.id, "status": d.status, "title": d.title, "subtitle": d.subtitle or "", "language": d.language,
        "translation_of": d.translation_of, "approved_at": d.decided_at, "site": site.domain if site else None,
        "site_name": site.name if site else None,
        "markdown": body, "html": to_html(body), "sources": [{"title": x.title, "url": x.url} for x in sources],
        **search_fields(s, d),
    }
    if out["approved_at"] is not None and out["jsonld"]:
        out["jsonld"][0]["datePublished"] = out["jsonld"][0]["dateModified"] = out["approved_at"].isoformat()
    out["image"] = None
    if img is not None:
        ext = "png" if img.mime == "image/png" else "jpg"
        base = public_url.rstrip("/")
        out["image"] = {"file": f"cover.{ext}", "alt": img.alt, "type": img.mime, "content": bytes(img.content),
                        "url": f"{base}/media/{img.token}" if base else None}
    return out


def _meta(a: dict) -> dict:
    keep = ("title", "subtitle", "language", "slug", "seo_title", "meta_description", "keyphrase", "site",
            "sources", "jsonld")
    out = {k: a[k] for k in keep}
    out["approved_at"] = a["approved_at"].isoformat() if a["approved_at"] else None
    out["image"] = {k: v for k, v in a["image"].items() if k != "content"} if a["image"] else None
    return out


def page(a: dict, image_src: str | None = None, intro: str = "") -> str:
    """The article as a page of its own: the search fields in the head, the
    schema.org data, the cover image (`image_src`, by default the file next
    to the page; "" for none) and the text with its sources."""
    e = html.escape
    img = a["image"]
    src = (img["file"] if img else None) if image_src is None else image_src
    head = ['<meta charset="utf-8">', '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{e(a['seo_title'])}</title>",
            f'<meta name="description" content="{e(a["meta_description"])}">',
            f'<meta property="og:title" content="{e(a["title"])}">',
            f'<meta property="og:description" content="{e(a["meta_description"])}">',
            '<meta property="og:type" content="article">']
    if img and img.get("url"):
        head.append(f'<meta property="og:image" content="{e(img["url"])}">')
    for item in a["jsonld"] or []:
        data = json.dumps(item, ensure_ascii=False).replace("</", "<\\/")
        head.append(f'<script type="application/ld+json">{data}</script>')
    body = [intro] if intro else []
    body.append(f"<h1>{e(a['title'])}</h1>")
    if a["subtitle"]:
        body.append(f'<p class="subtitle"><em>{e(a["subtitle"])}</em></p>')
    if src:
        body.append(f'<img src="{e(src)}" alt="{e(img["alt"])}" width="1344" height="768">')
    body.append(a["html"])
    style = ("body{max-width:720px;margin:2rem auto;padding:0 1rem;font:18px/1.6 Georgia,serif;color:#222}"
             "img{width:100%;height:auto}h1{line-height:1.2}.sources{font-size:.85em}")
    return (f'<!doctype html>\n<html lang="{e(a["language"] or "")}">\n<head>\n' + "\n".join(head)
            + f"\n<style>{style}</style>\n</head>\n<body>\n<article>\n" + "\n".join(body)
            + "\n</article>\n</body>\n</html>\n")


def markdown_file(a: dict) -> str:
    """Markdown with front matter, as the static site generators read it."""
    fm = {"title": a["title"], "description": a["meta_description"], "slug": a["slug"],
          "date": a["approved_at"].isoformat() if a["approved_at"] else None, "lang": a["language"],
          "keywords": [a["keyphrase"]] if a["keyphrase"] else [],
          "image": a["image"]["file"] if a["image"] else None,
          "image_alt": a["image"]["alt"] if a["image"] else None}
    lines = ["---"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fm.items() if v not in (None, [])]
    lines += ["---", ""]
    if a["subtitle"]:
        lines += [f"_{a['subtitle']}_", ""]
    return "\n".join(lines) + a["markdown"] + "\n"


def package(a: dict) -> tuple[str, bytes]:
    """(file name, ZIP) of the article: page, text, Markdown, image, search fields."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("article.html", page(a))
        z.writestr("body.html", a["html"] + "\n")
        z.writestr("article.md", markdown_file(a))
        z.writestr("meta.json", json.dumps(_meta(a), ensure_ascii=False, indent=1) + "\n")
        if a["image"]:
            z.writestr(a["image"]["file"], a["image"]["content"], compress_type=zipfile.ZIP_STORED)
    return f"{a['slug'] or 'article'}-{a['language'] or 'xx'}.zip", buf.getvalue()


# The email

def email_recipients(s) -> list[str]:
    return list(s.execute(select(m.recipients.c.address).where(
        m.recipients.c.channel == "email", m.recipients.c.enabled)).scalars())


def smtp_sender(settings, smtp_factory=smtplib.SMTP):
    """A function (to, subject, text, html, attachment) that sends one email
    to several people in Bcc, or None when SMTP is not configured."""
    if not settings.smtp_host:
        return None

    def send(to: list[str], subject: str, body: str, page_html: str, attachment: tuple[str, bytes] | None) -> None:
        msg = EmailMessage()
        sender = settings.mail_from or settings.smtp_user
        msg["Subject"], msg["From"], msg["To"] = subject, sender, sender
        msg["Bcc"] = ", ".join(to)
        msg.set_content(body)
        msg.add_alternative(page_html, subtype="html")
        if attachment:
            msg.add_attachment(attachment[1], maintype="application", subtype="zip", filename=attachment[0])
        with smtp_factory(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)

    return send


def email(db, tenant_id: str, draft_id: int, send, public_url: str = "") -> dict:
    """Send the approved article to the newsroom's email recipients, with the package."""
    with db.tenant(tenant_id) as s:
        a = article(s, draft_id, public_url)
        to = email_recipients(s)
        lang = s.execute(select(m.tenants.c.language).where(m.tenants.c.tenant_id == tenant_id)).scalar()
    if a is None or a["status"] != "approved":
        raise RuntimeError("the draft is not approved")
    if not to:
        return {"title": a["title"], "sent": 0}
    if send is None:
        raise RuntimeError("smtp is not configured: set EDITOR_SMTP_HOST on the server")
    name, data = package(a)
    intro = t("Articolo approvato per {site}, pronto da pubblicare. In allegato il pacchetto: la pagina "
              "(article.html), il testo da incollare nel CMS (body.html), il Markdown per i siti statici, "
              "l'immagine e i campi per Google (meta.json).", lang, site=a["site"] or "")
    fields = "\n".join(f"{label}: {value}" for label, value in (
        (t("Titolo per Google", lang), a["seo_title"]), (t("Descrizione per Google", lang), a["meta_description"]),
        (t("Indirizzo", lang), a["slug"]), (t("Frase chiave", lang), a["keyphrase"])) if value)
    body = f"{intro}\n\n{fields}\n\n# {a['title']}\n\n" + (f"_{a['subtitle']}_\n\n" if a["subtitle"] else "") + a["markdown"] + "\n"
    shown = page(a, image_src=(a["image"] or {}).get("url") or "",
                 intro=f"<p><em>{html.escape(intro)}</em></p><pre>{html.escape(fields)}</pre>")
    send(to, t("Articolo approvato: {title}", lang, title=a["title"]), body, shown, (name, data))
    return {"title": a["title"], "sent": len(to)}


# The feed

def feed_token(db, tenant_id: str, site_id: int, renew: bool = False) -> str:
    """The token of the site's feed, created the first time; `renew` gives a
    new one, and the old address stops working."""
    with db.tenant(tenant_id) as s:
        cur = s.execute(select(m.feeds.c.token).where(m.feeds.c.site_id == site_id)).scalar()
        if cur and not renew:
            return cur
        if s.execute(select(m.sites.c.id).where(m.sites.c.id == site_id)).first() is None:
            raise LookupError(site_id)
        token = secrets.token_urlsafe(24)
        s.execute(delete(m.feeds).where(m.feeds.c.site_id == site_id))
        s.execute(insert(m.feeds).values(tenant_id=tenant_id, site_id=site_id, token=token))
        return token


def current_token(s, site_id: int) -> str | None:
    return s.execute(select(m.feeds.c.token).where(m.feeds.c.site_id == site_id)).scalar()


def by_token(db, token: str):
    """(tenant_id, site_id) of the feed with this token; None if unknown."""
    if not token or len(token) > 64:
        return None
    with db.engine.connect() as conn:
        if db.sqlite:
            return conn.execute(text("SELECT tenant_id, site_id FROM editor.feeds WHERE token = :t"),
                                {"t": token}).first()
        return conn.execute(text("SELECT tenant_id, site_id FROM editor.feed_by_token(:t)"), {"t": token}).first()


def feed_articles(db, tenant_id: str, site_id: int, public_url: str, language: str | None = None) -> list[dict]:
    with db.tenant(tenant_id) as s:
        q = (select(m.drafts.c.id).where(m.drafts.c.site_id == site_id, m.drafts.c.status == "approved")
             .order_by(m.drafts.c.decided_at.desc(), m.drafts.c.id.desc()).limit(FEED_ITEMS))
        if language:
            q = q.where(m.drafts.c.language == language)
        return [a for a in (article(s, i, public_url) for i in s.execute(q).scalars()) if a]


def json_feed(arts: list[dict], site: str | None, url: str) -> dict:
    """JSON Feed 1.1 (jsonfeed.org); the fields for the CMS under `_redazia`."""
    items = []
    for a in arts:
        img = a["image"]
        items.append({k: v for k, v in {
            "id": f"redazia:{a['site']}:{a['id']}", "title": a["title"], "summary": a["meta_description"],
            "content_html": a["html"], "image": img["url"] if img else None,
            "date_published": a["approved_at"].isoformat() if a["approved_at"] else None,
            "language": a["language"], "tags": [a["keyphrase"]] if a["keyphrase"] else None,
            "_redazia": {"subtitle": a["subtitle"], "slug": a["slug"], "seo_title": a["seo_title"],
                         "meta_description": a["meta_description"], "keyphrase": a["keyphrase"],
                         "markdown": a["markdown"], "image_alt": img["alt"] if img else None,
                         "sources": a["sources"], "jsonld": a["jsonld"], "translation_of": a["translation_of"]},
        }.items() if v is not None})
    return {"version": "https://jsonfeed.org/version/1.1", "title": site or "Redazia",
            "home_page_url": f"https://{site}/" if site else None, "feed_url": url, "items": items}


def rss(arts: list[dict], site: str | None, url: str) -> str:
    """RSS 2.0 with the whole text (content:encoded) and the cover image."""
    e = html.escape
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
           'xmlns:media="http://search.yahoo.com/mrss/" xmlns:atom="http://www.w3.org/2005/Atom">',
           "<channel>", f"<title>{e(site or 'Redazia')}</title>", f"<link>{e(f'https://{site}/' if site else url)}</link>",
           f"<description>{e(t('Articoli approvati di {site}', None, site=site or ''))}</description>",
           f'<atom:link href="{e(url)}" rel="self" type="application/rss+xml"/>']
    for a in arts:
        img = a["image"]
        out.append("<item>")
        out.append(f"<title>{e(a['title'])}</title>")
        out.append(f'<guid isPermaLink="false">redazia:{e(a["site"] or "")}:{a["id"]}</guid>')
        if a["approved_at"]:
            when = a["approved_at"] if a["approved_at"].tzinfo else a["approved_at"].replace(tzinfo=UTC)
            out.append(f"<pubDate>{format_datetime(when)}</pubDate>")
        out.append(f"<description>{e(a['meta_description'])}</description>")
        if a["keyphrase"]:
            out.append(f"<category>{e(a['keyphrase'])}</category>")
        out.append("<content:encoded><![CDATA[" + a["html"].replace("]]>", "]]]]><![CDATA[>") + "]]></content:encoded>")
        if img and img["url"]:
            out.append(f'<enclosure url="{e(img["url"])}" length="{len(img["content"])}" type="{e(img["type"])}"/>')
            out.append(f'<media:content url="{e(img["url"])}" medium="image" type="{e(img["type"])}">'
                       f'<media:description>{e(img["alt"])}</media:description></media:content>')
        out.append("</item>")
    out += ["</channel>", "</rss>", ""]
    return "\n".join(out)
