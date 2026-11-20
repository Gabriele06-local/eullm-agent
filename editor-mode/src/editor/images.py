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
cover's style: "impact" (made to be clicked: one strong subject, natural
contrast, readable as a thumbnail, optionally a short headline drawn in) or
"editorial" (a calm news photograph). Both are photographs, never renders.

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
from sqlalchemy import case, delete, insert, select, text

from . import models as m
from . import credits, usage
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
        return img.content, _mime(img.content), cost


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
        body = {"model": self.model, "prompt": prompt, "aspect_ratio": "16:9", "resolution": "1K",
                "output_format": "jpeg", "n": 1}
        if self.model.startswith("openai/"):  # quality is OpenAI's: FLUX and others refuse it
            body["quality"] = self.quality
        r = self.http.post(f"{self.url}/images", headers={"Authorization": f"Bearer {self.key}"}, json=body)
        if r.status_code == 400 and "support" in r.text.lower():
            # A model that refuses one of the options: once more with the prompt
            # and the shape only, then with the prompt alone.
            for keep in (("model", "prompt", "n", "aspect_ratio"), ("model", "prompt", "n")):
                r = self.http.post(f"{self.url}/images", headers={"Authorization": f"Bearer {self.key}"},
                                   json={k: v for k, v in body.items() if k in keep})
                if not (r.status_code == 400 and "support" in r.text.lower()):
                    break
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
        return content, _mime(content), float(cost) if isinstance(cost, (int, float)) else None


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


COVER_SIDE = 1600  # the longest side a cover needs on a page (1360 x 768 from the generators)


