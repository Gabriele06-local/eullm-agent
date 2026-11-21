"""The weekly report, Google Search Console and the guided path."""

import base64
import json
import re
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text

from editor import opportunity, searchconsole, weekly
from editor.config import Settings
from editor.db import Database
from test_publishing import approved_draft
from test_ui import logged_in


@pytest.fixture
def admin(db):
    a = Database.from_url(db.admin_url)
    yield a
    a.engine.dispose()


@pytest.fixture
def key():
    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = pk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()
    return {"type": "service_account", "client_email": "redazia@progetto.iam.gserviceaccount.com",
            "private_key": pem, "token_uri": "https://oauth2.googleapis.com/token"}


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def mail_settings(**kw):
    return Settings(smtp_host="smtp.test", mail_from="noreply@redazia.com", public_url="https://app.redazia.com", **kw)


# --- Search Console ----------------------------------------------------------------


def test_properties_keys_and_matching(key):
    assert searchconsole.normalise_property("Example.it") == "sc-domain:example.it"
    assert searchconsole.normalise_property("https://example.it") == "https://example.it/"
    assert searchconsole.normalise_property("sc-domain:Example.it/") == "sc-domain:example.it"
    assert searchconsole.normalise_property("  ") == ""
    raw = json.dumps(key)
    assert searchconsole.load_key({"EDITOR_GSC_KEY": raw}).client_email == key["client_email"]
    assert searchconsole.load_key({"EDITOR_GSC_KEY": base64.b64encode(raw.encode()).decode()}) is not None
    assert searchconsole.load_key({"EDITOR_GSC_KEY": "rubbish"}) is None
    assert searchconsole.load_key({}) is None
    queries = [(frozenset({"fibra", "bari"}), 100.0), (frozenset({"pizza"}), 10.0)]
    assert searchconsole.match({"fibra", "bari", "open"}, queries) == 1.0
    assert 0 < searchconsole.match({"fibra"}, queries) < 1
    assert searchconsole.match({"calcio"}, queries) == 0.0
    assert searchconsole.match({"fibra"}, []) == 0.0


def test_search_is_a_bonus_not_a_weight():
    now = datetime.now(UTC)
    args = dict(topic_id=1, topic_terms={"fibra"}, titles=["Fibra a Bari"],
                profile={"subtopics": [{"name": "Fibra", "keywords": ["fibra"]}]}, hype=50, newest=now, history=[],
                proposed_recently=False, feedback=[], source_scores=[0.7], now=now)
    base = opportunity.opportunity(**args)
    assert "search" not in base.components
    same = opportunity.opportunity(**args, search=0.0)
    assert same.score == base.score
    more = opportunity.opportunity(**args, search=1.0)
    assert more.score == min(100, round(base.score + opportunity.SEARCH_BONUS, 2)) and more.components["search"] == 1.0


def _google(router, rows_page, rows_query, level="siteOwner"):
    router.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "ya29.x", "expires_in": 3600}))
    router.get(url__regex=r"https://www\.googleapis\.com/webmasters/v3/sites/[^/]+$").mock(
        return_value=httpx.Response(200, json={"permissionLevel": level}))

    def query(request):
        dims = json.loads(request.content)["dimensions"]
        assert request.headers["authorization"] == "Bearer ya29.x"
        return httpx.Response(200, json={"rows": rows_page if dims[1] == "page" else rows_query})
    router.post(url__regex=r".*/searchAnalytics/query$").mock(side_effect=query)


def test_position_averages_only_rows_that_have_a_position(db, tenant):
    _, site_id = approved_draft(db, tenant)
    start, end = date(2026, 10, 1), date(2026, 10, 31)
    with db.tenant(tenant) as s:
        s.execute(text("INSERT INTO editor.search_stats (tenant_id, site_id, day, kind, key, clicks, impressions, position) "
                       "VALUES (:t, :s, '2026-10-10', 'page', 'a', 2, 100, 5.0), "
                       "(:t, :s, '2026-10-11', 'page', 'b', 0, 100, NULL), "
                       "(:t, :s, '2026-10-12', 'query', 'fibra', 1, 50, 4.0)"), {"t": tenant, "s": site_id})
        tot = searchconsole.totals(s, site_id, start, end)
        top = searchconsole.top(s, site_id, "page", start, end)
    assert tot["clicks"] == 2 and tot["impressions"] == 200 and tot["position"] == 5.0
    assert top[0]["position"] == 5.0


