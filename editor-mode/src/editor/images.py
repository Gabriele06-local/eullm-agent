"""Cover images for the articles.

The photos of the news an article comes from belong to their publishers and
are never reused. Each article gets an image of its own instead: the model
writes the prompt from the article (one short call, with the alternative
text in the article's language), FLUX by Black Forest Labs draws it on its
EU endpoint, and the image is kept here, since the provider deletes it after
ten minutes. The dashboard shows it on the draft; the sites get it at
publication (WordPress as the featured image, webhooks as a link), served by
an unguessable token.

Without `EDITOR_IMAGES_API_KEY` nothing of this runs and drafts have no
image. Every image is recorded in the usage like a model call, with its cost.
"""

from __future__ import annotations

import logging
import secrets
import time

import httpx
from sqlalchemy import delete, insert, select, text

from . import models as m
from . import usage
from .core_client import CoreError, extract_json
from .i18n import N_
from .progress import step

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 1344, 768  # about 16:9, one megapixel
CREDIT = 0.01  # USD: Black Forest Labs bills in credits of one cent


class ImageError(RuntimeError):
    pass


class Imager:
    """Black Forest Labs' API: submit, poll the URL it returns, download."""

    def __init__(self, key: str, url: str, model: str, http: httpx.Client | None = None,
                 poll: float = 0.5, wait: float = 120):
        self.key, self.url, self.model = key, url.rstrip("/"), model
        self.http = http or httpx.Client(timeout=30)
        self.poll, self.wait = poll, wait

    def draw(self, prompt: str) -> tuple[bytes, str, float | None]:
        """The image for `prompt`: its bytes, type and cost in USD (None: not given)."""
        headers = {"x-key": self.key, "accept": "application/json"}
        r = self.http.post(f"{self.url}/{self.model}", headers=headers, json={
            "prompt": prompt, "width": WIDTH, "height": HEIGHT, "output_format": "jpeg"})
        if r.status_code >= 400:
            raise ImageError(f"image service answered {r.status_code}: {r.text[:200]}")
        job = r.json()
        cost = job.get("cost")
        cost = float(cost) * CREDIT if isinstance(cost, (int, float)) else None
        polling = job.get("polling_url")
        if not polling:
            raise ImageError("image service gave no polling URL")
        until = time.monotonic() + self.wait
        while True:
            res = self.http.get(polling, headers=headers).json()
            status = res.get("status")
            if status == "Ready":
                break
            if status in ("Error", "Failed", "Content Moderated", "Request Moderated"):
                raise ImageError(f"image not made: {status}")
            if time.monotonic() > until:
                raise ImageError("image service did not finish in time")
            time.sleep(self.poll)
        img = self.http.get(res["result"]["sample"])
        img.raise_for_status()
        return img.content, img.headers.get("content-type", "image/jpeg").split(";")[0], cost


def from_settings(st) -> Imager | None:
    return Imager(st.images_api_key, st.images_url, st.images_model) if st.images_api_key else None


SYSTEM = (
    "You write the prompt for the cover image of a news article, for an image model. Describe one clear "
    "scene that illustrates the article's subject as an editorial photograph or a clean illustration, "
    "matching the site's tone. Never ask for text, letters, numbers, logos, brand marks, flags of real "
    "organisations or recognisable real people: show the subject through places, objects, technology and "
    "anonymous people. Wide 16:9 composition. Write the prompt in English, 40 to 80 words. Then write the "
    "image's alternative text for blind readers, in the article's language, one sentence. Reply with JSON "
    'only: {"prompt": "...", "alt": "..."}'
)


def describe(core, d, profile: dict) -> tuple[str, str]:
    """The image prompt and its alternative text, from the article. Without a
    model, or when it fails, a plain prompt from the title."""
    plain = (f"Editorial photograph illustrating: {d.title}. {d.subtitle or ''} No text, no logos, "
             "no recognisable people, wide 16:9 composition.")
    if core is None:
        return plain, d.title
    lead = d.body_md.split("\n## ", 1)[0][:1500]
    try:
        res = core.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": (
            f"Site: {profile.get('name')} ({profile.get('domain')}); tone: "
            f"{(profile.get('style') or {}).get('tone') or 'not determined'}\n"
            f"Article language: {d.language or profile.get('language') or 'unknown'}\n\n{lead}")}])
        value = extract_json(res.content)
    except (CoreError, ValueError) as e:
        log.info("image prompt by the model failed, using the title: %s", e)
        return plain, d.title
    prompt = " ".join(str((value or {}).get("prompt") or "").split()) if isinstance(value, dict) else ""
    alt = " ".join(str((value or {}).get("alt") or "").split()) if isinstance(value, dict) else ""
    return (prompt[:1500] or plain), (alt[:300] or d.title)