def for_site(content: bytes, mime: str) -> tuple[bytes, str]:
    """The cover as a site should keep it: at most COVER_SIDE pixels on its
    longest side, a progressive JPEG at quality 78 (a PNG when it has
    transparency). Measured on aitoday.it's covers: 450 KB down to 145 KB,
    no visible difference. The same image always gives the same bytes, so a
    translation finds its original's file. The original when it cannot be
    read, or when the new one would not be at least a tenth smaller."""
    import io

    try:
        from PIL import Image

        with Image.open(io.BytesIO(content)) as im:
            im.load()
            icc = im.info.get("icc_profile")
            if max(im.size) > COVER_SIDE:
                im.thumbnail((COVER_SIDE, COVER_SIDE), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                im.save(out, "PNG", optimize=True)
                new_mime = "image/png"
            else:
                im.convert("RGB").save(out, "JPEG", quality=78, optimize=True, progressive=True,
                                       **({"icc_profile": icc} if icc else {}))
                new_mime = "image/jpeg"
    except Exception:  # not an image Pillow reads: sent as it came
        return content, mime
    data = out.getvalue()
    return (data, new_mime) if len(data) < len(content) * 0.9 else (content, mime)


def mime_of(content: bytes) -> str:
    """An image type read from the file's first bytes; never anything else."""
    return _mime(content)


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


# Photographs, not renders (Francesco, 2026-11-04: covers "a bit too
# cartoon"). Image models drift to 3D, glow and fire trails when asked for
# drama, and when asked for a view nobody could have shot (a satellite
# burning in the sky): the scene is one a photographer could take.
PHOTO = (
    "The image must look like a real photograph by a professional photojournalist, never an illustration: "
    "choose a scene a photographer could actually have shot for this story (the place, the people at work, "
    "the object itself on the ground or in a lab, the room where it happened, a detail that tells it), not "
    "an imagined view of the event. Say it in the prompt: documentary news photograph, shot on a full-frame "
    "camera with a 35mm or 50mm lens, natural or available light, true-to-life colours, real textures and "
    "small imperfections, shallow depth of field. End the prompt with: no illustration, no 3D render, no "
    "CGI, no cartoon, no concept art, no glow effects, no oversaturated colours. "
)

RULES = (
    "Never ask for logos, brand marks, flags of real organisations or recognisable real people: show the "
    "subject through places, objects, technology and anonymous people, seen from behind, at a distance or "
    "with faces out of focus. Wide 16:9 composition. Write the "
    "prompt in English, 40 to 90 words. Then write the image's alternative text for blind readers, in the "
    "article's language, one sentence. Reply with JSON only: "
)

SYSTEM = (
    "You write the prompt for the cover image of a news article, for an image model. Describe one clear "
    "scene that illustrates the article's subject as a calm editorial photograph, matching the site's tone. "
    + PHOTO + "Never ask for text, letters or numbers. " + RULES + '{"prompt": "...", "alt": "..."}'
)

IMPACT = (
    "You write the prompt for the cover image of a news article, for an image model. The cover must make "
    "people stop scrolling and click, on the blog's list, in search and on social networks, while staying "
    "true to the article: never show something the article does not say. Pick the single most striking "
    "subject of the story a photographer could capture and make it the hero of the frame: close, sharp and "
    "well composed, with strong natural contrast (low sun, a window, street or stage light) and a clean, "
    "uncluttered background, so that it still reads as a small thumbnail. A sense of urgency, scale or "
    "surprise that matches the news, as in the best front-page photographs. " + PHOTO
)

NO_TEXT = "Never ask for text, letters or numbers in the image. " + RULES + '{"prompt": "...", "alt": "..."}'

HEADLINE = (
    "Also write a headline of 2 to 4 words in the article's language that sums up the hook of the news, "
    "and ask the image model to draw it exactly as written, in large bold sans-serif letters with strong "
    "contrast, on a clear area of the image; no other text. " + RULES
    + '{"prompt": "...", "headline": "...", "alt": "..."}'
)

# The photograph of a recipe (drafts.OWN_RECIPE): the finished dish, as the
# site's own food photographer would shoot it.
FOOD = (
    "You write the prompt for the photograph of a recipe, for an image model. Show the finished dish exactly "
    "as the recipe makes it (its ingredients, colours and texture, nothing it does not have), plated and ready "
    "to serve, as a professional food photographer shoots it for a cookery magazine: natural window light from "
    "the side, a 45-degree or overhead angle, shallow depth of field, a simple table, linen or board and at most "
    "two ingredients of the recipe as props, true-to-life appetising colours, real textures and small crumbs or "
    "drops. No people, no hands, no text, no logos, no packaging. End the prompt with: no illustration, no 3D "
    "render, no CGI, no oversaturated colours. " + RULES + '{"prompt": "...", "alt": "..."}'
)

STYLES = ("impact", "editorial")

# Covers that look all alike (Francesco 2026-11-14: «tavoli, computer, gilet gialli, sale riunioni a
# gogo, sempre stesso legno, senza persone»): the stock scenes are named and kept out, the site's
# last covers are shown so their scene is not taken again, and each cover gets its own kind of shot.
CLICHES = (
    "Avoid the stock scenes of news covers unless the article is really about them: empty meeting or "
    "conference rooms, long wooden tables with chairs, wooden desks with a laptop or a computer screen, "
    "hands typing, workers in hi-vis vests or hard hats, generic offices, a podium with microphones, a "
    "smartphone held in a hand. Show what is specific to this story instead: its place (a street, a "
    "landscape, a building, a lab, a port, a stadium, a field), its object or its people in action, seen "
    "from behind, at a distance or out of focus. Use colour, weather and time of day that suit the story. "
)
SHOTS = (
    "a wide establishing shot of the place where the story happens, outdoors",
    "a close-up detail of the story's key object, macro lens, shallow depth of field",
    "anonymous people in action, seen from behind or at a distance, in their real setting",
    "an aerial or high-angle view of the place",
    "a street-level documentary scene with movement and depth",
    "a symbolic still life of one object that stands for the story, on a background that is not a desk",
    "a night or blue-hour scene with artificial light",
)
RECENT_COVERS = 12


def is_recipe(d) -> bool:
    return bool(((getattr(d, "seo", None) or {}).get("recipe") or {}).get("own"))


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


def describe(core, d, profile: dict, recent: list[str] | None = None) -> tuple[str, str]:
    """The image prompt and its alternative text, from the article. Without a
    model, or when it fails, a plain prompt from the title."""
    style, words = style_of(profile)
    if is_recipe(d):
        style, words = "food", False
        plain = (f"Professional food photograph of the finished dish: {d.title}. Plated and ready to serve, natural "
                 "window light, 45-degree angle, shallow depth of field, simple table, true-to-life colours. No "
                 "people, no text, no logos, wide 16:9 composition, no illustration, no 3D render, no CGI.")
    elif style == "impact":
        plain = (f"Documentary news photograph for the story: {d.title}. {d.subtitle or ''} One strong subject, "
                 "close and sharp, natural light with strong contrast, clean background, readable as a "
                 "thumbnail, shot on a full-frame camera, true-to-life colours. No text, no logos, no "
                 "recognisable people, wide 16:9 composition, no illustration, no 3D render, no CGI.")
    else:
        plain = (f"Editorial news photograph illustrating: {d.title}. {d.subtitle or ''} Natural light, "
                 "true-to-life colours. No text, no logos, no recognisable people, wide 16:9 composition, "
                 "no illustration, no 3D render.")
    if core is None:
        return plain, d.title
    # A recipe's cover shows the dish: its ingredients and steps are the description.
    lead = d.body_md[:3000] if style == "food" else d.body_md.split("\n## ", 1)[0][:1500]
    variety = ""
    if style != "food":
        shot = SHOTS[(d.id or 0) % len(SHOTS)]
        variety = (f"\nShot for this cover, unless it cannot show the story: {shot}."
                   + ("\nThe site's last covers (do not repeat their scene, setting, props or colours):\n"
                      + "\n".join(f"- {x[:220]}" for x in recent) if recent else ""))
    try:
        res = core.chat([{"role": "system", "content": FOOD if style == "food" else CLICHES + system_for(profile)},
                         {"role": "user", "content": (
            f"Site: {profile.get('name')} ({profile.get('domain')}); tone: "
            f"{(profile.get('style') or {}).get('tone') or 'not determined'}\n"
            f"Article language: {d.language or profile.get('language') or 'unknown'}\n"
            f"Title: {d.title}\n\n{lead}{variety}")}])
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
        # The first cover and the free redraws are included; then credits.
        credits.need(s, credits.cover_cost(s, draft_id))
        recent = list(s.execute(select(m.draft_images.c.prompt).join(
            m.drafts, m.drafts.c.id == m.draft_images.c.draft_id).where(
            m.drafts.c.site_id == d.site_id, m.drafts.c.id != draft_id, m.drafts.c.translation_of.is_(None))
            .order_by(m.draft_images.c.created_at.desc()).limit(RECENT_COVERS)).scalars())
    step(N_("Il modello descrive l'immagine"))
    with usage.tagged(db, tenant_id, "image", site_id=d.site_id, ref=f"draft:{draft_id}"):
        prompt, alt = describe(core, d, profile, recent)
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
        credits.charge_cover(s, draft_id)
        s.execute(delete(m.draft_images).where(m.draft_images.c.draft_id == draft_id))
        s.execute(insert(m.draft_images).values(
            tenant_id=tenant_id, draft_id=draft_id, token=token, prompt=prompt, alt=alt, model=imager.model,
            mime=mime, width=width, height=height, content=content))
    return token


def copy(s, tenant_id: str, from_draft: int, to_draft: int, alt: str | None = None) -> None:
    """The original's image for one of its translations, with the alternative
    text in the translation's language. The bytes are not copied: the
    translation points at the original's image (`same_as`), so a site in
    five languages keeps one image per article, not five."""
    img = s.execute(select(m.draft_images).where(m.draft_images.c.draft_id == from_draft)).first()
    if img is None:
        return
    s.execute(delete(m.draft_images).where(m.draft_images.c.draft_id == to_draft))
    s.execute(insert(m.draft_images).values(
        tenant_id=tenant_id, draft_id=to_draft, token=secrets.token_urlsafe(24), prompt=img.prompt,
        alt=alt or img.alt, model=img.model, mime=img.mime, width=img.width, height=img.height,
        content=b"", same_as=img.same_as if img.same_as is not None else from_draft))


def _with_bytes():
    """draft_images with the bytes of the image it points at, if any."""
    i, o = m.draft_images, m.draft_images.alias("orig")
    content = case((i.c.same_as.is_not(None) & o.c.content.is_not(None), o.c.content), else_=i.c.content)
    return i, o, content


def of(s, draft_id: int):
    """The draft's image, without its bytes; None when it has none."""
    i = m.draft_images.c
    return s.execute(select(i.token, i.alt, i.prompt, i.model, i.mime, i.width, i.height, i.created_at)
                     .where(i.draft_id == draft_id)).first()


def content(s, draft_id: int):
    """mime, content (the bytes, the original's for a translation), alt, token, created_at."""
    i, o, data = _with_bytes()
    return s.execute(select(i.c.mime, data.label("content"), i.c.alt, i.c.token, i.c.created_at)
                     .select_from(i.outerjoin(o, o.c.draft_id == i.c.same_as))
                     .where(i.c.draft_id == draft_id)).first()


def by_token(db, token: str):
    """(mime, bytes) of the image with this token, whatever the tenant; None if unknown."""
    if not token or len(token) > 64:
        return None
    with db.engine.connect() as conn:
        if db.sqlite:
            return conn.execute(text(
                "SELECT i.mime, CASE WHEN i.same_as IS NOT NULL AND o.content IS NOT NULL THEN o.content "
                "ELSE i.content END AS content FROM editor.draft_images i "
                "LEFT JOIN editor.draft_images o ON o.draft_id = i.same_as WHERE i.token = :t"), {"t": token}).first()
        return conn.execute(text("SELECT mime, content FROM editor.image_by_token(:t)"), {"t": token}).first()


def remove(db, tenant_id: str, draft_id: int) -> bool:
    with db.tenant(tenant_id) as s:
        return bool(s.execute(delete(m.draft_images).where(m.draft_images.c.draft_id == draft_id)).rowcount)
