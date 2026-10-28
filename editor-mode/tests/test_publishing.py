import hashlib
import hmac
import json
import socket

import httpx
import pytest
import respx
from sqlalchemy import text

from editor import drafts, publishing as pub
from editor.markdown import to_html
from test_drafts import accepted_proposal, chat
from editor.core_client import CoreClient


_real_getaddrinfo = socket.getaddrinfo
LOCAL = ("localhost", "127.0.0.1", "::1")


def _fake(ip):
    # Patched over socket.getaddrinfo: the test database (localhost) still
    # resolves for real, every other host gets the fixed address.
    def resolve(host, port, *args, **kwargs):
        if host in LOCAL:
            return _real_getaddrinfo(host, port, *args, **kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    return resolve


public_resolver = _fake("93.184.216.34")
private_resolver = _fake("10.0.0.5")


def approved_draft(db, tenant):
    pid, cited = accepted_proposal(db, tenant)
    answer = json.dumps({"title": "Bari in fibra", "subtitle": "La rete è completa", "sections": [{"heading": "Fatti", "claims": [
        {"text": "Open Fiber ha lavorato a Bari.", "sources": [cited[0]]},
        {"text": "La rete in fibra è completa.", "sources": [cited[0]]},
        {"text": "I lavori sono finiti.", "sources": [cited[0]]}]}]})
    with respx.mock() as router:
        router.post("http://core.test/v1/llm/chat").mock(return_value=chat(answer))
        did = drafts.write_draft(db, tenant, pid, CoreClient("http://core.test", "tok"), None)
    drafts.decide(db, tenant, did, True, "francesco")
    with db.tenant(tenant) as s:
        site_id = s.execute(text("SELECT site_id FROM editor.drafts WHERE id = :d"), {"d": did}).scalar()
    return did, site_id


def test_markdown_rendering_escapes():
    page = to_html("# T <b>\n\n_sub_\n\n## H\n\nTesto[^1] <script>x</script>\n\n[^1]: [Fonte](https://a.example/x), Nome")
    assert "<h1>T &lt;b&gt;</h1>" in page and "<em>sub</em>" in page and "<script>" not in page
    assert '<sup><a href="#fn1">1</a></sup>' in page and '<li id="fn1"><a href="https://a.example/x"' in page


def test_targets_must_be_public_and_secret_free(db, tenant, monkeypatch):
    did, site_id = approved_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", private_resolver)
    with pytest.raises(pub.PublishError, match="non-public"):
        pub.add_target(db, tenant, site_id, "wordpress", "wp", {"url": "https://intranet.example", "user": "a"}, "WP_PASS")
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    with pytest.raises(pub.PublishError, match="https"):
        pub.add_target(db, tenant, site_id, "webhook", "hook", {"url": "http://hooks.example/x"}, "HOOK")
    with pytest.raises(pub.PublishError, match="stored apart"):
        pub.add_target(db, tenant, site_id, "wordpress", "wp", {"url": "https://blog.example", "user": "a", "password": "x"}, None)


def test_a_tenant_cannot_name_server_or_other_tenants_variables(db, tenant, monkeypatch):
    did, site_id = approved_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    monkeypatch.setenv("EDITOR_DATABASE_URL", "postgresql://app:pw@db/editor")
    monkeypatch.setenv("WP_PASS", "server-wide")
    for bad in ("lower", "_X", "A__B", "X_", "A-B"):
        with pytest.raises(pub.PublishError, match="secret name"):
            pub.add_target(db, tenant, site_id, "webhook", "h", {"url": "https://hooks.example/in"}, bad)
    # A plain name is always read inside the tenant's own namespace.
    target = pub.add_target(db, tenant, site_id, "wordpress", "wp", {"url": "https://evil.example", "user": "a"},
                            "EDITOR_DATABASE_URL")
    pid = pub.request(db, tenant, did, target, "draft", "redazione")
    pub.decide(db, tenant, pid, True, "francesco")
    with respx.mock(assert_all_called=False) as router:
        wp = router.post("https://evil.example/wp-json/wp/v2/posts").mock(return_value=httpx.Response(201, json={"id": 1}))
        out = pub.Publisher(resolver=public_resolver).run(db, tenant, pid)
    assert out["status"] == "failed" and not wp.called
    assert pub.secret_variable(tenant, "EDITOR_DATABASE_URL") in out["error"]
    assert pub.secret_variable("a-b", "X") != pub.secret_variable("a", "B_X")
    with pytest.raises(pub.PublishError, match="tenant id"):
        pub.secret_variable("a_b", "X")


def test_nothing_leaves_without_approval(db, tenant, monkeypatch):
    did, site_id = approved_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    monkeypatch.setenv(pub.secret_variable(tenant, "WP_PASS"), "app-pass-123")
    target = pub.add_target(db, tenant, site_id, "wordpress", "Blog", {"url": "https://blog.example", "user": "editor"}, "WP_PASS")
    pid = pub.request(db, tenant, did, target, "draft", "redazione")
    publisher = pub.Publisher(resolver=public_resolver)
    with respx.mock() as router:
        wp = router.post("https://blog.example/wp-json/wp/v2/posts").mock(
            return_value=httpx.Response(201, json={"id": 77, "link": "https://blog.example/?p=77"}))
        with pytest.raises(pub.PublishError, match="approval"):
            publisher.run(db, tenant, pid)
        assert not wp.called
        assert pub.decide(db, tenant, pid, True, "francesco")
        assert not pub.decide(db, tenant, pid, False, "francesco")  # decided once
        assert publisher.run(db, tenant, pid) == {"status": "published", "url": "https://blog.example/?p=77"}
        assert publisher.run(db, tenant, pid)["status"] == "published" and wp.call_count == 1  # idempotent
        sent = json.loads(wp.calls[0].request.content)
        assert sent["status"] == "draft" and sent["title"] == "Bari in fibra" and "<h2>Fatti</h2>" in sent["content"]
        assert wp.calls[0].request.headers["authorization"].startswith("Basic ")
    with pytest.raises(Exception):  # the same publication cannot be requested twice
        pub.request(db, tenant, did, target, "draft", "redazione")


def test_webhook_is_signed_and_telegram_token_is_not_logged(db, tenant, monkeypatch):
    did, site_id = approved_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    monkeypatch.setenv(pub.secret_variable(tenant, "HOOK_SECRET"), "s3cret")
    monkeypatch.setenv(pub.secret_variable(tenant, "TG_TOKEN"), "123:ABC")
    hook = pub.add_target(db, tenant, site_id, "webhook", "social", {"url": "https://hooks.example/in"}, "HOOK_SECRET")
    tg = pub.add_target(db, tenant, site_id, "telegram_channel", "canale", {"chat_id": "@canale"}, "TG_TOKEN")
    publisher = pub.Publisher(resolver=public_resolver)
    with respx.mock() as router:
        route = router.post("https://hooks.example/in").mock(return_value=httpx.Response(200))
        router.post("https://api.telegram.org/bot123:ABC/sendMessage").mock(return_value=httpx.Response(500))
        p1 = pub.request(db, tenant, did, hook, "publish", "redazione")
        p2 = pub.request(db, tenant, did, tg, "publish", "redazione")
        for p in (p1, p2):
            pub.decide(db, tenant, p, True, "francesco")
        assert publisher.run(db, tenant, p1)["status"] == "published"
        failed = publisher.run(db, tenant, p2)
    req = route.calls[0].request
    expected = hmac.new(b"s3cret", req.content, hashlib.sha256).hexdigest()
    assert req.headers["x-editor-signature"] == f"sha256={expected}"
    assert failed["status"] == "failed" and "123:ABC" not in failed["error"]
    with db.tenant(tenant) as s:
        err = s.execute(text("SELECT error FROM editor.publications WHERE id = :p"), {"p": p2}).scalar()
    assert "123:ABC" not in err and "***" in err


def test_unapproved_drafts_cannot_be_queued(db, tenant, monkeypatch):
    did, site_id = approved_draft(db, tenant)  # status 'draft', not approved yet
    with db.tenant(tenant) as s:
        s.execute(text("UPDATE editor.drafts SET status = 'draft' WHERE id = :d"), {"d": did})
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    target = pub.add_target(db, tenant, site_id, "webhook", "h", {"url": "https://hooks.example/in"}, "X")
    with pytest.raises(pub.PublishError, match="approved drafts"):
        pub.request(db, tenant, did, target, "draft", "redazione")


def test_a_send_cut_short_is_never_repeated_on_its_own(db, tenant, monkeypatch):
    did, site_id = approved_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    monkeypatch.setenv(pub.secret_variable(tenant, "HOOK"), "s")
    target = pub.add_target(db, tenant, site_id, "webhook", "h", {"url": "https://hooks.example/in"}, "HOOK")
    pid = pub.request(db, tenant, did, target, "publish", "redazione")
    pub.decide(db, tenant, pid, True, "francesco")
    # A worker died after claiming the send: the outcome is unknown.
    with db.tenant(tenant) as s:
        s.execute(text("UPDATE editor.publications SET status = 'sending' WHERE id = :p"), {"p": pid})
    with respx.mock(assert_all_called=False) as router:
        hook = router.post("https://hooks.example/in").mock(return_value=httpx.Response(200))
        assert pub.Publisher(resolver=public_resolver).run(db, tenant, pid) == {"status": "sending"}
    assert not hook.called
    with pytest.raises(Exception):  # nor can it be requested again while uncertain
        pub.request(db, tenant, did, target, "publish", "redazione")
    # A person checks the CMS, finds nothing, and marks it failed: now it can be requested again.
    assert pub.mark_uncertain_failed(db, tenant, pid, "francesco")
    assert not pub.mark_uncertain_failed(db, tenant, pid, "francesco")
    assert pub.request(db, tenant, did, target, "publish", "redazione") != pid


def test_github_target_needs_a_repository(db, tenant, monkeypatch):
    did, site_id = approved_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    with pytest.raises(pub.PublishError, match="owner/name"):
        pub.add_target(db, tenant, site_id, "github", "sito", {"repo": "non-un-repo"}, "GH")
    with pytest.raises(pub.PublishError, match=r"cannot be \.\."):
        pub.add_target(db, tenant, site_id, "github", "sito", {"repo": "me/site", "path": "a/../b"}, "GH")
    cfg = pub.github_config({"repo": " Me/Sito.it ", "branch": "/main/", "path": "", "image_path": "public/img"})
    assert cfg == {"repo": "Me/Sito.it", "branch": "main", "image_path": "public/img"}
    assert pub.site_path("public/img/a.jpg") == "/img/a.jpg" and pub.site_path("static/a.png") == "/a.png"
    assert pub.site_path("assets/a.jpg") == "/assets/a.jpg"


ASTRO_POST = """---
title: 'Vecchio articolo'
description: 'Una descrizione'
pubDate: 'Jul 08 2026'
heroImage: '/blog/vecchio.jpg'
author: 'Redazione I3K'
tags: ["ai"]
draft: false
---

Testo.
"""

ASTRO_POST_2 = ASTRO_POST.replace("Vecchio articolo", "Altro").replace("Jul 08 2026", "Jun 01 2026").replace(
    "/blog/vecchio.jpg", "/blog/altro.jpg")


def _target(db, tenant, site_id, monkeypatch, **config):
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    monkeypatch.setenv(pub.secret_variable(tenant, "GH"), "ghp_test123")
    return pub.add_target(db, tenant, site_id, "github", "sito", {"repo": "me/site", **config}, "GH")


def _send(db, tenant, did, target, mode, gh, publisher=None):
    from github_fake import FakeGitHub  # noqa: F401
    pid = pub.request(db, tenant, did, target, mode, "redazione")
    pub.decide(db, tenant, pid, True, "francesco")
    publisher = publisher or pub.Publisher(resolver=public_resolver, sleep=lambda s: None)
    with respx.mock(assert_all_called=False) as router:
        gh.route(router)
        out = publisher.run(db, tenant, pid)
    return pid, out


def test_github_target_options(db, tenant, monkeypatch):
    did, site_id = approved_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    with pytest.raises(pub.PublishError, match="owner/name"):
        pub.add_target(db, tenant, site_id, "github", "sito", {"repo": "non-un-repo"}, "GH")
    with pytest.raises(pub.PublishError, match=r"cannot be \.\."):
        pub.add_target(db, tenant, site_id, "github", "sito", {"repo": "me/site", "path": "a/../b"}, "GH")
    with pytest.raises(pub.PublishError, match="slug"):
        pub.github_config({"repo": "me/site", "url": "https://sito.it/blog/"})
    cfg = pub.github_config({"repo": " Me/Sito.it ", "branch": "/main/", "path": "", "image_path": "public/img",
                             "url": "https://sito.it/blog/{slug}/", "verify": False, "wait_minutes": "90"})
    assert cfg == {"repo": "Me/Sito.it", "branch": "main", "image_path": "public/img",
                   "url": "https://sito.it/blog/{slug}/", "verify": False, "wait_minutes": 60}
    assert pub.github_config({"repo": "me/site", "verify": True}) == {"repo": "me/site"}
    assert pub.site_path("public/img/a.jpg") == "/img/a.jpg" and pub.site_path("static/a.png") == "/a.png"
    assert pub.site_path("assets/a.jpg") == "/assets/a.jpg"


def test_github_draft_opens_a_pull_request_only(db, tenant, monkeypatch):
    from github_fake import FakeGitHub
    did, site_id = approved_draft(db, tenant)
    target = _target(db, tenant, site_id, monkeypatch)
    gh = FakeGitHub({"README.md": "sito"})
    pid, out = _send(db, tenant, did, target, "draft", gh)
    assert out == {"status": "published", "url": "https://github.com/me/site/pull/1"}
    assert gh.token == "Bearer ghp_test123"
    assert gh.pulls[0]["state"] == "open" and "content/blog/bari-in-fibra.md" not in gh.files()
    md = gh.text("content/blog/bari-in-fibra.md", f"editor/publish-{pid}")
    assert md.startswith("---\n") and 'title: "Bari in fibra"' in md and "\nimage:" not in md
    assert "# Bari in fibra" not in md and "Open Fiber ha lavorato a Bari." in md  # the title is in the front matter


def test_github_publish_copies_the_blog_and_merges_after_the_checks(db, tenant, monkeypatch):
    """An Astro blog: front matter keys, date format, image place and the
    author shared by every post are copied; llms.txt and the static sitemap
    get the article; everything is one commit, merged once Vercel's preview
    deploy is green."""
    from github_fake import FakeGitHub
    did, site_id = approved_draft(db, tenant)
    with db.tenant(tenant) as s:
        s.execute(text("INSERT INTO editor.draft_images (tenant_id, draft_id, token, prompt, alt, model, mime, width,"
                       " height, content) VALUES (:t, :d, 'tok', 'p', 'Una strada cablata', 'm', 'image/jpeg', 10, 10,"
                       " :c)"), {"t": tenant, "d": did, "c": b"\xff\xd8jpeg"})
    target = _target(db, tenant, site_id, monkeypatch, path="src/content/blog")
    gh = FakeGitHub({
        "src/content/blog/vecchio-articolo.md": ASTRO_POST, "src/content/blog/altro.md": ASTRO_POST_2,
        "public/llms.txt": "# Sito\n\n## Blog\n\n- [Vecchio articolo](https://sito.it/blog/vecchio-articolo/): x\n\n## Altro\n\n- [Chi siamo](https://sito.it/chi/)\n",
        "public/sitemap.xml": '<?xml version="1.0"?>\n<urlset>\n  <url><loc>https://sito.it/</loc></url>\n</urlset>\n'})
    gh.checks["*"] = ["pending", "pending", "success"]
    before = len(gh.commits)
    pid, out = _send(db, tenant, did, target, "publish", gh)
    assert out["status"] == "published", out
    assert out["url"] == "https://sito.it/blog/bari-in-fibra/"
    md = gh.text("src/content/blog/bari-in-fibra.md")
    fm = md.split("---\n")[1]
    assert fm.splitlines()[0] == 'title: "Bari in fibra"'
    assert "\npubDate: '" in fm and "heroImage: \"/blog/bari-in-fibra.jpg\"" in fm
    assert "author: 'Redazione I3K'" in fm and "draft: false" in fm
    assert gh.files()["public/blog/bari-in-fibra.jpg"] == b"\xff\xd8jpeg"
    llms = gh.text("public/llms.txt")
    assert llms.index("bari-in-fibra") < llms.index("## Altro")
    assert "<loc>https://sito.it/blog/bari-in-fibra/</loc>" in gh.text("public/sitemap.xml")
    # one commit with every file, then the merge
    assert len(gh.commits) == before + 2 and gh.pulls[0]["state"] == "merged"
    assert f"editor/publish-{pid}" not in gh.branches


def test_github_failed_checks_leave_the_site_as_it_was(db, tenant, monkeypatch):
    from github_fake import FakeGitHub
    did, site_id = approved_draft(db, tenant)
    target = _target(db, tenant, site_id, monkeypatch)
    gh = FakeGitHub({"README.md": "sito"})
    gh.checks["*"] = ["pending", "failure"]
    pid, out = _send(db, tenant, did, target, "publish", gh)
    assert out["status"] == "failed" and "https://vercel.com/me/site/dpl_1" in out["error"]
    assert "content/blog/bari-in-fibra.md" not in gh.files() and gh.pulls[0]["state"] == "open"


def test_github_without_checks_merges_and_a_repeated_send_is_safe(db, tenant, monkeypatch):
    from github_fake import FakeGitHub
    did, site_id = approved_draft(db, tenant)
    target = _target(db, tenant, site_id, monkeypatch, branch="prod")
    gh = FakeGitHub({"README.md": "sito"}, default="prod")
    clock = iter(range(0, 10_000, 30))
    publisher = pub.Publisher(resolver=public_resolver, sleep=lambda s: None)
    import editor.sitegit as sg
    monkeypatch.setattr(sg.time, "monotonic", lambda: next(clock))
    pid, out = _send(db, tenant, did, target, "publish", gh, publisher)
    assert out["status"] == "published" and "content/blog/bari-in-fibra.md" in gh.files("prod")
    # Sent again (outcome was unknown): the same article is there, nothing new is written.
    commits = len(gh.commits)
    with db.tenant(tenant) as s:
        s.execute(text("UPDATE editor.publications SET status = 'approved' WHERE id = :p"), {"p": pid})
    with respx.mock(assert_all_called=False) as router:
        gh.route(router)
        out = publisher.run(db, tenant, pid)
    assert out["status"] == "published" and len(gh.commits) == commits
    # Other content in the same file is refused, not overwritten.
    gh.trees[gh.commits[gh.branches["prod"]]["tree"]]["content/blog/bari-in-fibra.md"] = b"altro"
    with db.tenant(tenant) as s:
        s.execute(text("UPDATE editor.publications SET status = 'approved' WHERE id = :p"), {"p": pid})
    with respx.mock(assert_all_called=False) as router:
        gh.route(router)
        out = publisher.run(db, tenant, pid)
    assert out["status"] == "failed" and "other content" in out["error"]


def test_github_folder_posts_mdx_and_image_beside_the_post(db, tenant, monkeypatch):
    from github_fake import FakeGitHub
    did, site_id = approved_draft(db, tenant)
    with db.tenant(tenant) as s:
        s.execute(text("INSERT INTO editor.draft_images (tenant_id, draft_id, token, prompt, alt, model, mime, width,"
                       " height, content) VALUES (:t, :d, 'tok', 'p', 'alt', 'm', 'image/png', 10, 10, :c)"),
                  {"t": tenant, "d": did, "c": b"\x89PNG..."})
    with db.tenant(tenant) as s:
        s.execute(text("UPDATE editor.drafts SET body_md = body_md || :x WHERE id = :d"),
                  {"d": did, "x": "\n\nCosto {stimato} < 5 euro"})
    target = _target(db, tenant, site_id, monkeypatch, verify=False, url="https://sito.it/news/{slug}")
    post = "---\ntitle: Primo\ndate: 2026-10-01\ncover:\n  src: ./cover.jpg\n  alt: Una foto\n---\n\nCiao\n"
    gh = FakeGitHub({"content/blog/primo/index.mdx": post, "content/blog/primo/cover.jpg": b"x"})
    pid, out = _send(db, tenant, did, target, "publish", gh)
    assert out == {"status": "published", "url": "https://sito.it/news/bari-in-fibra"}
    files = gh.files()
    assert files["content/blog/bari-in-fibra/cover.png"] == b"\x89PNG..."
    md = files["content/blog/bari-in-fibra/index.mdx"].decode()
    assert "cover:\n  src: \"./cover.png\"\n  alt: \"alt\"" in md and "\ndate: 20" in md
    assert "Costo \\{stimato\\} &lt; 5 euro" in md
    assert not gh.pulls  # verify: false goes straight on the branch


def test_sitegit_formats():
    from editor import sitegit as sg
    from datetime import datetime
    assert sg.format_date(datetime(2026, 7, 8), ("human", "'")) == "'Jul 08 2026'"
    assert sg.format_date(datetime(2026, 7, 8), ("date", "")) == "2026-07-08"
    keys = sg.parse_front_matter("title: A\ntags:\n  - x\n  - y\nimage:\n  url: /a.jpg\n")
    assert [k for k, _ in keys] == ["title", "tags", "image"] and sg.nested(keys[2][1]) == {"url": "/a.jpg"}
    assert sg.add_to_llms("# S\n", "T", "https://s/b/t", None, None).endswith("## Blog\n\n- [T](https://s/b/t)\n")
    assert sg.add_to_llms("- [T](https://s/b/t)\n", "T", "https://s/b/t", None, None) is None
    assert sg.add_to_sitemap("<sitemapindex></sitemapindex>", "https://s/x", datetime(2026, 1, 1)) is None
    fmt = sg.learn([sg.Post("content/posts/a.md", [("title", "A"), ("date", "2026-01-01")], "a")])
    assert sg.guess_url(fmt, "content/posts", "s.it", []) == "https://s.it/posts/{slug}"
    assert sg.guess_url(fmt, "content/posts", "s.it", ["- [A](https://s.it/articoli/aaaa/)"]) == "https://s.it/posts/{slug}"
    fmt.samples[0].slug = "aaaa"
    assert sg.guess_url(fmt, "content/posts", "s.it", ["- [A](https://s.it/articoli/aaaa/)"]) == "https://s.it/articoli/{slug}/"
