"""Connecting a site: the guided path from a domain to a site the newsroom
can update. The credential is tried for real before anything is saved."""

import json
import socket

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update

from editor import connect, models as m, publishing as pub, vault
from editor.api import create_app
from github_fake import FakeGitHub
from test_publishing import ASTRO_POST, approved_draft, public_resolver
from test_ui import logged_in

WP = "https://blog.example"


def wp_site(router, *, auth=True, role_caps=None, categories=None, posts=3):
    router.get(f"{WP}/wp-json/").mock(return_value=httpx.Response(200, json={
        "name": "Il Blog &amp; Co", "description": "Notizie", "namespaces": ["wp/v2"]}))
    me = router.get(f"{WP}/wp-json/wp/v2/users/me")
    if auth:
        me.mock(return_value=httpx.Response(200, json={
            "name": "Redazione", "roles": ["editor"],
            "capabilities": role_caps or {"edit_posts": True, "publish_posts": True, "upload_files": True}}))
    else:
        me.mock(return_value=httpx.Response(401, json={"code": "incorrect_password"}))
    router.get(f"{WP}/wp-json/wp/v2/categories").mock(return_value=httpx.Response(200, json=categories or [
        {"id": 5, "name": "Tecnologia &amp; AI", "slug": "tecnologia-ai", "count": 12},
        {"id": 1, "name": "Senza categoria", "slug": "senza-categoria", "count": 3},
        {"id": 9, "name": "Economia", "slug": "economia", "count": 4}]))
    router.get(f"{WP}/wp-json/wp/v2/posts").mock(return_value=httpx.Response(
        200, json=[{"id": 1}] if posts else [], headers={"X-WP-Total": str(posts)}))


def test_wordpress_is_tried_and_its_categories_read():
    with respx.mock(assert_all_called=False) as router:
        wp_site(router)
        found = connect.wordpress("blog.example/wp-admin/", "redazione", "abcd efgh", resolver=public_resolver)
    assert found["base"] == WP and found["name"] == "Il Blog & Co"
    assert found["can_publish"] and found["can_upload"]
    assert [c["name"] for c in found["categories"]] == ["Tecnologia & AI", "Senza categoria", "Economia"]
    assert found["sections"] == ["Tecnologia & AI", "Economia"]  # the default category says nothing about the site
    assert found["posts"] == 3


def test_wordpress_without_pretty_permalinks_goes_by_rest_route():
    """wp.aitoday.it, 2026-11-05: /wp-json/ answered 404 from the web server,
    ?rest_route= worked; the user check went to /wp-json/ and failed."""
    page404 = httpx.Response(404, text="<h1>Not Found</h1>", headers={"content-type": "text/html"})
    with respx.mock(assert_all_called=False) as router:
        router.route(host="blog.example", path__startswith="/wp-json/").mock(return_value=page404)
        router.get(f"{WP}/", params={"rest_route": "/"}).mock(return_value=httpx.Response(200, json={
            "name": "Blog", "namespaces": ["wp/v2"]}))
        me = router.get(f"{WP}/", params__contains={"rest_route": "/wp/v2/users/me"}).mock(
            return_value=httpx.Response(200, json={"name": "R", "capabilities": {"edit_posts": True}}))
        router.get(f"{WP}/", params__contains={"rest_route": "/wp/v2/categories"}).mock(
            return_value=httpx.Response(200, json=[{"id": 9, "name": "Economia", "slug": "economia", "count": 4}]))
        router.get(f"{WP}/", params__contains={"rest_route": "/wp/v2/posts"}).mock(
            return_value=httpx.Response(200, json=[], headers={"X-WP-Total": "0"}))
        found = connect.wordpress(WP, "redazione", "abcd efgh", resolver=public_resolver)
    assert me.called and dict(me.calls[0].request.url.params)["context"] == "edit"
    assert found["sections"] == ["Economia"]
    assert pub.wp_url(WP, "wp/v2/posts", "a=1", plain=True) == f"{WP}/?rest_route=/wp/v2/posts&a=1"