def test_connect_read_and_disconnect(db, tenant, key):
    _, site_id = approved_draft(db, tenant)
    client = searchconsole.Client(searchconsole.Key(key["client_email"], key["private_key"]))
    yesterday = (date.today() - timedelta(days=2)).isoformat()
    with respx.mock() as router:
        _google(router, [{"keys": [yesterday, "https://blog.example/fibra"], "clicks": 12, "impressions": 300, "position": 4.2}],
                [{"keys": [yesterday, "fibra bari"], "clicks": 9, "impressions": 120, "position": 3.1}])
        assert searchconsole.set_property(db, tenant, site_id, "blog.example", client) == "ok"
        assert searchconsole.sync(db, tenant, client)[site_id] == 2
        # Read again: the same days are replaced, not added.
        assert searchconsole.sync(db, tenant, client)[site_id] == 2
    with db.tenant(tenant) as s:
        assert s.execute(text("SELECT gsc_property FROM editor.sites WHERE id = :i"), {"i": site_id}).scalar() == "sc-domain:blog.example"
        tot = searchconsole.totals(s, site_id, date.today() - timedelta(days=7), date.today())
        assert tot == {"clicks": 12, "impressions": 300, "position": 4.2}
        assert searchconsole.top(s, site_id, "query", date.today() - timedelta(days=7), date.today())[0]["key"] == "fibra bari"
        assert searchconsole.demand(s, site_id)[0][0] == frozenset({"fibra", "bari"})
    with respx.mock(assert_all_called=False) as router:
        _google(router, [], [], level="siteUnverifiedUser")
        assert searchconsole.set_property(db, tenant, site_id, "sc-domain:altro.example", client) == "no_access"
    assert searchconsole.set_property(db, tenant, site_id, "", client) is None
    with db.tenant(tenant) as s:
        assert s.execute(text("SELECT count(*) FROM editor.search_stats")).scalar() == 0


# --- the weekly report -----------------------------------------------------------


def _published(db, tenant, did, site_id, when):
    with db.tenant(tenant) as s:
        target = s.execute(text("INSERT INTO editor.publish_targets (tenant_id, site_id, kind, name, config) "
                                "VALUES (:t, :s, 'webhook', 'Sito', :c) RETURNING id"),
                           {"t": tenant, "s": site_id, "c": "{}"}).scalar()
        s.execute(text("INSERT INTO editor.publications (tenant_id, draft_id, target_id, mode, payload, status, "
                       "requested_by, decided_by, published_at, external_url) VALUES (:t, :d, :g, 'publish', :p, 'published', 'x', 'x', :w, "
                       "'https://blog.example/bari-in-fibra')"),
                  {"t": tenant, "d": did, "g": target, "p": "{}", "w": when})


def test_the_week_in_one_email(db, tenant):
    did, site_id = approved_draft(db, tenant)
    monday = date(2026, 10, 26)
    _published(db, tenant, did, site_id, datetime(2026, 10, 28, 9, tzinfo=UTC))
    with db.tenant(tenant) as s:
        s.execute(text("INSERT INTO editor.search_stats (tenant_id, site_id, day, kind, key, clicks, impressions, position) "
                       "VALUES (:t, :s, '2026-10-27', 'page', 'https://blog.example/bari-in-fibra', 7, 90, 5.0), "
                       "(:t, :s, '2026-10-27', 'query', 'fibra bari', 5, 60, 4.0)"), {"t": tenant, "s": site_id})
        s.execute(text("UPDATE editor.sites SET gsc_property = 'sc-domain:blog.example', gsc_status = 'ok'"))
    rid, w, (subject, body, page) = weekly.build(db, tenant, monday, mail_settings())
    assert rid and w.published == 1 and w.sites[0].search["this"]["clicks"] == 7
    assert "Bari in fibra" in body and "https://blog.example/bari-in-fibra" in page and "fibra bari" in page
    assert subject.startswith(w.name) and "26 ott" in subject and body.startswith(subject)
    # Stored once per week.
    assert weekly.build(db, tenant, monday, mail_settings())[0] == rid


def test_a_withdrawn_article_is_not_listed_in_the_weekly_report(db, tenant, admin):
    did, site_id = approved_draft(db, tenant)
    _published(db, tenant, did, site_id, datetime(2026, 10, 28, 9, tzinfo=UTC))
    with db.tenant(tenant) as s:
        s.execute(text("UPDATE editor.publications SET withdrawn_at = :w WHERE draft_id = :d"),
                  {"w": datetime(2026, 10, 29, 9, tzinfo=UTC), "d": did})
    rid, w, _ = weekly.build(db, tenant, date(2026, 10, 26), mail_settings())
    assert rid and w.published == 0