def make(db, tenant_id: str, draft_id: int, imager: Imager, core=None) -> str:
    """Draw the cover image of a draft, replacing the one it has; returns its token.
    A translation gets the image of its original."""
    with db.tenant(tenant_id) as s:
        d = s.execute(select(m.drafts).where(m.drafts.c.id == draft_id)).first()
        if d is None:
            raise LookupError(draft_id)
        profile = s.execute(select(m.editorial_profiles.c.body).join(
            m.proposals, m.proposals.c.profile_id == m.editorial_profiles.c.id)
            .where(m.proposals.c.id == d.proposal_id)).scalar() or {}
    step(N_("Il modello descrive l'immagine"))
    with usage.tagged(db, tenant_id, "image", site_id=d.site_id, ref=f"draft:{draft_id}"):
        prompt, alt = describe(core, d, profile)
    step(N_("Disegno l'immagine di copertina"))
    started = time.monotonic()
    with usage.tagged(db, tenant_id, "image", site_id=d.site_id, ref=f"draft:{draft_id}"):
        try:
            content, mime, cost = imager.draw(prompt)
        except (ImageError, httpx.HTTPError) as e:
            usage.record(imager.model, None, None, int((time.monotonic() - started) * 1000), str(e))
            raise
        usage.record(imager.model, None, cost, int((time.monotonic() - started) * 1000))
    token = secrets.token_urlsafe(24)
    with db.tenant(tenant_id) as s:
        s.execute(delete(m.draft_images).where(m.draft_images.c.draft_id == draft_id))
        s.execute(insert(m.draft_images).values(
            tenant_id=tenant_id, draft_id=draft_id, token=token, prompt=prompt, alt=alt, model=imager.model,
            mime=mime, width=WIDTH, height=HEIGHT, content=content))
    return token


def copy(s, tenant_id: str, from_draft: int, to_draft: int, alt: str | None = None) -> None:
    """The original's image for one of its translations, with the alternative
    text in the translation's language."""
    img = s.execute(select(m.draft_images).where(m.draft_images.c.draft_id == from_draft)).first()
    if img is None:
        return
    s.execute(delete(m.draft_images).where(m.draft_images.c.draft_id == to_draft))
    s.execute(insert(m.draft_images).values(
        tenant_id=tenant_id, draft_id=to_draft, token=secrets.token_urlsafe(24), prompt=img.prompt,
        alt=alt or img.alt, model=img.model, mime=img.mime, width=img.width, height=img.height, content=img.content))


def of(s, draft_id: int):
    """The draft's image, without its bytes; None when it has none."""
    i = m.draft_images.c
    return s.execute(select(i.token, i.alt, i.prompt, i.model, i.mime, i.width, i.height, i.created_at)
                     .where(i.draft_id == draft_id)).first()


def content(s, draft_id: int):
    return s.execute(select(m.draft_images.c.mime, m.draft_images.c.content, m.draft_images.c.alt,
                            m.draft_images.c.token)
                     .where(m.draft_images.c.draft_id == draft_id)).first()


def by_token(db, token: str):
    """(mime, bytes) of the image with this token, whatever the tenant; None if unknown."""
    if not token or len(token) > 64:
        return None
    with db.engine.connect() as conn:
        if db.sqlite:
            return conn.execute(text("SELECT mime, content FROM editor.draft_images WHERE token = :t"),
                                {"t": token}).first()
        return conn.execute(text("SELECT mime, content FROM editor.image_by_token(:t)"), {"t": token}).first()


def remove(db, tenant_id: str, draft_id: int) -> bool:
    with db.tenant(tenant_id) as s:
        return bool(s.execute(delete(m.draft_images).where(m.draft_images.c.draft_id == draft_id)).rowcount)
