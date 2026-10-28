"""Cover images for the articles.

The photos of the news an article comes from belong to their publishers and
are never reused. Each article gets an image of its own instead: the model
writes the prompt from the article (one short call, with the alternative
text in the article's language), an image model draws it, and the image is
kept here. The dashboard shows it on the draft; the sites get it at
publication (WordPress as the featured image, GitHub in the repository,
webhooks as a link), served by an unguessable token.

Two services: OpenRouter's image API (one key for many models, the default
openai/gpt-image-2 writes readable words) and Black Forest Labs' FLUX on its
EU endpoint. A key starting with sk-or- is OpenRouter's. Each site picks the
cover's style: "impact" (made to be clicked: bold subject, dramatic light,
strong colours, readable as a thumbnail, optionally a short headline drawn
in) or "editorial" (a calm news photograph).

Without `EDITOR_IMAGES_API_KEY` nothing of this runs and drafts have no
image. Every image is recorded in the usage like a model call, with its cost.
"""

from __future__ import annotations

import base64
import logging
import secrets
import struct
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

    name = "bfl"

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


class OpenRouterImager:
    """OpenRouter's image API: one request, the image comes back in base64
    with its cost in USD."""

    name = "openrouter"

    def __init__(self, key: str, url: str, model: str, quality: str = "medium",
                 http: httpx.Client | None = None):
        self.key, self.url, self.model = key, url.rstrip("/"), model
        self.quality = quality if quality in ("low", "medium", "high", "auto") else "medium"
        self.http = http or httpx.Client(timeout=180)

    def draw(self, prompt: str) -> tuple[bytes, str, float | None]:
        r = self.http.post(f"{self.url}/images", headers={"Authorization": f"Bearer {self.key}"}, json={
            "model": self.model, "prompt": prompt, "aspect_ratio": "16:9", "resolution": "1K",
            "quality": self.quality, "output_format": "jpeg", "n": 1})
        if r.status_code >= 400:
            raise ImageError(f"image service answered {r.status_code}: {r.text[:200]}")
        body = r.json()
        data = (body.get("data") or [{}])[0]
        if not data.get("b64_json"):
            raise ImageError("image service returned no image")
        try:
            content = base64.b64decode(data["b64_json"])
        except ValueError as e:
            raise ImageError("image service returned a broken image") from e
        cost = (body.get("usage") or {}).get("cost")
        return content, data.get("media_type") or _mime(content), float(cost) if isinstance(cost, (int, float)) else None


OPENROUTER = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = "openai/gpt-image-2"
BFL = "https://api.eu.bfl.ai/v1"
BFL_MODEL = "flux-2-pro"


def from_settings(st):
    """The image service of the settings, or None without a key. The
    provider follows the key unless EDITOR_IMAGES_PROVIDER names it; a FLUX
    model name (no slash) is not an OpenRouter model, so it is replaced by
    the default there."""
    key = st.images_api_key
    if not key:
        return None
    provider = st.images_provider or ("openrouter" if key.startswith("sk-or-") else "bfl")
    if provider == "openrouter":
        model = st.images_model if "/" in (st.images_model or "") else OPENROUTER_MODEL
        return OpenRouterImager(key, st.images_url or OPENROUTER, model, st.images_quality)
    return Imager(key, st.images_url or BFL, st.images_model or BFL_MODEL)


def _mime(content: bytes) -> str:
    return "image/png" if content.startswith(b"\x89PNG") else "image/webp" if content[8:12] == b"WEBP" else "image/jpeg"


