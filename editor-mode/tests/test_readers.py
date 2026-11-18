"""Feedback from readers (2026-11-11): only from the site that switched it
on, confirmed by email, private to the newsroom, gone if never confirmed."""

import json
import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import text

from editor import auth, readers, repo
from editor.api import create_app
from editor.ratelimit import Limiter


def _site(db, tenant, on=True):
    domain = f"{tenant}.example"
    with db.tenant(tenant) as s:
        site_id = repo.ensure_site(s, tenant, domain, "Il Blog")
        s.execute(text("UPDATE editor.sites SET feedback = :on WHERE id = :i"), {"on": on, "i": site_id})
    return site_id, domain


def _client(db, sent, monkeypatch):
    monkeypatch.setattr(readers, "limiter", Limiter(window=3600, per_email=3, per_address=10))
    monkeypatch.delenv("EDITOR_TURNSTILE_SECRET", raising=False)
    return TestClient(create_app(db, mail=lambda to, subject, body: sent.append((to, subject, body)),
                                 public_url="https://app.test"))


def _post(c, domain, **body):
    data = {"kind": "error", "message": "La data nel secondo paragrafo è sbagliata.", "email": "Lettore@Example.org",
            "url": f"https://www.{domain}/articoli/x", "title": "Un articolo", "lang": "it"} | body
    return c.post("/api/public/feedback", content=json.dumps(data),
                  headers={"Origin": f"https://www.{domain}", "Content-Type": "text/plain"})


def test_page_url_trusts_only_the_site_and_its_subdomains():
    from editor.readers import page_url

    assert page_url("https://example.com/x", "example.com") == "https://example.com/x"
    assert page_url("https://www.example.com/x", "example.com") == "https://www.example.com/x"
    assert page_url("https://blog.example.com/x", "example.com") == "https://blog.example.com/x"
    assert page_url("https://example.com/x", "blog.example.com") == ""
    assert page_url("https://other.example/x", "blog.example.com") == ""


def test_a_message_arrives_only_after_the_email_is_confirmed(db, tenant, monkeypatch):
    site_id, domain = _site(db, tenant)
    sent = []
    c = _client(db, sent, monkeypatch)
    cfg = c.get("/api/public/feedback", headers={"Origin": f"https://{domain}"})
    assert cfg.json()["enabled"] is True and cfg.headers["access-control-allow-origin"] == f"https://{domain}"
    r = _post(c, domain)
    assert r.status_code == 202 and r.json() == {"ok": True}
    (to, subject, body), = sent
    assert to == "lettore@example.org" and "Il Blog" in subject
    link = re.search(r"https://app\.test/feedback/confirm\?t=\S+", body).group(0)
    with db.tenant(tenant) as s:
        assert s.execute(text("SELECT status, url FROM editor.reader_messages")).one() == ("pending", f"https://www.{domain}/articoli/x")
    owner = TestClient(create_app(db))
    owner.post("/login", data={"token": auth.create_token(db, tenant, "owner-user", "owner")})
    assert "La data nel secondo paragrafo" not in owner.get("/feedback").text  # not yet confirmed
    # Opening the link (a mail scanner does it too) only shows the button.
    token = link.split("t=")[1]
    page = c.get(f"/feedback/confirm?t={token}")
    assert "Conferma e invia alla redazione" in page.text and "La data nel secondo paragrafo" in page.text
    with db.tenant(tenant) as s:
        assert s.execute(text("SELECT status FROM editor.reader_messages")).scalar() == "pending"
    done = c.post("/feedback/confirm", data={"t": token})
    assert "è arrivato alla redazione" in done.text
    assert "era già confermato" in c.post("/feedback/confirm", data={"t": token}).text
    page = owner.get("/feedback").text
    assert "La data nel secondo paragrafo" in page and "lettore@example.org" in page and "Nuovo" in page
    assert '>1</span>' in page  # the count in the menu
    mid = re.search(r'id="m-(\d+)"', page).group(1)
    assert owner.post(f"/ui/feedback/{mid}", data={"status": "closed"}, follow_redirects=False).status_code == 303
    assert "La data nel secondo paragrafo" not in owner.get("/feedback").text
    assert "La data nel secondo paragrafo" in owner.get("/feedback?show=all").text


def test_other_sites_switched_off_sites_and_bad_messages_are_refused(db, tenant, monkeypatch):
    _, domain = _site(db, tenant)
    sent = []
    c = _client(db, sent, monkeypatch)
    other = c.post("/api/public/feedback", content="{}", headers={"Origin": "https://evil.example"})
    assert other.status_code == 403 and "access-control-allow-origin" not in other.headers
    assert c.get("/api/public/feedback", headers={"Origin": "https://evil.example"}).json() == {"enabled": False}
    assert _post(c, domain, email="non-una-email").json()["error"] == "email"
    assert _post(c, domain, email="a@example.org", message="corto").json()["error"] == "length"
    assert _post(c, domain, email="b@example.org", message="x" * 2001).json()["error"] == "length"
    assert _post(c, domain, website="http://spam.example").status_code == 202  # the trap: answered, dropped
    assert not sent
    assert _post(c, domain, url="https://evil.example/x").status_code == 202
    with db.tenant(tenant) as s:
        rows = s.execute(text("SELECT url, message FROM editor.reader_messages")).all()
    assert [tuple(r) for r in rows] == [("", "La data nel secondo paragrafo è sbagliata.")]  # a foreign url is not kept
    assert _post(c, domain).json()["error"] == "pending"  # one message waiting per email
    assert _post(c, domain).json()["error"] == "pending"
    assert _post(c, domain).json()["error"] == "limit"  # three tries an hour per email
    with db.tenant(tenant) as s:
        s.execute(text("UPDATE editor.sites SET feedback = :f"), {"f": False})
    assert c.get("/api/public/feedback", headers={"Origin": f"https://{domain}"}).json() == {"enabled": False}


def test_unconfirmed_messages_go_after_two_days(db, tenant, monkeypatch):
    _, domain = _site(db, tenant)
    sent = []
    c = _client(db, sent, monkeypatch)
    assert _post(c, domain).status_code == 202
    token = sent[0][2].split("t=")[1].split()[0]
    later = datetime.now(UTC) + timedelta(hours=49)
    assert readers.confirm(db, token, now=later) == "unknown"
    with db.tenant(tenant) as s:
        assert readers.prune(s, later) == 1


def test_without_mail_the_form_does_not_show(db, tenant):
    _, domain = _site(db, tenant)
    c = TestClient(create_app(db))
    assert c.get("/api/public/feedback", headers={"Origin": f"https://{domain}"}).json() == {"enabled": False}


def test_the_widget_is_served():
    from editor import api

    kind, body = api._STATIC["feedback.js"]
    assert kind.startswith("text/javascript") and b"/api/public/feedback" in body
