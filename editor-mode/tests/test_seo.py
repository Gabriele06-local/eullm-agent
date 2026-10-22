import json
import socket
from datetime import UTC, datetime

import httpx
import respx
from sqlalchemy import select

from editor import drafts, models as m, publishing as pub, seo
from editor.core_client import CoreClient
from editor.dedup import minhash
from editor.normalize import url_hash
from test_drafts import accepted_proposal, chat
from test_publishing import public_resolver


def test_slug_title_and_description_are_clamped():
    assert seo.slugify("Città più veloci: la fibra è arrivata!") == "citta-piu-veloci-la-fibra-e-arrivata"
    sd = seo.parse({"title": "Un titolo per Google davvero molto lungo che supera di parecchio i sessanta caratteri",
                    "description": "parola " * 40, "keyphrase": "fibra Bari"}, "Titolo")
    assert len(sd.title) <= seo.TITLE_MAX and not sd.title.endswith(" ")
    assert len(sd.description) <= seo.DESCRIPTION[1] and sd.description.endswith("…")
    assert sd.slug == "fibra-bari"
    missing = seo.parse(None, "Bari cablata in fibra")
    assert missing.title == "Bari cablata in fibra" and missing.slug == "bari-cablata-in-fibra" and missing.keyphrase == ""


def test_checks_say_what_is_missing():
    sd = seo.Seo(keyphrase="fibra Bari", title="Fibra a Bari: cosa cambia per le case",
                 description="La rete in fibra di Bari è completa: chi è coperto, quanto va veloce e cosa fare per attivarla da subito.",
                 faq=["Chi è coperto?", "Quanto costa?"], links=[{"title": "x", "url": "https://blog.example/x"}])
    lead = "A Bari la rete in fibra è completa: raggiunge 120.000 case con velocità fino a 10 Gbit/s, dopo diciotto mesi di lavori."
    ok = {c["key"]: c["ok"] for c in seo.checks(sd, sd.title, lead, [None, "I lavori", "Chi è coperto?"], 600, 700, 3)}
    assert all(ok.values()), ok
    bad = {c["key"]: c["ok"] for c in seo.checks(seo.Seo(title="Breve"), "Breve", "Poche parole.", [None], 100, 700, 1)}
    assert not any(bad.values()), bad


def test_structured_data_says_what_the_page_says():
    data = seo.jsonld(title="T", description="D", language="it", url=None, keyphrase="k", published="2026-10-22",
                      site_name="Blog", site_url="https://blog.example/", citations=["https://a.example/1"],
                      faq=[("Chi?", "Tutti."), ("Quando?", "Ora.")])
    assert data[0]["@type"] == "NewsArticle" and data[0]["citation"] == ["https://a.example/1"] and "url" not in data[0]
    assert data[1]["@type"] == "FAQPage" and data[1]["mainEntity"][0]["acceptedAnswer"]["text"] == "Tutti."
    assert len(seo.jsonld(title="T", description="D", language=None, url=None, keyphrase="", published=None,
                          site_name=None, site_url=None, citations=[], faq=[("Chi?", "Tutti.")])) == 1


def seo_draft(db, tenant):
    pid, cited = accepted_proposal(db, tenant)
    first = cited[0]
    with db.tenant(tenant) as s:
        site_id = s.execute(select(m.proposals.c.site_id).where(m.proposals.c.id == pid)).scalar()
        for title, url in (("Fibra a Bari: i quartieri coperti", "https://blog.example/bari-quartieri"),
                           ("Ricette di stagione", "https://blog.example/ricette")):
            s.execute(m.site_posts.insert().values(tenant_id=tenant, site_id=site_id, url=url, url_hash=url_hash(url),
                                                   title=title, minhash=minhash(title), published_at=datetime.now(UTC)))
    answer = json.dumps({
        "title": "Fibra a Bari: la rete è completa", "subtitle": "Open Fiber ha chiuso i lavori",
        "sections": [
            {"heading": None, "claims": [
                {"text": "A Bari la rete in fibra fino a casa di Open Fiber è stata completata.", "sources": [first]},
                {"text": "La copertura riguarda 120.000 unità immobiliari.", "sources": [first]}]},
            {"heading": "Quanto è veloce la fibra a Bari?", "claims": [
                {"text": "Le velocità annunciate arrivano a 10 Gbit/s.", "sources": [first]}]}],
        "faq": [
            {"question": "Quanto sono durati i lavori?", "claims": [{"text": "Il cantiere è durato 18 mesi.", "sources": [first]}]},
            {"question": "Quali zone sono coperte?", "claims": [{"text": "I lavori hanno coinvolto 3 quartieri.", "sources": [first]}]},
            {"question": "Domanda senza risposta", "claims": []}],
        "seo": {"keyphrase": "fibra Bari", "title": "Fibra a Bari: rete completa, 120.000 case",
                "description": "Open Fiber ha completato la rete FTTH a Bari: 120.000 case coperte, fino a 10 Gbit/s dopo 18 mesi di lavori in 3 quartieri.",
                "slug": "fibra-bari-rete-completa"}})
    with respx.mock(assert_all_called=False) as router:
        router.post("http://core.test/v1/llm/chat").mock(return_value=chat(answer))
        did = drafts.write_draft(db, tenant, pid, CoreClient("http://core.test", "tok"), None)
    drafts.decide(db, tenant, did, True, "francesco")
    return did, site_id


