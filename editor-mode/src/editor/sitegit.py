"""Publishing into the repository a static site is deployed from (Vercel,
Netlify, GitHub Pages), as a person of the site would.

Every blog has its own shape: Astro wants `pubDate` and `heroImage`, another
site `date` and `cover`, one keeps each post in a folder with its picture,
another in `.mdx`. Before writing, the newest posts in the articles folder
are read and the new one copies them: the front matter's keys, order and
date format, values that are the same in every post (author, layout), the
file type and the place of the image. The blog's list then shows the new
article and its thumbnail like the others.

What a publication changes goes in one commit: the article, its cover, the
site's `llms.txt` (a line for the article) and a `sitemap.xml` kept as a
file (a generated sitemap updates itself at the build). One commit is one
deploy. In "publish" mode the commit goes on a branch with a pull request;
the site's checks (the preview deploy of Vercel or Netlify) must pass before
it is merged, so a post that would break the build never reaches the
site in production. Without checks on the repository it is merged at once.

Only top-level `key: value` front matter in YAML is read (`---` fences);
anything else (TOML, no posts yet) gets the plain front matter.
"""

from __future__ import annotations

import base64
import json
import posixpath
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote

POST_EXT = (".md", ".mdx", ".json")
SAMPLES = 8  # posts read to learn the format

ROLES = {
    "title": ("title",),
    "description": ("description", "excerpt", "summary", "metadescription", "meta_description", "subtitle"),
    "date": ("pubdate", "date", "publishdate", "published_at", "publishedat", "pubdatetime", "published_date",
             "created", "createdat", "created_at"),
    "image": ("heroimage", "image", "cover", "coverimage", "cover_image", "thumbnail", "featuredimage",
              "featured_image", "ogimage", "og_image", "banner", "img", "hero"),
    "image_alt": ("heroimagealt", "imagealt", "image_alt", "coveralt", "cover_alt", "alt", "thumbnailalt"),
    "tags": ("tags", "keywords"),
    "slug": ("slug",),
    "lang": ("lang", "language", "locale"),
    "section": ("section", "category", "rubrica"),
}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --- front matter ---------------------------------------------------------------

def split_front_matter(text: str) -> tuple[str | None, str]:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---", 3)
    if end < 0:
        return None, text
    rest = text[end + 4:]
    return text[4:end + 1], rest[1:] if rest.startswith("\n") else rest


def parse_front_matter(fm: str) -> list[tuple[str, str]]:
    """Top-level keys with their raw value, continuation lines included."""
    out: list[tuple[str, str]] = []
    for line in fm.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^([A-Za-z_][\w-]*)\s*:(.*)$", line)
        if match and not line[0].isspace():
            out.append((match.group(1), match.group(2).strip()))
        elif out:
            key, raw = out[-1]
            out[-1] = (key, f"{raw}\n{line}")
    return out


