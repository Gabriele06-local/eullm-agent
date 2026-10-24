"""Delivery of the approved articles without a connected site: the package
to download, the email at each approval, the feed for the automation tools."""

import io
import json
import zipfile
from xml.etree import ElementTree

import respx
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, text

from editor import auth, delivery, images, models as m
from editor.api import create_app

from test_images import JPEG, fake_bfl, imager
from test_publishing import approved_draft


def with_image(db, tenant):
    did, site_id = approved_draft(db, tenant)
    with respx.mock() as router:
        fake_bfl(router)
        images.make(db, tenant, did, imager(), None)
    return did, site_id


def owner_client(db, tenant, **kw):
    token = auth.create_token(db, tenant, "owner-user", "owner")
    c = TestClient(create_app(db, public_url="https://redazione.example", **kw))
    assert c.post("/login", data={"token": token}, follow_redirects=False).status_code == 303
    return c


def test_the_package_has_page_text_markdown_image_and_fields(db, tenant):
    did, _ = with_image(db, tenant)
    c = owner_client(db, tenant)
    r = c.get(f"/drafts/{did}/package.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert 'filename="bari-in-fibra-it.zip"' in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["article.html", "article.md", "body.html", "cover.jpg", "meta.json"]
    assert z.read("cover.jpg") == JPEG
    body = z.read("body.html").decode()
    assert "<h1>" not in body and "Open Fiber ha lavorato a Bari." in body and 'class="sources"' in body
    page = z.read("article.html").decode()
    assert '<html lang="it">' in page and '<img src="cover.jpg" alt="Bari in fibra"' in page
    assert '"@type": "NewsArticle"' in page and "<h1>Bari in fibra</h1>" in page
    md = z.read("article.md").decode()
    assert md.startswith('---\ntitle: "Bari in fibra"\n') and 'image: "cover.jpg"' in md and "\n_La rete è completa_\n" in md
    meta = json.loads(z.read("meta.json"))
    assert meta["slug"] == "bari-in-fibra" and meta["image"]["url"].startswith("https://redazione.example/media/")
    assert meta["sources"] and meta["approved_at"]
    page = c.get(f"/drafts/{did}").text
    assert f'href="/drafts/{did}/package.zip"' in page and 'data-copy="body-html"' in page


def test_approving_emails_the_article_with_the_package(db, tenant):
    did, _ = with_image(db, tenant)
    with db.tenant(tenant) as s:
        s.execute(text("UPDATE editor.drafts SET status = 'draft', decided_by = NULL, decided_at = NULL WHERE id = :d"),
                  {"d": did})
        s.execute(insert(m.recipients).values(tenant_id=tenant, channel="email", address="redazione@example.com",
                                              enabled=True))
    c = owner_client(db, tenant)
    r = c.post(f"/ui/drafts/{did}/decision", data={"accept": "1"})
    assert "per email" in r.text
    with db.tenant(tenant) as s:
        assert s.execute(select(m.jobs.c.subject).where(m.jobs.c.kind == "deliver")).scalar() == f"draft {did}"
    sent = []
    out = delivery.email(db, tenant, did, lambda *a: sent.append(a), "https://redazione.example")
    assert out == {"title": "Bari in fibra", "sent": 1}
    to, subject, body, page, (name, data) = sent[0]
    assert to == ["redazione@example.com"] and subject == "Articolo approvato: Bari in fibra"
    assert "Titolo per Google: Bari in fibra" in body and "Open Fiber ha lavorato a Bari." in body
    assert 'src="https://redazione.example/media/' in page and name == "bari-in-fibra-it.zip"
    assert "cover.jpg" in zipfile.ZipFile(io.BytesIO(data)).namelist()


def test_the_feed_gives_the_approved_articles_without_login(db, tenant):
    did, site_id = with_image(db, tenant)
    c = owner_client(db, tenant)
    assert "Crea il feed" in c.get("/site").text
    c.post(f"/ui/sites/{site_id}/feed")
    with db.tenant(tenant) as s:
        token = delivery.current_token(s, site_id)
    assert f"/feeds/{token}.json" in c.get("/site").text
    anon = TestClient(create_app(db, public_url="https://redazione.example"))
    feed = anon.get(f"/feeds/{token}.json").json()
    item = feed["items"][0]
    assert feed["version"] == "https://jsonfeed.org/version/1.1" and item["title"] == "Bari in fibra"
    assert item["image"].startswith("https://redazione.example/media/") and "<h1>" not in item["content_html"]
    assert item["_redazia"]["slug"] == "bari-in-fibra" and item["_redazia"]["image_alt"] == "Bari in fibra"
    rss = ElementTree.fromstring(anon.get(f"/feeds/{token}.rss").content)
    assert rss.find("channel/item/title").text == "Bari in fibra"
    assert rss.find("channel/item/enclosure").get("length") == str(len(JPEG))
    assert anon.get(f"/feeds/{token}.json?lang=de").json()["items"] == []
    assert anon.get("/feeds/nope.json").status_code == 404
    c.post(f"/ui/sites/{site_id}/feed", data={"renew": "1"})
    assert anon.get(f"/feeds/{token}.json").status_code == 404