def test_draft_carries_faq_links_and_search_fields(db, tenant):
    did, _ = seo_draft(db, tenant)
    with db.tenant(tenant) as s:
        d = s.execute(select(m.drafts).where(m.drafts.c.id == did)).one()
    assert d.seo["keyphrase"] == "fibra Bari" and d.seo["slug"] == "fibra-bari-rete-completa"
    assert d.seo["faq"] == ["Quanto sono durati i lavori?", "Quali zone sono coperte?"]
    assert d.seo["links"] == [{"title": "Fibra a Bari: i quartieri coperti", "url": "https://blog.example/bari-quartieri"}]
    assert "## Quanto sono durati i lavori?" in d.body_md and "Domanda senza risposta" not in d.body_md
    assert "## Leggi anche" in d.body_md and d.body_md.index("## Leggi anche") < d.body_md.index("## Fonti")
    ok = {c["key"]: c["ok"] for c in d.seo["checks"]}
    assert ok["keyphrase_title"] and ok["keyphrase_lead"] and ok["faq"] and ok["internal_links"]


def test_wordpress_gets_slug_description_structured_data_and_indexnow(db, tenant, monkeypatch):
    did, site_id = seo_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    monkeypatch.setenv(pub.secret_variable(tenant, "WP_PASS"), "app-pass-123")
    plain = pub.add_target(db, tenant, site_id, "wordpress", "Bozze", {"url": "https://blog.example", "user": "e"}, "WP_PASS")
    full = pub.add_target(db, tenant, site_id, "wordpress", "Sito",
                          {"url": "https://blog.example", "user": "e", "jsonld": True, "indexnow_key": "abcd1234efgh"}, "WP_PASS")
    p1, p2 = pub.request(db, tenant, did, plain, "draft", "r"), pub.request(db, tenant, did, full, "publish", "r")
    for p in (p1, p2):
        pub.decide(db, tenant, p, True, "francesco")
    publisher = pub.Publisher(resolver=public_resolver)
    with respx.mock() as router:
        wp = router.post("https://blog.example/wp-json/wp/v2/posts").mock(side_effect=[
            httpx.Response(201, json={"id": 1, "link": "https://blog.example/?p=1"}),
            httpx.Response(201, json={"id": 2, "link": "https://blog.example/fibra-bari-rete-completa/"})])
        ping = router.post(seo.INDEXNOW).mock(return_value=httpx.Response(202))
        assert "indexnow" not in publisher.run(db, tenant, p1)
        assert publisher.run(db, tenant, p2)["indexnow"] == "sent"
    first, second = (json.loads(c.request.content) for c in wp.calls)
    assert first["slug"] == "fibra-bari-rete-completa" and first["excerpt"].startswith("Open Fiber ha completato")
    assert "application/ld+json" not in first["content"]
    assert '<script type="application/ld+json">' in second["content"] and '"FAQPage"' in second["content"]
    assert json.loads(ping.calls[0].request.content) == {
        "host": "blog.example", "key": "abcd1234efgh", "urlList": ["https://blog.example/fibra-bari-rete-completa/"]}


def test_indexnow_failure_keeps_the_publication(db, tenant, monkeypatch):
    did, site_id = seo_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    monkeypatch.setenv(pub.secret_variable(tenant, "WP_PASS"), "app-pass-123")
    target = pub.add_target(db, tenant, site_id, "wordpress", "Sito",
                            {"url": "https://blog.example", "user": "e", "indexnow_key": "abcd1234efgh"}, "WP_PASS")
    p = pub.request(db, tenant, did, target, "publish", "r")
    pub.decide(db, tenant, p, True, "francesco")
    with respx.mock() as router:
        router.post("https://blog.example/wp-json/wp/v2/posts").mock(
            return_value=httpx.Response(201, json={"id": 3, "link": "https://blog.example/x/"}))
        router.post(seo.INDEXNOW).mock(return_value=httpx.Response(403))
        out = pub.Publisher(resolver=public_resolver).run(db, tenant, p)
    assert out["status"] == "published" and out["indexnow"] == "failed: HTTP 403"


def test_draft_page_and_publication_form(db, tenant, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from editor import auth
    from editor.api import create_app
    did, site_id = seo_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    c = TestClient(create_app(db))
    c.post("/login", data={"token": auth.create_token(db, tenant, "boss", "owner")})
    page = c.get(f"/drafts/{did}").text
    assert "Google e risposte AI" in page and "fibra-bari-rete-completa" in page and "La frase chiave è nel titolo" in page
    assert "https://blog.example/bari-quartieri" in page
    r = c.post("/ui/targets", data={"site_id": site_id, "kind": "wordpress", "name": "Sito", "user": "e",
                                    "address": "https://blog.example", "indexnow_key": "no", "secret_env": "WP_PASS"})
    assert "8 a 128" in r.text
    c.post("/ui/targets", data={"site_id": site_id, "kind": "wordpress", "name": "Sito", "user": "e", "jsonld": "1",
                                "address": "https://blog.example", "indexnow_key": "abcd1234efgh", "secret_env": "WP_PASS"})
    with db.tenant(tenant) as s:
        config = s.execute(text("SELECT config FROM editor.publish_targets WHERE name = 'Sito'")).scalar()
    if isinstance(config, str):
        config = json.loads(config)
    assert config["indexnow_key"] == "abcd1234efgh" and config["jsonld"] is True