def scalar(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def nested(raw: str) -> dict[str, str]:
    """`key:` followed by indented `sub: value` lines."""
    if not raw.startswith("\n"):
        return {}
    out = {}
    for line in raw.split("\n")[1:]:
        match = re.match(r"^\s+([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if match:
            out[match.group(1)] = scalar(match.group(2))
    return out


def _role(key: str) -> str | None:
    k = key.lower()
    for role, names in ROLES.items():
        if k in names:
            return role
    return None


def _date_style(raw: str) -> tuple[str, str]:
    value, quote_ = scalar(raw), (raw.strip()[:1] if raw.strip()[:1] in "\"'" else "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return "date", quote_
    if re.fullmatch(r"[A-Z][a-z]{2} \d{2} \d{4}", value):
        return "human", quote_ or "'"
    return "iso", quote_


def parse_date(raw: str) -> datetime | None:
    value = scalar(raw)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%b %d %Y", "%B %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


# --- the blog's format ------------------------------------------------------------

@dataclass
class Post:
    path: str  # file in the repository
    keys: list[tuple[str, str]]
    slug: str
    data: dict | None = None  # a post kept as JSON (a Next.js site's content/*.json), whole


@dataclass
class Format:
    ext: str = ".md"
    folder: bool = False  # each post in a folder of its own, as index.<ext>
    keys: list[str] = field(default_factory=list)
    roles: dict[str, str] = field(default_factory=dict)  # role -> key
    constants: dict[str, str] = field(default_factory=dict)  # key -> raw value, same in every post
    date: tuple[str, str] = ("iso", "")
    image_sub: str | None = None  # image: {src|url: ..., alt: ...}
    image_alt_sub: str | None = None
    image_mode: str = "public"  # public: an address on the site; relative: a path from the post
    image_dir: str | None = None  # where the cover goes, for relative images
    image_sample: str | None = None  # the newest post's image, as written
    samples: list[Post] = field(default_factory=list)
    learned: bool = False
    data: dict | None = None  # the newest post, when the posts are JSON files


def learn(posts: list[Post]) -> Format:
    """The format of the newest post, with the values all posts share."""
    fmt = Format()
    if not posts:
        return fmt
    dated = []
    for p in posts:
        d = next((parse_date(raw) for k, raw in p.keys if _role(k) == "date"), None)
        dated.append(((d.replace(tzinfo=None) if d else datetime.min), p))
    dated.sort(key=lambda x: x[0], reverse=True)
    newest = dated[0][1]
    fmt.samples = [p for _, p in dated]
    fmt.learned = True
    fmt.ext = posixpath.splitext(newest.path)[1] or ".md"
    fmt.data = newest.data
    fmt.folder = posixpath.basename(newest.path).startswith("index.")
    fmt.keys = [k for k, _ in newest.keys]
    for key, raw in newest.keys:
        role = _role(key)
        if role and role not in fmt.roles:
            fmt.roles[role] = key
            if role == "date":
                fmt.date = _date_style(raw)
            if role == "image":
                sub = nested(raw)
                if sub:
                    fmt.image_sub = next((k for k in sub if k.lower() in ("src", "url", "path", "image")), None)
                    fmt.image_alt_sub = next((k for k in sub if k.lower() == "alt"), None)
                    value = sub.get(fmt.image_sub or "", "")
                else:
                    value = scalar(raw)
                fmt.image_sample = value or None
                if value and not value.startswith(("/", "http://", "https://")):
                    fmt.image_mode = "relative"
                    fmt.image_dir = posixpath.dirname(posixpath.normpath(
                        posixpath.join(posixpath.dirname(newest.path), value)))
            continue
        if key.lower() in ("draft", "published", "publish") and scalar(raw).lower() in ("true", "false"):
            continue
        values = [dict(p.keys).get(key) for p in posts]
        if len(posts) > 1 and all(v == raw for v in values):
            fmt.constants[key] = raw
        elif len(posts) == 1 and key.lower() in ("author", "authors", "layout", "type", "template"):
            fmt.constants[key] = raw
    return fmt


def format_date(when: datetime, style: tuple[str, str]) -> str:
    kind, q = style
    if kind == "date":
        value = when.strftime("%Y-%m-%d")
    elif kind == "human":
        value = f"{MONTHS[when.month - 1]} {when.day:02d} {when.year}"
    else:
        value = when.isoformat()
    return f"{q}{value}{q}"


def _q(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def front_matter(fmt: Format, payload: dict, slug: str, when: datetime, image_ref: str | None,
                 image_alt: str | None) -> str:
    """The new post's front matter, in the shape of the blog's posts."""
    description = payload.get("meta_description") or payload.get("subtitle")
    tags = [payload["keyphrase"]] if payload.get("keyphrase") else []
    if not fmt.learned:
        fm = {"title": payload["title"], "description": description, "slug": slug, "date": when.isoformat(),
              "lang": payload.get("language"), "section": payload.get("section"), "keywords": tags,
              "image": image_ref, "image_alt": image_alt, "translation_of": payload.get("translation_of"),
              "recipe": payload.get("recipe")}
        return "---\n" + "\n".join(f"{k}: {_q(v)}" for k, v in fm.items() if v not in (None, "")) + "\n---\n"
    values = {"title": payload["title"], "description": description, "slug": slug,
              "lang": payload.get("language"), "section": payload.get("section"), "tags": tags,
              "image_alt": image_alt}
    lines = []
    for key in fmt.keys:
        role = _role(key) if fmt.roles.get(_role(key) or "") == key else None
        if role == "date":
            lines.append(f"{key}: {format_date(when, fmt.date)}")
        elif role == "image":
            if not image_ref:
                continue
            if fmt.image_sub:
                lines.append(f"{key}:\n  {fmt.image_sub}: {_q(image_ref)}")
                if fmt.image_alt_sub and image_alt:
                    lines.append(f"  {fmt.image_alt_sub}: {_q(image_alt)}")
            else:
                lines.append(f"{key}: {_q(image_ref)}")
        elif role == "tags":
            if tags:
                lines.append(f"{key}: {_q(tags)}")
        elif role == "image_alt":
            if image_ref and image_alt:
                lines.append(f"{key}: {_q(image_alt)}")
        elif role:
            if values.get(role) not in (None, ""):
                lines.append(f"{key}: {_q(values[role])}")
            elif key in fmt.constants:  # a category every post shares, when the article has none
                lines.append(f"{key}: {fmt.constants[key]}")
        elif key.lower() == "draft":
            lines.append(f"{key}: false")
        elif key.lower() in ("published", "publish"):
            lines.append(f"{key}: true")
        elif key in fmt.constants:
            lines.append(f"{key}: {fmt.constants[key]}")
    if "title" not in fmt.roles:
        lines.insert(0, f"title: {_q(payload['title'])}")
    # The original's slug, for the site's language switch: a key the blog's
    # posts may not have yet, added for translations.
    if payload.get("translation_of"):
        lines.append(f"translation_of: {_q(payload['translation_of'])}")
    # A recipe's card (ingredients, steps, servings, times), for the site's schema.org Recipe: JSON is YAML.
    if payload.get("recipe"):
        lines.append(f"recipe: {_q(payload['recipe'])}")
    return "---\n" + "\n".join(lines) + "\n---\n"


def mdx_safe(md: str) -> str:
    """Markdown that MDX also reads: braces and angle brackets outside code
    would be taken for expressions and components."""
    out, fence = [], False
    for line in md.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
        elif not fence:
            parts = re.split(r"(`[^`]*`)", line)
            line = "".join(p if p.startswith("`") else p.replace("{", "\\{").replace("}", "\\}").replace("<", "&lt;")
                           for p in parts)
        out.append(line)
    return "\n".join(out)


# --- posts kept as JSON -------------------------------------------------------------
# A site that keeps each post as a JSON file (a Next.js or a hand-made site)
# gets the new one in the same shape: the newest post's keys in their order,
# each value of the same type (a list stays a list, minutes stay a number,
# ingredients stay {quantity, unit, name}), the values all posts share.

RECIPE_FOLDERS = ("content/ricette", "content/recipes", "src/content/recipes", "src/content/ricette", "recipes",
                  "_recipes", "data/recipes", "content/rezepte", "content/recettes", "content/recetas")
BODY_KEYS = ("body", "content", "markdown", "text", "html")
STEP_KEYS = ("steps", "instructions", "method", "procedimento", "directions")
SERVING_KEYS = ("servings", "yield", "portions", "porzioni", "recipeyield")
TIME_KEYS = {"preptime": "prep", "prep_time": "prep", "cooktime": "cook", "cook_time": "cook",
             "totaltime": "total", "total_time": "total"}
UNITS = ("kg", "g", "gr", "mg", "l", "dl", "cl", "ml", "cucchiai", "cucchiaio", "cucchiaini", "cucchiaino",
         "pizzico", "pizzichi", "spicchi", "spicchio", "bustina", "bustine", "fette", "fetta", "rametti", "rametto",
         "bicchiere", "bicchieri", "tazza", "tazze", "foglie", "foglia", "mazzetto", "noce", "pz", "tbsp", "tsp",
         "cups", "cup", "oz", "lb", "pinch", "cloves", "clove", "slices", "slice", "el", "tl", "prise",
         "c. à soupe", "c. à café", "cucharadas", "cucharada", "cucharaditas", "cucharadita")
_INGREDIENT = re.compile(r"^(?P<q>\d+(?:[.,/]\d+)?(?:\s*-\s*\d+(?:[.,]\d+)?)?|½|¼|¾|mezz[oa]|un[oa]?)\s*"
                         r"(?:(?P<u>" + "|".join(re.escape(u) for u in sorted(UNITS, key=len, reverse=True))
                         + r")\.?(?=\s|$))?\s*(?:di\s+|d'|of\s+|de\s+)?(?P<n>.+)$", re.I)
_TO_TASTE = re.compile(r"^(?P<n>.+?)[,\s]+(?P<q>q\.\s?b\.?|qb|to taste|quanto basta)$", re.I)


def is_recipes(posts: list[Post]) -> bool:
    """Whether the posts are recipes: ingredients and steps in each."""
    return bool(posts) and all(
        any(k.lower() == "ingredients" for k, _ in p.keys) and any(k.lower() in STEP_KEYS for k, _ in p.keys)
        for p in posts)


def split_ingredient(text: str) -> dict:
    """ "320 g di spaghetti" -> {quantity: "320", unit: "g", name: "spaghetti"}."""
    text = " ".join(text.split())
    taste = _TO_TASTE.match(text)
    if taste:
        return {"quantity": "q.b.", "unit": "", "name": taste.group("n")}
    found = _INGREDIENT.match(text)
    if found and found.group("n"):
        return {"quantity": found.group("q"), "unit": found.group("u") or "", "name": found.group("n")}
    return {"quantity": "", "unit": "", "name": text}


def _minutes(iso: str | None) -> int | None:
    found = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", iso or "")
    return int(found.group(1) or 0) * 60 + int(found.group(2) or 0) if found and (found.group(1) or found.group(2)) else None


def blocks(md: str) -> list[dict]:
    """Markdown as a site's content blocks: headings and paragraphs (a list's items one paragraph each)."""
    out = []
    for part in re.split(r"\n\s*\n", md.strip()):
        part = part.strip()
        head = re.match(r"^(#{1,6})\s+(.*)$", part)
        if head:
            out.append({"type": "heading", "level": len(head.group(1)), "text": head.group(2).strip()})
            continue
        lines = part.split("\n")
        if all(re.match(r"^(\d+[.)]|[-*])\s+", x.strip()) for x in lines):
            out += [{"type": "paragraph", "text": re.sub(r"^(\d+[.)]|[-*])\s+", "", x.strip())} for x in lines]
        elif part:
            out.append({"type": "paragraph", "text": " ".join(x.strip() for x in lines)})
    return out


def json_post(fmt: Format, payload: dict, slug: str, when: datetime, image_ref: str | None,
              image_alt: str | None, body: str) -> str:
    """The new post as JSON, in the shape of the site's newest post."""
    sample = fmt.data or {}
    card = payload.get("recipe") or {}
    description = payload.get("meta_description") or payload.get("subtitle")
    tags = list(card.get("tags") or ([payload["keyphrase"]] if payload.get("keyphrase") else []))
    minutes = {"prep": card.get("prep_minutes") or _minutes(card.get("prepTime")),
               "cook": card.get("cook_minutes") or _minutes(card.get("cookTime"))}
    if minutes["prep"] or minutes["cook"]:
        minutes["total"] = (minutes["prep"] or 0) + (minutes["cook"] or 0)
    out: dict = {}
    for key in sample or ("title", "description", "slug", "date", "image", "tags", "body"):
        sv, low = sample.get(key), key.lower()
        role = _role(key)
        value = None
        if low == "title":
            value = payload["title"]
        elif low == "slug":
            value = slug
        elif low == "keywords":
            words = list(dict.fromkeys(x for x in [payload.get("keyphrase"), *tags] if x))
            value = (words if isinstance(sv, list) else ", ".join(words)) or None
        elif low == "tags":
            value = (tags if not isinstance(sv, str) else ", ".join(tags)) or None
        elif role == "description":
            value = description
        elif role == "date":
            value = when.strftime("%Y-%m-%d") if isinstance(sv, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", sv) \
                else when.isoformat()
        elif role == "image":
            if image_ref and isinstance(sv, dict):
                sub = next((k for k in sv if k.lower() in ("src", "url", "path", "image")), "src")
                value = {sub: image_ref, **({"alt": image_alt} if "alt" in sv and image_alt else {})}
            else:
                value = image_ref
        elif role == "image_alt":
            value = image_alt if image_ref else None
        elif role == "section":
            value = payload.get("section") or (json.loads(fmt.constants[key]) if key in fmt.constants else None)
        elif role == "lang":
            value = payload.get("language")
        elif low in BODY_KEYS:
            value = blocks(body) if isinstance(sv, list) else body
        elif card and low == "ingredients":
            items = card.get("ingredient_list") or [split_ingredient(x) for x in card.get("ingredients") or []]
            if isinstance(sv, list) and sv and isinstance(sv[0], dict):
                names = list(sv[0])
                value = [{k: x.get(k, x.get("name", "") if k not in ("quantity", "unit") else "") for k in names}
                         for x in items]
            else:
                value = list(card.get("ingredients") or [])
        elif card and low in STEP_KEYS:
            if isinstance(sv, list) and sv and isinstance(sv[0], dict):
                sub = next((k for k, v in sv[0].items() if isinstance(v, str)), "text")
                value = [{sub: x} for x in card.get("steps") or []]
            else:
                value = list(card.get("steps") or [])
        elif card and low in SERVING_KEYS:
            number = card.get("servings") or next((int(x) for x in re.findall(r"\d+", str(card.get("yield") or ""))), None)
            value = number if isinstance(sv, (int, float)) else (card.get("yield") or (str(number) if number else None))
        elif card and low in TIME_KEYS:
            n = minutes.get(TIME_KEYS[low])
            value = (n if isinstance(sv, (int, float)) else (f"PT{n}M" if n else None)) if n else None
        elif card and low == "difficulty":
            value = card.get("difficulty")
        elif card and low == "nutrition":
            value = card.get("nutrition")
        elif low in ("readingtime", "reading_time") and isinstance(sv, (int, float)):
            value = max(1, round(len(body.split()) / 200))
        elif key in fmt.constants:
            value = json.loads(fmt.constants[key])
        if value not in (None, "", [], {}):
            out[key] = value
    if "title" not in out:
        out = {"title": payload["title"], **out}
    if card and not any(k.lower() == "ingredients" for k in out):
        out["recipe"] = {k: v for k, v in card.items() if k != "ingredient_list"}
    if payload.get("translation_of"):
        out["translation_of"] = payload["translation_of"]
    return json.dumps(out, ensure_ascii=False, indent=2) + "\n"


# --- the site's files ------------------------------------------------------------

def guess_url(fmt: Format, articles: str, domain: str | None, texts: list[str]) -> str | None:
    """The address pattern of the articles ({slug} in it): from a sample
    post found in llms.txt or the sitemap, else the folder's name on the
    site's domain."""
    for post in fmt.samples:
        if len(post.slug) < 4:
            continue
        for text in texts:
            match = re.search(r"https?://[^\s\"'<>)]*/" + re.escape(post.slug) + r"(/?)(?=[\s\"'<>)]|$)", text)
            if match:
                u = match.group(0)
                i = u.rfind("/" + post.slug)
                return u[:i] + "/{slug}" + u[i + 1 + len(post.slug):]
    if not domain:
        return None
    return f"https://{domain}/{posixpath.basename(articles.rstrip('/')) or 'blog'}/{{slug}}"


def add_to_llms(text: str, title: str, url: str, description: str | None, prefix: str | None) -> str | None:
    """llms.txt with a line for the article; None when it is there already."""
    if url in text:
        return None
    line = f"- [{title}]({url})" + (f": {description}" if description else "")
    lines = text.rstrip("\n").split("\n")
    at = None
    if prefix:
        at = next((i for i in range(len(lines) - 1, -1, -1) if prefix in lines[i] and lines[i].lstrip().startswith("- [")),
                  None)
    if at is None:
        head = next((i for i, x in enumerate(lines)
                     if re.match(r"^#{2,}\s.*(blog|news|notizie|articol|post|artikel|actualit|noticias)", x, re.I)), None)
        if head is not None:
            at = head
            while at + 1 < len(lines) and (lines[at + 1].lstrip().startswith("- ") or not lines[at + 1].strip()) \
                    and not (not lines[at + 1].strip() and at + 2 < len(lines) and lines[at + 2].startswith("#")):
                at += 1
            while at > head and not lines[at].strip():
                at -= 1
    if at is None:
        return "\n".join(lines) + f"\n\n## Blog\n\n{line}\n"
    lines.insert(at + 1, line)
    return "\n".join(lines) + "\n"


def add_to_sitemap(text: str, url: str, when: datetime) -> str | None:
    """A sitemap.xml kept as a file, with the article; None for an index of
    sitemaps or when the article is there already."""
    if "<urlset" not in text or "</urlset>" not in text or f"<loc>{url}</loc>" in text:
        return None
    entry = f"  <url>\n    <loc>{url}</loc>\n    <lastmod>{when.strftime('%Y-%m-%d')}</lastmod>\n  </url>\n"
    i = text.rfind("</urlset>")
    return text[:i] + entry + text[i:]


def remove_from_llms(text: str, url: str) -> str | None:
    """llms.txt without the article's line; None when it has none."""
    lines = text.split("\n")
    kept = [x for x in lines if not (x.lstrip().startswith("- [") and f"]({url})" in x)]
    return None if len(kept) == len(lines) else "\n".join(kept)


def remove_from_sitemap(text: str, url: str) -> str | None:
    """sitemap.xml without the article; None when it is not there."""
    out, n = re.subn(r"[ \t]*<url>\s*<loc>" + re.escape(url) + r"</loc>.*?</url>\n?", "", text, flags=re.S)
    return out if n else None


# --- GitHub ----------------------------------------------------------------------

READ_RETRIES = (2, 5, 15)  # seconds before each new try of a read that failed on GitHub's side


class GitHubError(RuntimeError):
    pass


class Repo:
    """The few GitHub calls a publication needs; `call(method, path, **kw)`
    sends one request to the API."""

    def __init__(self, call, repo: str, sleep=None, clock=None):
        self.raw_call, self.repo = call, repo
        self.sleep = sleep or time.sleep
        self.clock = clock or (lambda: time.monotonic())

    def call(self, method: str, path: str, retry: bool = True, **kw):
        """One request; when GitHub answers with a server error (500, 502,
        503, 504) or a rate limit it is tried again after a pause: these are
        GitHub's hiccups, and publications failed for them (2026-11-03,
        "reading llms.txt: 500"; 2026-11-04, "merge: 502"). Every write of a
        publication can be repeated without harm (git objects are content
        addressed, an existing branch or pull request is found again, the
        branch is moved to the same commit); the merge, which cannot, asks
        for `retry=False` and checks the pull request itself."""
        r = self.raw_call(method, path, **kw)
        for pause in READ_RETRIES:
            if not retry or r.status_code not in (429, 500, 502, 503, 504):
                break
            self.sleep(pause)
            r = self.raw_call(method, path, **kw)
        return r

    def _ok(self, r, what: str):
        if r.status_code >= 400:
            try:
                detail = r.json().get("message") or r.text
            except ValueError:
                detail = r.text
            raise GitHubError(f"GitHub, {what}: {r.status_code} {str(detail)[:200]}")
        return r.json() if r.content else {}

    def file(self, path: str, ref: str) -> bytes | None:
        r = self.call("GET", f"/repos/{self.repo}/contents/{quote(path)}?ref={quote(ref)}")
        if r.status_code == 404:
            return None
        body = self._ok(r, f"reading {path}")
        if not isinstance(body, dict) or "content" not in body:
            return None
        return base64.b64decode("".join((body.get("content") or "").split()))

    def listing(self, path: str, ref: str) -> list[dict]:
        r = self.call("GET", f"/repos/{self.repo}/contents/{quote(path)}?ref={quote(ref)}")
        if r.status_code == 404:
            return []
        body = self._ok(r, f"listing {path}")
        return body if isinstance(body, list) else []

    def posts(self, path: str, ref: str, limit: int = SAMPLES) -> list[Post]:
        """The newest posts of the articles folder (by name, which often
        starts with the date), with their front matter."""
        entries = sorted(self.listing(path, ref), key=lambda e: e.get("name", ""), reverse=True)
        out = []
        for e in entries:
            if len(out) >= limit:
                break
            name = e.get("name", "")
            if e.get("type") == "file" and name.endswith(POST_EXT) and not name.startswith(("_", "index.")):
                file_path, slug = e["path"], posixpath.splitext(name)[0]
            elif e.get("type") == "dir" and not name.startswith((".", "_")):
                found = next((x for x in self.listing(e["path"], ref)
                              if x.get("type") == "file" and x.get("name") in ("index.md", "index.mdx")), None)
                if found is None:
                    continue
                file_path, slug = found["path"], name
            else:
                continue
            content = self.file(file_path, ref)
            if file_path.endswith(".json"):
                try:
                    data = json.loads((content or b"").decode("utf-8", "replace"))
                except ValueError:
                    continue
                if not isinstance(data, dict) or not data.get("title"):
                    continue
                keys = [(k, json.dumps(v, ensure_ascii=False)) for k, v in data.items()]
                out.append(Post(file_path, keys, str(data.get("slug") or "") or slug, data))
                continue
            fm, _ = split_front_matter((content or b"").decode("utf-8", "replace"))
            if fm is None:
                continue
            keys = parse_front_matter(fm)
            slug = scalar(dict(keys).get("slug", "")) or slug
            out.append(Post(file_path, keys, slug))
        return out

    def recipe_folder(self, near: str, ref: str) -> str | None:
        """The folder of the site's recipes (posts with ingredients and steps):
        next to the articles' folder, or where sites usually keep them."""
        parent = posixpath.dirname(near.rstrip("/"))
        found = [e["path"] for e in self.listing(parent, ref) if e.get("type") == "dir"] if parent else []
        for folder in dict.fromkeys([*found, *RECIPE_FOLDERS]):
            if folder != near and is_recipes(self.posts(folder, ref, limit=2)):
                return folder
        return None

    def head(self, branch: str) -> str:
        return self._ok(self.call("GET", f"/repos/{self.repo}/git/ref/heads/{quote(branch)}"), "branch")["object"]["sha"]

    def commit(self, base_sha: str, files: list[tuple[str, bytes | None]], message: str) -> str:
        """One commit on top of `base_sha` with `files` (None removes the
        file); returns its sha."""
        tree = self._ok(self.call("GET", f"/repos/{self.repo}/git/commits/{base_sha}"), "base commit")["tree"]["sha"]
        entries = []
        for path, content in files:
            if content is None:  # the file is removed
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            blob = self._ok(self.call("POST", f"/repos/{self.repo}/git/blobs", json={
                "content": base64.b64encode(content).decode(), "encoding": "base64"}), f"writing {path}")
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        new_tree = self._ok(self.call("POST", f"/repos/{self.repo}/git/trees",
                                      json={"base_tree": tree, "tree": entries}), "tree")["sha"]
        return self._ok(self.call("POST", f"/repos/{self.repo}/git/commits", json={
            "message": message, "tree": new_tree, "parents": [base_sha]}), "commit")["sha"]

    def point(self, branch: str, sha: str, create: bool = False) -> None:
        """Move `branch` to `sha` (only forward), or create it; a branch left
        by an earlier attempt is moved, forcibly, to the new commit."""
        if create:
            r = self.call("POST", f"/repos/{self.repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})
            if r.status_code != 422:
                self._ok(r, "new branch")
                return
            self._ok(self.call("PATCH", f"/repos/{self.repo}/git/refs/heads/{quote(branch)}",
                               json={"sha": sha, "force": True}), "branch")
            return
        self._ok(self.call("PATCH", f"/repos/{self.repo}/git/refs/heads/{quote(branch)}",
                           json={"sha": sha, "force": False}), f"updating {branch}")

    def pull(self, head: str, base: str, title: str, body: str) -> dict:
        owner = self.repo.split("/", 1)[0]
        r = self.call("POST", f"/repos/{self.repo}/pulls", json={"title": title, "head": head, "base": base, "body": body})
        if r.status_code == 422:  # open already, from an earlier attempt
            found = self._ok(self.call("GET", f"/repos/{self.repo}/pulls?head={quote(f'{owner}:{head}')}&state=open"),
                             "pull request")
            if not found:
                raise GitHubError(f"cannot open the pull request for {head}, and none is open")
            return found[0]
        return self._ok(r, "pull request")

    def checks(self, sha: str) -> tuple[str, str | None]:
        """"success", "failure" (with the link of what failed), "pending", or
        "none" when the repository has no checks at all."""
        status = self._ok(self.call("GET", f"/repos/{self.repo}/commits/{sha}/status"), "checks")
        states = [(s.get("state"), s.get("target_url")) for s in status.get("statuses") or []]
        r = self.call("GET", f"/repos/{self.repo}/commits/{sha}/check-runs")
        if r.status_code < 400:  # a token without the checks permission sees only the statuses
            for run in (r.json() or {}).get("check_runs") or []:
                if run.get("status") != "completed":
                    states.append(("pending", run.get("details_url")))
                elif run.get("conclusion") in ("success", "neutral", "skipped"):
                    states.append(("success", run.get("details_url")))
                else:
                    states.append(("failure", run.get("details_url") or run.get("html_url")))
        if not states:
            return "none", None
        failures = [u for s, u in states if s in ("failure", "error")]
        if failures:
            return "failure", next((u for u in failures if u is not None), None)
        if any(s == "pending" for s, _ in states):
            return "pending", None
        return "success", None

    def wait(self, sha: str, minutes: float, grace: float = 90, every: float = 15) -> tuple[str, str | None]:
        """The checks' outcome; the deploy services need a few seconds before
        they report, so "none" counts only after `grace` seconds."""
        start = self.clock()
        while True:
            state, link = self.checks(sha)
            waited = self.clock() - start
            if state in ("success", "failure") or (state == "none" and waited >= grace):
                return state, link
            if waited >= minutes * 60:
                return "timeout", link
            self.sleep(every)

    def merge(self, number: int, title: str) -> str:
        for method in ("squash", "merge", "rebase"):
            for pause in (0, *READ_RETRIES):
                self.sleep(pause) if pause else None
                r = self.call("PUT", f"/repos/{self.repo}/pulls/{number}/merge", retry=False,
                              json={"merge_method": method, "commit_title": title})
                if r.status_code not in (429, 500, 502, 503, 504):
                    break
                # The answer is lost, the merge may not be: look at the pull request before sending it again.
                info = self.call("GET", f"/repos/{self.repo}/pulls/{number}")
                if info.status_code == 200 and info.json().get("merged"):
                    return info.json().get("merge_commit_sha") or ""
            if r.status_code == 405 and "method" in r.text.lower():
                continue
            return self._ok(r, "merge").get("sha", "")
        raise GitHubError("the repository allows no merge method")

    def drop(self, branch: str) -> None:
        self.call("DELETE", f"/repos/{self.repo}/git/refs/heads/{quote(branch)}")


def now() -> datetime:
    return datetime.now(UTC)
