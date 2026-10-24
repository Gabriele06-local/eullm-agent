"""Cover images: drawn for the article (never taken from the news), kept
here, served by token, shared by the translations and sent to the sites."""

import json
import socket

import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from editor import auth, images, models as m, publishing as pub, translate as tr
from editor.api import create_app
from editor.core_client import CoreClient

from test_drafts import chat
from test_publishing import approved_draft, public_resolver
from test_translate import ENGLISH

BFL = "https://api.eu.bfl.ai/v1"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


def fake_bfl(router, cost=3):
    submit = router.post(f"{BFL}/flux-2-pro").mock(return_value=httpx.Response(200, json={
        "id": "r1", "polling_url": f"{BFL}/get_result?id=r1", "cost": cost}))
    router.get(f"{BFL}/get_result?id=r1").mock(side_effect=[
        httpx.Response(200, json={"status": "Pending"}),
        httpx.Response(200, json={"status": "Ready", "result": {"sample": "https://delivery-eu.bfl.ai/r1.jpg"}})])
    router.get("https://delivery-eu.bfl.ai/r1.jpg").mock(
        return_value=httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"}))
    return submit


def imager():
    return images.Imager("bfl-key", BFL, "flux-2-pro", httpx.Client(), poll=0)


def test_an_image_is_drawn_for_the_article_and_served_by_token(db, tenant):
    did, _ = approved_draft(db, tenant)
    with respx.mock() as router:
        submit = fake_bfl(router)
        router.post("http://core.test/v1/llm/chat").mock(return_value=chat(json.dumps({
            "prompt": "A fibre optic cabinet on a sunny street in southern Italy, editorial photo",
            "alt": "Un armadio della fibra ottica in una strada di Bari"})))
        token = images.make(db, tenant, did, imager(), CoreClient("http://core.test", "tok"))
        sent = json.loads(submit.calls[0].request.content)
        assert submit.calls[0].request.headers["x-key"] == "bfl-key"
    assert "fibre optic cabinet" in sent["prompt"] and sent["width"] == images.WIDTH
    with db.tenant(tenant) as s:
        img = images.of(s, did)
        costs = s.execute(text("SELECT model, cost FROM editor.llm_usage WHERE purpose = 'image' ORDER BY id")).all()
    assert img.token == token and img.alt.startswith("Un armadio")
    assert costs[-1].model == "flux-2-pro" and abs(costs[-1].cost - 0.03) < 1e-9  # 3 credits of one cent
    r = TestClient(create_app(db)).get(f"/media/{token}")  # no login: the sites fetch it
    assert r.status_code == 200 and r.content == JPEG and r.headers["content-type"] == "image/jpeg"
    assert TestClient(create_app(db)).get("/media/nope").status_code == 404


def test_a_translation_takes_the_image_with_its_own_alt_text(db, tenant):
    did, _ = approved_draft(db, tenant)
    with respx.mock() as router:
        fake_bfl(router)
        images.make(db, tenant, did, imager(), None)  # without the model: a prompt from the title
        router.post("http://core.test/v1/llm/chat").mock(
            return_value=chat(json.dumps(dict(ENGLISH, image_alt="A fibre cabinet in Bari"))))
        tid = tr.translate(db, tenant, did, "en", CoreClient("http://core.test", "tok"))
    with db.tenant(tenant) as s:
        orig, copy = images.of(s, did), images.of(s, tid)
    assert copy is not None and copy.alt == "A fibre cabinet in Bari" and copy.token != orig.token
    assert orig.alt == "Bari in fibra"


def test_wordpress_gets_the_featured_image(db, tenant, monkeypatch):
    did, site_id = approved_draft(db, tenant)
    with respx.mock() as router:
        fake_bfl(router)
        images.make(db, tenant, did, imager(), None)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    monkeypatch.setenv(pub.secret_variable(tenant, "WP_PASS"), "app-pass-123")
    target = pub.add_target(db, tenant, site_id, "wordpress", "Blog", {"url": "https://blog.example", "user": "editor"}, "WP_PASS")
    pid = pub.request(db, tenant, did, target, "draft", "redazione")
    pub.decide(db, tenant, pid, True, "francesco")
    with respx.mock() as router:
        media = router.post("https://blog.example/wp-json/wp/v2/media").mock(
            return_value=httpx.Response(201, json={"id": 501}))
        alt = router.post("https://blog.example/wp-json/wp/v2/media/501").mock(return_value=httpx.Response(200, json={}))
        post = router.post("https://blog.example/wp-json/wp/v2/posts").mock(
            return_value=httpx.Response(201, json={"id": 77, "link": "https://blog.example/?p=77"}))
        assert pub.Publisher(resolver=public_resolver).run(db, tenant, pid)["status"] == "published"
    assert media.calls[0].request.content == JPEG and "filename=" in media.calls[0].request.headers["content-disposition"]
    assert json.loads(alt.calls[0].request.content) == {"alt_text": "Bari in fibra"}
    assert json.loads(post.calls[0].request.content)["featured_media"] == 501


def test_the_draft_page_shows_the_image_and_can_redo_it(db, tenant):
    did, _ = approved_draft(db, tenant)
    with respx.mock() as router:
        fake_bfl(router)
        token = images.make(db, tenant, did, imager(), None)
    owner = auth.create_token(db, tenant, "owner-user", "owner")
    c = TestClient(create_app(db, imager=imager()))
    assert c.post("/login", data={"token": owner}, follow_redirects=False).status_code == 303
    page = c.get(f"/drafts/{did}").text
    assert f'src="/media/{token}"' in page and ("Rifai l&#39;immagine" in page or "Rifai l'immagine" in page)
    c.post(f"/ui/drafts/{did}/image")
    with db.tenant(tenant) as s:
        assert s.execute(select(m.jobs.c.subject).where(m.jobs.c.kind == "image")).scalar() == f"draft {did}"
    c.post(f"/ui/drafts/{did}/image/delete")
    with db.tenant(tenant) as s:
        assert images.of(s, did) is None