def test_wordpress_says_what_is_wrong():
    with respx.mock(assert_all_called=False) as router:
        wp_site(router, auth=False)
        with pytest.raises(connect.ConnectError) as e:
            connect.wordpress(WP, "redazione", "sbagliata", resolver=public_resolver)
    assert e.value.code == "auth"
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{WP}/wp-json/").mock(return_value=httpx.Response(404))
        router.get(f"{WP}/").mock(return_value=httpx.Response(404))
        with pytest.raises(connect.ConnectError) as e:
            connect.wordpress(WP, "u", "p", resolver=public_resolver)
    assert e.value.code == "not_wordpress"
    with respx.mock(assert_all_called=False) as router:
        wp_site(router, role_caps={"read": True})
        with pytest.raises(connect.ConnectError) as e:
            connect.wordpress(WP, "u", "p", resolver=public_resolver)
    assert e.value.code == "role"
    with pytest.raises(connect.ConnectError) as e:
        connect.wordpress("http://blog.example", "u", "p", resolver=public_resolver)
    assert e.value.code == "https"
    with pytest.raises(connect.ConnectError) as e:
        connect.wordpress(WP, "", "", resolver=public_resolver)
    assert e.value.code == "credentials_missing"


def astro_repo(**extra):
    files = {"package.json": json.dumps({"dependencies": {"astro": "^5"}}), "vercel.json": "{}",
             "public/llms.txt": "# Sito\n", "content/blog/vecchio.md": ASTRO_POST}
    files.update(extra)
    return FakeGitHub(files)


def test_github_reads_the_site_and_where_the_articles_go():
    gh = astro_repo()
    with respx.mock(assert_all_called=False) as router:
        gh.route(router)
        found = connect.github("https://github.com/me/site", "tok", resolver=public_resolver)
    assert found["repo"] == "me/site" and found["branch"] == "main"
    assert found["generator"] == "Astro" and found["host"] == "vercel"
    assert found["articles_path"] == "content/blog" and found["articles_found"] == 1
    assert found["format"]["ext"] == ".md" and found["has_llms"] and not found["has_sitemap"]
    assert gh.token == "Bearer tok"


def test_github_without_posts_or_known_host():
    gh = FakeGitHub({"README.md": "sito", "wrangler.toml": "name = 'x'"})
    with respx.mock(assert_all_called=False) as router:
        gh.route(router)
        found = connect.github("me/site", "tok", resolver=public_resolver)
    assert found["host"] == "cloudflare" and found["articles_path"] is None and found["format"] is None


def test_github_failures_have_a_reason():
    with respx.mock(assert_all_called=False) as router:
        router.get("https://api.github.com/repos/me/site").mock(return_value=httpx.Response(404, json={}))
        with pytest.raises(connect.ConnectError) as e:
            connect.github("me/site", "tok", resolver=public_resolver)
    assert e.value.code == "repo_missing"
    with respx.mock(assert_all_called=False) as router:
        router.get("https://api.github.com/repos/me/site").mock(return_value=httpx.Response(401, json={}))
        with pytest.raises(connect.ConnectError) as e:
            connect.github("me/site", "bad", resolver=public_resolver)
    assert e.value.code == "token_bad"
    with respx.mock(assert_all_called=False) as router:
        router.get("https://api.github.com/repos/me/site").mock(return_value=httpx.Response(
            200, json={"default_branch": "main", "permissions": {"pull": True, "push": False}}))
        with pytest.raises(connect.ConnectError) as e:
            connect.github("me/site", "ro", resolver=public_resolver)
    assert e.value.code == "no_write"
    with pytest.raises(connect.ConnectError) as e:
        connect.github("non un repo", "tok", resolver=public_resolver)
    assert e.value.code == "repo_name"