def size_of(content: bytes) -> tuple[int, int]:
    """Width and height read from a PNG or JPEG header; the nominal size when
    the header is not understood."""
    try:
        if content.startswith(b"\x89PNG") and len(content) >= 24:
            return struct.unpack(">II", content[16:24])
        if content.startswith(b"\xff\xd8"):
            i = 2
            while i + 9 < len(content):
                if content[i] != 0xFF:
                    i += 1
                    continue
                marker = content[i + 1]
                length = struct.unpack(">H", content[i + 2:i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack(">HH", content[i + 5:i + 9])
                    return w, h
                i += 2 + length
    except struct.error:
        pass
    return WIDTH, HEIGHT


RULES = (
    "Never ask for logos, brand marks, flags of real organisations or recognisable real people: show the "
    "subject through places, objects, technology and anonymous people. Wide 16:9 composition. Write the "
    "prompt in English, 40 to 90 words. Then write the image's alternative text for blind readers, in the "
    "article's language, one sentence. Reply with JSON only: "
)

SYSTEM = (
    "You write the prompt for the cover image of a news article, for an image model. Describe one clear "
    "scene that illustrates the article's subject as an editorial photograph or a clean illustration, "
    "matching the site's tone. Never ask for text, letters or numbers. " + RULES + '{"prompt": "...", "alt": "..."}'
)

IMPACT = (
    "You write the prompt for the cover image of a news article, for an image model. The cover must make "
    "people stop scrolling and click, on the blog's list, in search and on social networks, while staying "
    "true to the article: never show something the article does not say. Pick the single most striking "
    "subject of the story and make it the hero of the frame: bold, close and centred, with dramatic "
    "cinematic lighting, strong contrast, a saturated palette with one accent colour, a dynamic angle and a "
    "clean background, so that it still reads as a small thumbnail. A sense of urgency, scale or surprise "
    "that matches the news. "
)

NO_TEXT = "Never ask for text, letters or numbers in the image. " + RULES + '{"prompt": "...", "alt": "..."}'

HEADLINE = (
    "Also write a headline of 2 to 4 words in the article's language that sums up the hook of the news, "
    "and ask the image model to draw it exactly as written, in large bold sans-serif letters with strong "
    "contrast, on a clear area of the image; no other text. " + RULES
    + '{"prompt": "...", "headline": "...", "alt": "..."}'
)

STYLES = ("impact", "editorial")


def style_of(profile: dict) -> tuple[str, bool]:
    """The site's cover style and whether a headline is drawn in."""
    st = (profile or {}).get("settings") or {}
    style = st.get("cover_style") if st.get("cover_style") in STYLES else "impact"
    return style, bool(st.get("cover_text")) and style == "impact"


def system_for(profile: dict) -> str:
    style, words = style_of(profile)
    if style == "editorial":
        return SYSTEM
    return IMPACT + (HEADLINE if words else NO_TEXT)


def describe(core, d, profile: dict) -> tuple[str, str]:
    """The image prompt and its alternative text, from the article. Without a
    model, or when it fails, a plain prompt from the title."""
    style, words = style_of(profile)
    if style == "impact":
        plain = (f"Striking cover image for a news story: {d.title}. {d.subtitle or ''} One bold subject, "
                 "dramatic cinematic lighting, strong contrast, saturated colours, clean background, readable "
                 "as a thumbnail. No text, no logos, no recognisable people, wide 16:9 composition.")
    else:
        plain = (f"Editorial photograph illustrating: {d.title}. {d.subtitle or ''} No text, no logos, "
                 "no recognisable people, wide 16:9 composition.")
    if core is None:
        return plain, d.title
    lead = d.body_md.split("\n## ", 1)[0][:1500]
    try:
        res = core.chat([{"role": "system", "content": system_for(profile)}, {"role": "user", "content": (
            f"Site: {profile.get('name')} ({profile.get('domain')}); tone: "
            f"{(profile.get('style') or {}).get('tone') or 'not determined'}\n"
            f"Article language: {d.language or profile.get('language') or 'unknown'}\n"
            f"Title: {d.title}\n\n{lead}")}])
        value = extract_json(res.content)
    except (CoreError, ValueError) as e:
        log.info("image prompt by the model failed, using the title: %s", e)
        return plain, d.title
    value = value if isinstance(value, dict) else {}
    prompt = " ".join(str(value.get("prompt") or "").split())
    alt = " ".join(str(value.get("alt") or "").split())
    headline = " ".join(str(value.get("headline") or "").split())[:60]
    if prompt and words and headline and headline.lower() not in prompt.lower():
        headline = headline.replace('"', "")
        prompt += f' Large bold headline text, spelled exactly: "{headline}".'
    return (prompt[:1800] or plain), (alt[:300] or d.title)


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
        # The cover style is the site's setting of today, not of the proposal's day.
        current = s.execute(select(m.editorial_profiles.c.body).where(
            m.editorial_profiles.c.site_id == d.site_id, m.editorial_profiles.c.status == "approved")).scalar()
        if current:
            profile = {**profile, "settings": {**(profile.get("settings") or {}), **(current.get("settings") or {})}}
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
    width, height = size_of(content)
    with db.tenant(tenant_id) as s:
        s.execute(delete(m.draft_images).where(m.draft_images.c.draft_id == draft_id))
        s.execute(insert(m.draft_images).values(
            tenant_id=tenant_id, draft_id=draft_id, token=token, prompt=prompt, alt=alt, model=imager.model,
            mime=mime, width=width, height=height, content=content))
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