def test_monday_report_goes_out_once(db, tenant, admin):
    approved_draft(db, tenant)
    with db.tenant(tenant) as s:
        s.execute(text("INSERT INTO editor.recipients (tenant_id, channel, address) VALUES (:t, 'email', 'capo@blog.example')"),
                  {"t": tenant})
    with admin.engine.begin() as conn:
        conn.execute(text("UPDATE editor.tenants SET timezone = 'Europe/Rome', briefing_hour = 8, language = 'en' "
                          "WHERE tenant_id = :t"), {"t": tenant})
    FakeSMTP.sent = []
    sunday = datetime(2026, 11, 1, 10, tzinfo=UTC)
    assert tenant not in weekly.run(db, admin, mail_settings(), now=sunday, smtp_factory=FakeSMTP)
    early = datetime(2026, 11, 2, 5, tzinfo=UTC)  # 06:00 in Rome
    assert tenant not in weekly.run(db, admin, mail_settings(), now=early, smtp_factory=FakeSMTP)
    monday = datetime(2026, 11, 2, 8, tzinfo=UTC)  # 09:00 in Rome
    assert weekly.run(db, admin, mail_settings(), now=monday, smtp_factory=FakeSMTP)[tenant]["sent"] == 1
    assert tenant not in weekly.run(db, admin, mail_settings(), now=monday + timedelta(hours=1), smtp_factory=FakeSMTP)
    mine = [x for x in FakeSMTP.sent if x["Bcc"] == "capo@blog.example"]
    assert len(mine) == 1 and "the week from 26 Oct to 1 Nov" in mine[0]["Subject"]
    # Switched off: nothing the next Monday.
    with admin.engine.begin() as conn:
        conn.execute(text("UPDATE editor.tenants SET weekly_report = false WHERE tenant_id = :t"), {"t": tenant})
    assert tenant not in weekly.run(db, admin, mail_settings(), now=monday + timedelta(days=7), smtp_factory=FakeSMTP)


def test_a_refused_report_is_tried_again(db, tenant, admin):
    approved_draft(db, tenant)
    with db.tenant(tenant) as s:
        s.execute(text("INSERT INTO editor.recipients (tenant_id, channel, address) VALUES (:t, 'email', 'a@blog.example')"),
                  {"t": tenant})

    class Down(FakeSMTP):
        def send_message(self, msg):
            raise OSError("connection refused")
    monday = datetime(2026, 11, 2, 12, tzinfo=UTC)
    assert "refused" in weekly.run(db, admin, mail_settings(), now=monday, smtp_factory=Down)[tenant]["error"]
    FakeSMTP.sent = []
    assert weekly.run(db, admin, mail_settings(), now=monday + timedelta(hours=1), smtp_factory=FakeSMTP)[tenant]["sent"] == 1
    # Without Message-ID and Date SpamAssassin called it spam (Francesco 2026-11-16).
    sent = FakeSMTP.sent[-1]
    assert sent["Message-ID"].endswith("@redazia.com>") and sent["Date"]


# --- pages -------------------------------------------------------------------------


def test_pages_for_report_search_console_and_first_steps(db, tenant, monkeypatch):
    c = logged_in(db, tenant, "owner")
    start = c.get("/start")
    assert start.status_code == 200 and "Primi passi" in start.text and 'name="domain"' in start.text
    did, site_id = approved_draft(db, tenant)
    start = c.get("/start").text
    assert "Collega il sito" in start and 'class="step done"' in start
    desk = c.get("/").text
    assert "Continua i primi passi" in desk
    settings = c.get("/settings").text
    assert "Rapporto settimanale" in settings and 'name="weekly_report"' in settings
    preview = c.get("/reports/preview")
    assert preview.status_code == 200 and "Bari in fibra" not in preview.text and "blog.example" in preview.text
    monkeypatch.delenv("EDITOR_GSC_KEY", raising=False)
    monkeypatch.delenv("EDITOR_GSC_KEY_FILE", raising=False)
    assert "Google Search Console" not in c.get("/site?tab=publish").text  # nothing to connect without the server's key
    r = c.post(f"/ui/sites/{site_id}/search-console", data={"prop": "blog.example"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/site?tab=publish"
    assert "sc-domain:blog.example" in c.get("/site?tab=publish").text
    r = c.post("/ui/preferences", data={"timezone": "Europe/Rome", "briefing_hour": "8", "language": "it"},
               follow_redirects=False)
    assert r.status_code == 303
    assert "Spento: si riaccende in Preferenze." in c.get("/settings").text
    viewer = logged_in(db, tenant, "viewer")
    assert viewer.post(f"/ui/sites/{site_id}/search-console", data={"prop": ""}).status_code == 403
    assert re.search(r"Rapporto settimanale", viewer.get("/settings").text)