def test_the_article_goes_in_the_category_of_its_section(db, tenant, monkeypatch):
    did, site_id = approved_draft(db, tenant)
    config = {"url": WP, "user": "e", "categories": [{"id": 5, "name": "Tecnologia & AI", "slug": "tecnologia-ai"}]}
    target = type("T", (), {"config": config})()
    post = pub.Publisher._wordpress_post(target, {"title": "T", "html": "<p>x</p>", "subtitle": "s",
                                                  "section": "tecnologia & ai"}, "publish")
    assert post["categories"] == [5]
    post = pub.Publisher._wordpress_post(target, {"title": "T", "html": "<p>x</p>", "subtitle": "s",
                                                  "section": "Sport"}, "publish")
    assert "categories" not in post  # no such category: the site's default stays
    assert connect.category_id(config, "tecnologia-ai") == 5 and connect.category_id(None, "x") is None


def test_adding_a_site_leads_to_the_connection_and_the_connection_to_the_analysis(db, tenant, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    monkeypatch.setattr(connect, "resolver", public_resolver)
    monkeypatch.setenv(vault.KEY_ENV, vault.new_key())
    c = logged_in(db, tenant, "owner")
    r = c.post("/ui/sites", data={"domain": "nuovo.example", "back": "/start"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/connect"
    with db.tenant(tenant) as s:
        site_id = s.execute(select(m.sites.c.id).where(m.sites.c.domain == "nuovo.example")).scalar()
        assert site_id is not None
        assert not s.execute(select(m.jobs.c.id).where(m.jobs.c.kind == "analysis")).first()  # not yet read
    page = c.get("/connect")
    assert page.status_code == 200 and "nuovo.example" in page.text and "WordPress" in page.text
    assert "Password delle applicazioni" in c.get("/connect?kind=wordpress").text
    assert "github.com/settings/personal-access-tokens/new" in c.get("/connect?kind=github").text
    assert "vercel.com/new" in c.get("/connect?kind=github&host=vercel").text

    # A wrong password: nothing saved, the reason on the page, the analysis not started.
    with respx.mock(assert_all_called=False) as router:
        wp_site(router, auth=False)
        bad = c.post(f"/ui/connect/{site_id}/check", data={
            "kind": "wordpress", "address": WP, "user": "redazione", "secret": "sbagliata"})
    assert bad.status_code == 200 and "ha rifiutato utente o password" in bad.text
    with db.tenant(tenant) as s:
        assert not s.execute(select(m.publish_targets.c.id).where(m.publish_targets.c.site_id == site_id)).first()
        assert not s.execute(select(m.jobs.c.id).where(m.jobs.c.kind == "analysis")).first()

    with respx.mock(assert_all_called=False) as router:
        wp_site(router)
        ok = c.post(f"/ui/connect/{site_id}/check", data={
            "kind": "wordpress", "address": WP, "user": "redazione", "secret": "abcd efgh ijkl"})
    assert ok.status_code == 200 and "Collegamento verificato" in ok.text
    assert "Tecnologia &amp; AI" in ok.text and "Economia" in ok.text
    with db.tenant(tenant) as s:
        t = s.execute(select(m.publish_targets).where(m.publish_targets.c.site_id == site_id)).one()
        assert t.kind == "wordpress" and t.config["url"] == WP and t.config["user"] == "redazione"
        assert [x["id"] for x in t.config["categories"]] == [5, 1, 9]
        assert t.secret_env == "WP_NUOVO_EXAMPLE"
        assert s.execute(select(m.jobs.c.kind).where(m.jobs.c.kind == "analysis")).scalar() == "analysis"
        assert connect.known_categories(s, site_id) == {"Tecnologia & AI": 12, "Economia": 4}
    assert vault.get(db, tenant, "WP_NUOVO_EXAMPLE") == "abcd efgh ijkl"
    # The same site connected again updates the destination instead of adding a second one.
    with respx.mock(assert_all_called=False) as router:
        wp_site(router)
        c.post(f"/ui/connect/{site_id}/check", data={"kind": "wordpress", "address": WP, "user": "redazione",
                                                      "secret": "nuova password"})
    with db.tenant(tenant) as s:
        assert len(s.execute(select(m.publish_targets.c.id).where(m.publish_targets.c.site_id == site_id)).all()) == 1
    assert vault.get(db, tenant, "WP_NUOVO_EXAMPLE") == "nuova password"
    steps = c.get("/start").text
    assert "Sito collegato" in steps

    # A WordPress with no articles yet: nothing to read, the page asks for the context instead.
    c.post("/ui/sites", data={"domain": "vuoto.example", "back": "/start"}, follow_redirects=False)
    with db.tenant(tenant) as s:
        empty_id = s.execute(select(m.sites.c.id).where(m.sites.c.domain == "vuoto.example")).scalar()
    with respx.mock(assert_all_called=False) as router:
        wp_site(router, posts=0)
        done = c.post(f"/ui/connect/{empty_id}/check", data={
            "kind": "wordpress", "address": WP, "user": "redazione", "secret": "abcd"})
    assert "Collegamento verificato" in done.text and "Scrivi il contesto" in done.text
    with db.tenant(tenant) as s:
        assert s.execute(select(func.count()).select_from(m.jobs).where(m.jobs.c.kind == "analysis")).scalar() == 1


def test_the_analysis_starts_from_the_categories_the_site_already_has(db, tenant, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    from editor import profile as prof
    from editor.site import SiteSnapshot

    from editor import repo

    with db.tenant(tenant) as s:
        site_id = repo.add_site(s, tenant, "nuovo.example")
    pub.add_target(db, tenant, site_id, "wordpress", "Sito", {
        "url": WP, "user": "e", "categories": [{"id": 5, "name": "Tecnologia", "slug": "tecnologia", "count": 7},
                                               {"id": 1, "name": "Senza categoria", "slug": "senza-categoria", "count": 9}]}, None)

    class Crawler:
        def analyse(self, domain):
            return SiteSnapshot(domain=domain, title="Blog", description="", status="partial", language="it")

    prof.analyse_site(db, tenant, "nuovo.example", Crawler(), None)
    with db.tenant(tenant) as s:
        body = s.execute(text("SELECT body FROM editor.editorial_profiles WHERE site_id = :s ORDER BY id DESC LIMIT 1"),
                         {"s": site_id}).scalar()
    body = body if isinstance(body, dict) else json.loads(body)
    assert body["settings"]["auto"]["sections"] == ["Tecnologia"]
    assert any(x["name"] == "Tecnologia" for x in body["subtopics"]) or "Tecnologia" in json.dumps(body)


def test_github_connection_is_saved_with_what_was_found(db, tenant, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    monkeypatch.setattr(connect, "resolver", public_resolver)
    monkeypatch.setenv(vault.KEY_ENV, vault.new_key())
    c = logged_in(db, tenant, "owner")
    did, site_id = approved_draft(db, tenant)
    gh = astro_repo()
    with respx.mock(assert_all_called=False) as router:
        gh.route(router)
        ok = c.post(f"/ui/connect/{site_id}/check", data={
            "kind": "github", "address": "me/site", "secret": "github_pat_x", "host": ""})
    assert ok.status_code == 200 and "Collegamento verificato" in ok.text and "Astro" in ok.text and "Vercel" in ok.text
    with db.tenant(tenant) as s:
        t = s.execute(select(m.publish_targets).where(m.publish_targets.c.site_id == site_id,
                                                      m.publish_targets.c.kind == "github")).one()
    assert t.config["repo"] == "me/site" and t.config["path"] == "content/blog" and t.config["host"] == "vercel"
    assert t.config.get("verify") is not False and vault.get(db, tenant, t.secret_env) == "github_pat_x"


def test_connecting_later_reads_the_site_without_a_destination(db, tenant):
    c = logged_in(db, tenant, "owner")
    c.post("/ui/sites", data={"domain": "senza.example"}, follow_redirects=False)
    with db.tenant(tenant) as s:
        site_id = s.execute(select(m.sites.c.id).where(m.sites.c.domain == "senza.example")).scalar()
    r = c.post(f"/ui/connect/{site_id}/later", follow_redirects=False)
    assert r.status_code == 303
    with db.tenant(tenant) as s:
        assert s.execute(select(m.jobs.c.kind).where(m.jobs.c.kind == "analysis")).scalar() == "analysis"
        assert not s.execute(select(m.publish_targets.c.id).where(m.publish_targets.c.site_id == site_id)).first()


def test_only_owners_connect(db, tenant):
    for role in ("viewer", "editor"):
        c = logged_in(db, tenant, role)
        assert c.get("/connect").status_code == 403
        assert c.post("/ui/connect/1/later").status_code == 403


def test_the_desk_of_an_automatic_site_shows_its_day_and_does_not_ask_to_decide(db, tenant):
    from datetime import UTC, datetime, timedelta

    from editor import autopilot, profile as prof

    c = logged_in(db, tenant, "owner")
    did, site_id = approved_draft(db, tenant)
    with db.tenant(tenant) as s:
        assert prof.approved_profile(s, site_id) is not None
        autopilot.save(s, site_id, {"enabled": True, "per_day": 6})
        s.execute(text("UPDATE editor.proposals SET status = 'proposed', created_at = :t WHERE site_id = :s"),
                  {"t": datetime.now(UTC) - timedelta(hours=40), "s": site_id})
    desk = c.get("/").text
    assert "La redazione automatica di blog.example, oggi" in desk and "Oggi:" in desk
    assert "la sceglie la redazione automatica" not in desk  # it picks by itself: nothing to decide one by one
    # What it did not use lapses.
    assert autopilot.lapse(db, tenant, site_id, datetime.now(UTC)) >= 1
    with db.tenant(tenant) as s:
        assert not s.execute(text("SELECT 1 FROM editor.proposals WHERE site_id = :s AND status = 'proposed'"),
                             {"s": site_id}).first()


def test_publishing_goes_by_rest_route_where_wp_json_is_not_routed():
    page404 = httpx.Response(404, text="<h1>Not Found</h1>", headers={"content-type": "text/html"})
    with respx.mock(assert_all_called=False) as router:
        router.route(host="blog.example", path__startswith="/wp-json/").mock(return_value=page404)
        post = router.post(f"{WP}/", params__contains={"rest_route": "/wp/v2/posts"}).mock(
            return_value=httpx.Response(201, json={"id": 7, "link": f"{WP}/articolo"}))
        r = pub.Publisher(http=httpx.Client(), resolver=public_resolver)._wp("POST", WP, "wp/v2/posts", json={"title": "x"})
    assert post.called and r.json()["id"] == 7
    # A REST 404 (a post that is not there) is an answer, not a missing route.
    assert not pub.not_rest(httpx.Response(404, json={"code": "rest_post_invalid_id"}))


def test_a_section_without_its_category_gets_one_on_wordpress():
    """Francesco 2026-11-05: a new site has no categories; the sections are
    decided in Redazia and their categories made on WordPress."""
    p = pub.Publisher(http=httpx.Client(), resolver=public_resolver)
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{WP}/wp-json/wp/v2/categories").mock(return_value=httpx.Response(200, json=[]))
        made = router.post(f"{WP}/wp-json/wp/v2/categories").mock(return_value=httpx.Response(201, json={"id": 12}))
        assert p._wordpress_category(WP, ("u", "p"), "Intelligenza artificiale") == 12
    assert json.loads(made.calls[0].request.content) == {"name": "Intelligenza artificiale"}
    with respx.mock(assert_all_called=False) as router:  # already there: found, not made twice
        router.get(f"{WP}/wp-json/wp/v2/categories").mock(return_value=httpx.Response(200, json=[
            {"id": 4, "name": "Economia &amp; finanza"}]))
        made = router.post(f"{WP}/wp-json/wp/v2/categories")
        assert p._wordpress_category(WP, ("u", "p"), "Economia & finanza") == 4 and not made.called
    with respx.mock(assert_all_called=False) as router:  # not allowed: the article goes out without
        router.get(f"{WP}/wp-json/wp/v2/categories").mock(return_value=httpx.Response(200, json=[]))
        router.post(f"{WP}/wp-json/wp/v2/categories").mock(return_value=httpx.Response(
            403, json={"code": "rest_cannot_create"}))
        assert p._wordpress_category(WP, ("u", "p"), "Mondo") is None


def test_the_title_is_not_sent_twice_to_wordpress():
    """wp.aitoday.it, 2026-11-05: the theme shows the title from its field,
    and the text started with it again."""
    target = type("T", (), {"config": {"url": WP, "user": "e"}})()
    md = "# Alfabetizzazione AI a scuola\n\n_L'indagine ESEP su 454 professionisti_\n\nL'alfabetizzazione AI è poco strutturata."
    post = pub.Publisher._wordpress_post(target, {"title": "Alfabetizzazione AI a scuola", "markdown": md,
                                                  "html": "<h1>x</h1>", "subtitle": "s"}, "publish")
    assert "<h1>" not in post["content"] and "Alfabetizzazione AI a scuola" not in post["content"]
    assert "ESEP" in post["content"] and "poco strutturata" in post["content"]


def test_the_writer_files_the_article_under_a_section_and_the_director_can_change_it(db, tenant, monkeypatch):
    from editor import autopilot, drafts

    assert drafts.site_section({"settings": {"auto": {"sections": ["Strumenti AI", "Lavoro e competenze"]}}},
                               "lavoro e competenze") == "Lavoro e competenze"
    assert drafts.site_section({"settings": {"auto": {"sections": ["Strumenti AI"]}}}, "Sport") is None
    assert drafts.site_section({}, "Sport") is None
    assert '"section"' in drafts.SYSTEM

    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    did, site_id = approved_draft(db, tenant)
    with db.tenant(tenant) as s:
        body = s.execute(select(m.editorial_profiles.c.body).where(m.editorial_profiles.c.site_id == site_id,
                                                                   m.editorial_profiles.c.status == "approved")).scalar()
        autopilot.save(s, site_id, {**autopilot.settings_of(body), "sections": ["Rete", "Mercato"]})
    target = pub.add_target(db, tenant, site_id, "wordpress", "Blog", {"url": WP, "user": "editor"}, "WP_PASS")
    c = logged_in(db, tenant, "editor")
    page = c.get(f"/drafts/{did}").text
    assert 'name="section"' in page and ">Mercato</option>" in page
    r = c.post(f"/ui/drafts/{did}/publish", data={"target_id": target, "mode": "publish", "section": "mercato"},
               follow_redirects=False)
    assert r.status_code == 303
    with db.tenant(tenant) as s:
        assert s.execute(select(m.drafts.c.seo).where(m.drafts.c.id == did)).scalar()["section"] == "Mercato"
        # Already online: the section changes with «Aggiorna sul sito».
        pid = s.execute(select(m.publications.c.id).where(m.publications.c.draft_id == did)).scalar()
        s.execute(update(m.publications).where(m.publications.c.id == pid).values(
            status="published", decided_by="test", external_id="7", external_url=f"{WP}/?p=7"))
    owner = logged_in(db, tenant, "owner")
    page = owner.get(f"/drafts/{did}").text
    assert f'action="/ui/publications/{pid}/update"' in page and page.count('name="section"') == 2
    assert owner.post(f"/ui/publications/{pid}/update", data={"section": "Rete"},
                      follow_redirects=False).status_code == 303
    with db.tenant(tenant) as s:
        assert s.execute(select(m.drafts.c.seo).where(m.drafts.c.id == did)).scalar()["section"] == "Rete"
        assert s.execute(select(m.jobs.c.subject).where(m.jobs.c.kind == "change")).scalar() == f"update {pid}"


def _menu_router(router, places, menus=(), items=()):
    router.get(f"{WP}/wp-json/wp/v2/menu-locations").mock(return_value=httpx.Response(200, json=places))
    router.get(f"{WP}/wp-json/wp/v2/menus").mock(return_value=httpx.Response(200, json=list(menus)))
    router.get(f"{WP}/wp-json/wp/v2/menu-items").mock(return_value=httpx.Response(200, json=list(items)))
    router.get(f"{WP}/wp-json/wp/v2/categories").mock(return_value=httpx.Response(200, json=[
        {"id": 3, "name": "Strumenti AI"}]))
    router.post(f"{WP}/wp-json/wp/v2/categories").mock(return_value=httpx.Response(201, json={"id": 9}))


def test_the_sections_become_the_wordpress_menu(db, tenant, monkeypatch):
    """Francesco 2026-11-05: "creare anche le voci del menu dalle categorie
    scelte dal backoffice di redazia"."""
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    monkeypatch.setenv(pub.secret_variable(tenant, "WP_PASS"), "app-pass-123")
    _, site_id = approved_draft(db, tenant)
    tid = pub.add_target(db, tenant, site_id, "wordpress", "Blog", {"url": WP, "user": "admin"}, "WP_PASS")
    with db.tenant(tenant) as s:
        target = s.execute(select(m.publish_targets).where(m.publish_targets.c.id == tid)).one()
    p = pub.Publisher(resolver=public_resolver)
    with respx.mock(assert_all_called=False) as router:
        _menu_router(router, {"primary": {"name": "primary", "menu": 0}, "footer": {"name": "footer", "menu": 0}})
        made = router.post(f"{WP}/wp-json/wp/v2/menus").mock(return_value=httpx.Response(201, json={"id": 21}))
        entry = router.post(f"{WP}/wp-json/wp/v2/menu-items").mock(return_value=httpx.Response(201, json={"id": 1}))
        out = p.wordpress_menu(db, target, ["Strumenti AI", "Casi pratici"])
    assert out == {"menu": 21, "location": "primary", "added": ["Strumenti AI", "Casi pratici"], "present": 0,
                   "categories": 2, "missing": []}
    assert json.loads(made.calls[0].request.content) == {"name": pub.MENU_NAME, "locations": ["primary"]}
    sent = [json.loads(x.request.content) for x in entry.calls]
    assert [(x["object_id"], x["menus"], x["object"], x["type"]) for x in sent] == [
        (3, 21, "category", "taxonomy"), (9, 21, "category", "taxonomy")]

    # The theme's menu is already there with one of them: only the other is added.
    with respx.mock(assert_all_called=False) as router:
        _menu_router(router, {"primary": {"name": "primary", "menu": 5}},
                     items=[{"id": 40, "object": "category", "object_id": 3}])
        made = router.post(f"{WP}/wp-json/wp/v2/menus")
        entry = router.post(f"{WP}/wp-json/wp/v2/menu-items").mock(return_value=httpx.Response(201, json={"id": 2}))
        out = p.wordpress_menu(db, target, ["Strumenti AI", "Casi pratici"])
    assert out["added"] == ["Casi pratici"] and out["present"] == 1 and not made.called and entry.call_count == 1

    # An editor may not change menus, but makes the categories; a block theme has no menu places.
    with respx.mock(assert_all_called=False) as router:
        _menu_router(router, {})
        router.get(f"{WP}/wp-json/wp/v2/menu-locations").mock(return_value=httpx.Response(403, json={}))
        made = router.post(f"{WP}/wp-json/wp/v2/categories").mock(return_value=httpx.Response(201, json={"id": 9}))
        assert p.wordpress_menu(db, target, ["Strumenti AI", "Casi pratici"]) == {
            "error": "role", "categories": 2, "missing": []}
        assert json.loads(made.calls[0].request.content) == {"name": "Casi pratici"}
    with respx.mock(assert_all_called=False) as router:
        _menu_router(router, [])
        assert p.wordpress_menu(db, target, ["Strumenti AI"]) == {"error": "block_theme", "categories": 1,
                                                                 "missing": []}

    # The job reads the sections and the site's WordPress, and says what it did.
    from editor import activity, autopilot, work

    with db.tenant(tenant) as s:
        body = s.execute(select(m.editorial_profiles.c.body).where(m.editorial_profiles.c.site_id == site_id,
                                                                   m.editorial_profiles.c.status == "approved")).scalar()
        autopilot.save(s, site_id, {**autopilot.settings_of(body), "sections": ["Strumenti AI"]})
    with respx.mock(assert_all_called=False) as router:
        _menu_router(router, {"primary": {"name": "primary", "menu": 5}})
        router.post(f"{WP}/wp-json/wp/v2/menu-items").mock(return_value=httpx.Response(201, json={"id": 3}))
        res = work.handlers(db, publisher=p)["wpmenu"](tenant, f"site {site_id}", {"site_id": site_id})
    assert res["sites"][0]["added"] == ["Strumenti AI"]
    lines, _, _ = activity._result("wpmenu", res, site_id, str)
    assert "voci aggiunte: 1" in lines[0]
    lines, _, _ = activity._result("wpmenu", {"sites": [{"name": "Blog", "error": "role", "categories": 2}]},
                                   site_id, str)
    assert "categorie pronte (2)" in lines[0] and "amministratore" in lines[0]
    # A target switched off in Connect is not used by the menu job.
    with db.tenant(tenant) as s:
        s.execute(update(m.publish_targets).where(m.publish_targets.c.site_id == site_id,
                                                  m.publish_targets.c.kind == "wordpress").values(enabled=False))
    assert work.handlers(db, publisher=p)["wpmenu"](tenant, f"site {site_id}", {"site_id": site_id}) == {
        "site_id": site_id, "error": "no_wordpress"}
    with db.tenant(tenant) as s:
        s.execute(update(m.publish_targets).where(m.publish_targets.c.site_id == site_id,
                                                  m.publish_targets.c.kind == "wordpress").values(enabled=True))

    # Saving new sections queues the menu, and so does the button.
    c = logged_in(db, tenant, "owner")
    form = {"brief": "", "sections": "Strumenti AI\nCasi pratici", "per_day": 6, "min_opportunity": 40,
            "min_sources": 2, "start": 7, "end": 23, "on_problem": "fix"}
    assert c.post(f"/ui/sites/{site_id}/auto", data=form, follow_redirects=False).status_code == 303
    with db.tenant(tenant) as s:
        assert s.execute(select(func.count()).select_from(m.jobs).where(m.jobs.c.kind == "wpmenu")).scalar() == 1
    assert "Crea categorie e menu su WordPress" in c.get("/site?tab=auto").text

    # Confirming the context with its sections makes them at once, before the profile is approved.
    with db.tenant(tenant) as s:
        s.execute(m.jobs.delete().where(m.jobs.c.kind == "wpmenu"))
    assert c.post(f"/ui/sites/{site_id}/brief", data={"brief": "AI per le PMI italiane", "sections": "Casi pratici\nNorme",
                                                      "back": "/start"}, follow_redirects=False).status_code == 303
    with db.tenant(tenant) as s:
        args = s.execute(select(m.jobs.c.args).where(m.jobs.c.kind == "wpmenu")).scalars().all()
    assert args == [{"site_id": site_id, "sections": ["Casi pratici", "Norme"]}]


def test_the_connection_shows_the_recipes_folder_and_its_categories(db, tenant, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    monkeypatch.setattr(connect, "resolver", public_resolver)
    monkeypatch.setenv(vault.KEY_ENV, vault.new_key())
    c = logged_in(db, tenant, "owner")
    did, site_id = approved_draft(db, tenant)
    recipe = {"title": "Carbonara", "category": "I Primi", "ingredients": [{"quantity": "320", "unit": "g",
              "name": "spaghetti"}, {"quantity": "4", "unit": "", "name": "tuorli"}],
              "steps": [{"description": "Cuoci la pasta."}]}
    gh = astro_repo(**{"content/ricette/carbonara.json": json.dumps(recipe),
                       "content/ricette/amatriciana.json": json.dumps({**recipe, "title": "Amatriciana"})})
    with respx.mock(assert_all_called=False) as router:
        gh.route(router)
        ok = c.post(f"/ui/connect/{site_id}/check", data={
            "kind": "github", "address": "me/site", "secret": "github_pat_x", "host": ""})
    assert ok.status_code == 200 and "Cartella delle ricette" in ok.text
    assert "content/ricette" in ok.text and "I Primi" in ok.text
