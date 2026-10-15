import httpx
import respx
from fastapi.testclient import TestClient

import fakesite
from editor import auth, repo
from editor.api import create_app
from test_proposals import setup_site
from editor import opportunity as opp, proposals as props


def client_for(db, tenant, role="owner", http_factory=None):
    token = auth.create_token(db, tenant, f"{role}-user", role)
    app = create_app(db, http_factory=http_factory)
    return TestClient(app), {"Authorization": f"Bearer {token}"}, token


def test_tokens_scope_every_route_to_one_tenant(db, tenant):
    site_id = setup_site(db, tenant)
    other = f"{tenant}-other"
    with db.tenant(other) as s:
        repo.ensure_tenant(s, other, other)
    c, h, _ = client_for(db, tenant)
    c2, h2, _ = client_for(db, other)
    assert c.get("/api/sites").status_code == 401
    assert c.get("/api/sites", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert [s["domain"] for s in c.get("/api/sites", headers=h).json()["sites"]] == ["blog.example"]
    assert c2.get("/api/sites", headers=h2).json()["sites"] == []
    assert c2.get(f"/api/sites/{site_id}/profiles", headers=h2).status_code == 404
    assert c2.post(f"/api/sites/{site_id}/profiles/1/approve", headers=h2).status_code == 404
    assert c2.get("/api/topics", headers=h2).json()["topics"] == []


def test_roles(db, tenant):
    site_id = setup_site(db, tenant)
    ops = opp.score_site(db, tenant, site_id)
    pid = props.propose(db, tenant, site_id, ops)[0]
    viewer, hv, _ = client_for(db, tenant, "viewer")
    editor, he, _ = client_for(db, tenant, "editor")
    assert viewer.post(f"/api/proposals/{pid}/decision", json={"accept": True}, headers=hv).status_code == 403
    assert editor.post("/api/sites", json={"domain": "x.example"}, headers=he).status_code == 403
    r = editor.post(f"/api/proposals/{pid}/decision", json={"accept": True}, headers=he)
    assert r.status_code == 200 and r.json() == {"status": "accepted"}
    assert editor.post(f"/api/proposals/{pid}/decision", json={"accept": False}, headers=he).status_code == 409
    listed = viewer.get("/api/proposals?status=accepted", headers=hv).json()["proposals"]
    assert listed[0]["id"] == pid and listed[0]["citations"]


def test_add_site_analyses_and_discovers(db, tenant):
    with respx.mock(assert_all_called=False) as router:
        fakesite.mount(router)
        router.get(url__regex=r"https://(?!blog\.example).*").mock(return_value=httpx.Response(404))
        c, h, _ = client_for(db, tenant, http_factory=httpx.Client)
        assert c.post("/api/sites", json={"domain": "not a domain"}, headers=h).status_code == 400
        r = c.post("/api/sites", json={"domain": "https://www.Blog.Example/path"}, headers=h)
        assert r.status_code == 202 and r.json()["domain"] == "blog.example"
    sites = c.get("/api/sites", headers=h).json()["sites"]
    assert sites[0]["last_analysis"]["status"] == "partial"
    assert sites[0]["profiles"] == [{"version": 1, "status": "draft", "origin": "analysis"}]
    sid = sites[0]["id"]
    assert c.post(f"/api/sites/{sid}/profiles/1/approve", headers=h).json() == {"approved": 1}
    body = c.get(f"/api/sites/{sid}/profiles", headers=h).json()["profiles"][0]["body"]
    body["settings"]["exclude"] = ["gaming"]
    r = c.put(f"/api/sites/{sid}/profile", json=body, headers=h)
    assert r.json() == {"draft_version": 2}
    # A malformed edit is refused before it is stored, not kept to break
    # the dashboard and the drafts later.
    for bad in ({"subtopics": ["fibra"]},
                {"subtopics": [{"keywords": ["x"]}]},
                {"subtopics": [], "settings": {"min_relevance": 5}},
                {"subtopics": [], "style": "formale"}):
        r = c.put(f"/api/sites/{sid}/profile", json=bad, headers=h)
        assert r.status_code == 400, bad
        assert "invalid profile" in r.json()["error"]
    assert c.get("/site", headers=h).status_code == 200


def test_dashboard_login_and_csrf(db, tenant):
    setup_site(db, tenant)
    c, _, token = client_for(db, tenant)
    assert c.get("/", follow_redirects=False).status_code == 303
    assert c.post("/login", data={"token": "wrong"}).status_code == 401
    r = c.post("/login", data={"token": token}, follow_redirects=False)
    assert r.status_code == 303 and "httponly" in r.headers["set-cookie"].lower()
    page = c.get("/")
    assert page.status_code == 200 and "blog.example" in page.text and "Approva" not in page.text
    # A form posted from another site is refused even with the cookie.
    evil = c.post("/ui/sites", data={"domain": "x.example"}, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403


def test_draft_routes(db, tenant):
    from test_drafts import accepted_proposal, chat
    import json as _json
    from editor.core_client import CoreClient

    pid, cited = accepted_proposal(db, tenant)
    answer = _json.dumps({"title": "Bari in fibra", "sections": [{"heading": None, "claims": [
        {"text": "Open Fiber ha lavorato a Bari.", "sources": [cited[0]]},
        {"text": "La rete in fibra è completa.", "sources": [cited[0]]},
        {"text": "I lavori sono finiti.", "sources": [cited[0]]}]}]})
    token = auth.create_token(db, tenant, "ed", "editor")
    h = {"Authorization": f"Bearer {token}"}
    with respx.mock(assert_all_called=False) as router:
        router.post("http://core.test/v1/llm/chat").mock(return_value=chat(answer))
        c = TestClient(create_app(db, http_factory=None, core=CoreClient("http://core.test", "tok")))
        assert c.post(f"/api/proposals/{pid}/draft", headers=h).status_code == 202
    listed = c.get("/api/drafts", headers=h).json()["drafts"]
    assert len(listed) == 1
    d = c.get(f"/api/drafts/{listed[0]['id']}", headers=h).json()
    assert len(d["claims"]) == 3 and all(x["sources"] == [cited[0]] for x in d["claims"])
    assert c.post(f"/api/drafts/{d['id']}/decision", json={"accept": True}, headers=h).json() == {"status": "approved"}


def test_publication_routes_need_an_owner_decision(db, tenant, monkeypatch):
    import socket
    from editor import publishing
    from test_publishing import approved_draft, public_resolver

    did, site_id = approved_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    monkeypatch.setenv(publishing.secret_variable(tenant, "HOOK_SECRET"), "s")
    owner = auth.create_token(db, tenant, "owner", "owner")
    editor = auth.create_token(db, tenant, "ed", "editor")
    ho, he = {"Authorization": f"Bearer {owner}"}, {"Authorization": f"Bearer {editor}"}
    c = TestClient(create_app(db, publisher=publishing.Publisher(resolver=public_resolver)))
    assert c.post("/api/targets", json={"site_id": site_id, "kind": "webhook", "name": "h",
                                        "config": {"url": "https://hooks.example/in"}, "secret_env": "HOOK_SECRET"}, headers=he).status_code == 403
    tid = c.post("/api/targets", json={"site_id": site_id, "kind": "webhook", "name": "h",
                                       "config": {"url": "https://hooks.example/in"}, "secret_env": "HOOK_SECRET"}, headers=ho).json()["id"]
    pid = c.post(f"/api/drafts/{did}/publications", json={"target_id": tid, "mode": "publish"}, headers=he).json()["id"]
    assert c.post(f"/api/drafts/{did}/publications", json={"target_id": tid, "mode": "publish"}, headers=he).status_code == 409
    assert c.post(f"/api/publications/{pid}/decision", json={"accept": True}, headers=he).status_code == 403
    with respx.mock() as router:
        hook = router.post("https://hooks.example/in").mock(return_value=httpx.Response(200))
        assert c.post(f"/api/publications/{pid}/decision", json={"accept": True}, headers=ho).status_code == 200
        assert hook.called
    pubs = c.get("/api/publications", headers=he).json()["publications"]
    assert pubs[0]["status"] == "published" and pubs[0]["decided_by"] == "owner"
