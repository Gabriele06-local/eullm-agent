"""A whole menu for a site of recipes (Francesco 2026-11-10): the site's own
recipes where one fits, new ones written as drafts of their own, the menu
out by itself after its new recipes are online."""

import json
import socket

import httpx
import pytest
import respx
from sqlalchemy import insert, select, text, update

from editor import drafts, menus, models as m, publishing as pub
from editor.core_client import CoreClient
from editor.dedup import minhash
from editor.normalize import url_hash
from test_drafts import chat
from test_own_recipes import PAGE, doc, recipe_site
from test_publishing import public_resolver

ARROSTO = "https://lagrandecucina.example/ricette/arrosto-di-vitello"


def menu_doc(first):
    return {"title": "Menù di Natale della tradizione", "subtitle": "Quattro portate per la vigilia",
            "sections": [{"heading": None, "claims": [
                {"text": "A Natale la tavola italiana vuole piatti di festa da preparare in anticipo.",
                 "sources": [first]}]}],
            "seo": {"keyphrase": "menù di natale", "slug": "menu-di-natale"},
            "menu": [{"course": "Antipasto", "new": {"title": "Crostini di fegatini alla toscana",
                                                      "angle": "Un antipasto di festa"}, "why": "Si fa il giorno prima."},
                     {"course": "Secondo", "use": ARROSTO, "why": "L'arrosto della domenica diventa di festa."},
                     {"course": "Dolce", "new": {"title": "Panettone farcito alla crema",
                                                  "angle": "Il dolce delle feste"}, "why": "Si prepara in un'ora."},
                     {"course": "Primo", "use": "https://altrove.example/x", "why": "non è del sito"}]}


def write_menu(db, tenant, pid, first):
    answers = [json.dumps({**menu_doc(first), "menu": menu_doc(first)["menu"][:3] + [
        {"course": "Primo", "use": "https://altrove.example/x"}]}), json.dumps(menu_doc(first))]
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://(news|rete)\.example/.*").mock(
            return_value=httpx.Response(200, text=PAGE, headers={"content-type": "text/html"}))
        router.get(url__regex=r".*/robots\.txt").mock(return_value=httpx.Response(404))
        router.get(url__regex=r".*/\.well-known/tdmrep\.json").mock(return_value=httpx.Response(404))
        prompts = []

        def answer(request):
            prompts.append(json.loads(request.content)["messages"][-1]["content"])
            if len(prompts) == 1:  # an address not among the site's recipes: told to fix it
                return chat(answers[0])
            if len(prompts) == 2:
                return chat(json.dumps({**menu_doc(first), "menu": menu_doc(first)["menu"][:3]}))
            return chat(json.dumps(doc(first, title="Crostini di fegatini alla toscana" if len(prompts) == 3
                                       else "Panettone farcito alla crema")))

        router.post("http://core.test/v1/llm/chat").mock(side_effect=answer)
        with httpx.Client() as client:
            did = drafts.write_draft(db, tenant, pid, CoreClient("http://core.test", "tok"), client)
    return did, prompts


def test_a_menu_reuses_the_site_recipes_and_writes_the_missing_ones(db, tenant, monkeypatch):
    pid, cited, site_id = recipe_site(db, tenant)
    with db.tenant(tenant) as s:
        s.execute(update(m.proposals).where(m.proposals.c.id == pid).values(format="menu"))
        s.execute(insert(m.site_posts).values(tenant_id=tenant, site_id=site_id, url=ARROSTO, url_hash=url_hash(ARROSTO),
                                              title="Arrosto di vitello al latte", minhash=minhash("arrosto")))
    did, prompts = write_menu(db, tenant, pid, cited[0])
    assert "Arrosto di vitello al latte <" + ARROSTO + ">" in prompts[0] and "whole menu" in prompts[0]
    assert "not among the site's recipes" in prompts[1]
    with db.tenant(tenant) as s:
        d = s.execute(select(m.drafts).where(m.drafts.c.id == did)).one()
        menu = d.seo["menu"]
        kids = [x["draft_id"] for x in menu if x.get("draft_id")]
        recipes = s.execute(select(m.drafts.c.title, m.drafts.c.seo).where(m.drafts.c.id.in_(kids))).all()
        basis = s.execute(select(m.proposals.c.basis).join(m.drafts, m.drafts.c.proposal_id == m.proposals.c.id)
                          .where(m.drafts.c.id == kids[0])).scalar()
    assert [x["course"] for x in menu] == ["Antipasto", "Secondo", "Dolce"] and len(kids) == 2
    assert menu[1]["url"] == ARROSTO and all(r.seo["recipe"]["own"] for r in recipes)
    assert basis["menu_draft"] == did and basis["course"] == "Antipasto"
    assert f"](draft:{kids[0]})" in d.body_md and f"[Arrosto di vitello al latte]({ARROSTO})" in d.body_md

    # Not out before its new recipes: they go first, the menu follows by itself.
    drafts.decide(db, tenant, did, True, "francesco")
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    target = pub.add_target(db, tenant, site_id, "webhook", "sito", {"url": "https://hooks.example/in"}, None)
    with pytest.raises(pub.PublishError):
        pub.request(db, tenant, did, target, "publish", "francesco")
    sent = []
    assert menus.publish_first(db, tenant, did, target, "publish", "francesco", True, sent.append) == 2
    with db.tenant(tenant) as s:
        assert s.execute(text("SELECT count(*) FROM editor.drafts WHERE status = 'approved'")).scalar() == 3
        for n, (pid_, kid) in enumerate(zip(sent, kids)):
            s.execute(update(m.publications).where(m.publications.c.id == pid_).values(
                status="published", external_url=f"https://lagrandecucina.example/ricette/r{n}"))
    with monkeypatch.context() as mp:
        def refuse(*a, **kw):
            raise pub.PublishError("target refused")
        mp.setattr(pub, "request", refuse)
        assert menus.after_publication(db, tenant, sent[0], sent.append) is None  # failed publish
    with db.tenant(tenant) as s:
        assert "publish_after" in s.execute(select(m.drafts.c.seo).where(m.drafts.c.id == did)).scalar()
    assert menus.after_publication(db, tenant, sent[0], sent.append) is not None  # both online now
    with db.tenant(tenant) as s:
        p = s.execute(select(m.publications).where(m.publications.c.draft_id == did)).one()
        assert "publish_after" not in s.execute(select(m.drafts.c.seo).where(m.drafts.c.id == did)).scalar()
    assert p.status == "approved" and "](https://lagrandecucina.example/ricette/r0)" in p.payload["markdown"]
    assert menus.after_publication(db, tenant, sent[1], sent.append) is None  # sent once


def test_the_newsroom_reads_the_menu_with_its_courses(db, tenant, monkeypatch):
    from test_ui import logged_in

    pid, cited, site_id = recipe_site(db, tenant)
    with db.tenant(tenant) as s:
        s.execute(update(m.proposals).where(m.proposals.c.id == pid).values(format="menu"))
        s.execute(insert(m.site_posts).values(tenant_id=tenant, site_id=site_id, url=ARROSTO, url_hash=url_hash(ARROSTO),
                                              title="Arrosto di vitello al latte", minhash=minhash("arrosto")))
    did, _ = write_menu(db, tenant, pid, cited[0])
    page = logged_in(db, tenant, "owner").get(f"/drafts/{did}").text
    assert "Antipasto" in page and "ricetta già sul sito" in page and "ricetta nuova" in page
    assert "Crostini di fegatini alla toscana" in page
