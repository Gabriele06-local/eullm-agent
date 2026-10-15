import ssl
from datetime import UTC, date, datetime

import httpx
import respx
from sqlalchemy import text

from editor import briefing, profile as prof, repo
from editor.config import Settings
from editor.schedule import Runner
from test_proposals import setup_site


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    tls = []

    def starttls(self, context=None):
        FakeSMTP.tls.append(context)

    def login(self, u, p):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


SETTINGS = Settings(smtp_host="smtp.test", mail_from="editor@i3k.eu", telegram_token="T0K")


def add_recipients(db, tenant):
    with db.tenant(tenant) as s:
        s.execute(text("INSERT INTO editor.recipients (tenant_id, channel, address) VALUES "
                       "(:t, 'email', 'francesco@example.com'), (:t, 'telegram', '12345')"), {"t": tenant})


def test_morning_builds_and_sends_once(db, tenant):
    site_id = setup_site(db, tenant)
    with db.tenant(tenant) as s:  # a second site still waiting for approval
        other = repo.ensure_site(s, tenant, "nuovo.example", "Nuovo")
        prof.save_draft(s, tenant, other, {"domain": "nuovo.example", "subtopics": [{"name": "Cloud", "keywords": ["cloud"]}],
                                           "gaps": ["no publication dates found"], "analysis": {"status": "partial"}}, "analysis")
    add_recipients(db, tenant)
    FakeSMTP.sent = []
    with respx.mock() as router:
        tg = router.post("https://api.telegram.org/botT0K/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
        sender = briefing.Sender(SETTINGS, smtp_factory=FakeSMTP)
        runner = Runner(db, SETTINGS, http_factory=httpx.Client, core=None, sender=sender)
        day = date(2026, 10, 9)
        bid = runner.morning(tenant, day, datetime.now(UTC))
        assert tg.call_count == 1
        # A second run the same day sends nothing again.
        assert briefing.build(db, tenant, day) == bid
        again = sender.send(db, tenant, bid)
        assert again == {"email": 0, "telegram": 0, "errors": []} and tg.call_count == 1
    assert len(FakeSMTP.sent) == 1
    # TLS is started with a verifying context before the login
    ctx = FakeSMTP.tls[-1]
    assert ctx is not None and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    msg = FakeSMTP.sent[0]
    assert msg["Bcc"] == "francesco@example.com" and msg["To"] == "editor@i3k.eu"
    with db.tenant(tenant) as s:
        b = s.execute(text("SELECT body_md, body_html, proposal_ids, sent_email_at, sent_telegram_at FROM editor.briefings")).one()
    assert b.sent_email_at and b.sent_telegram_at and len(b.proposal_ids) == 2
    assert "## Blog Rete (blog.example)" in b.body_md
    assert "Profilo editoriale da approvare" in b.body_md and "no publication dates found" in b.body_md
    assert "https://news.example/" in b.body_md  # proposals carry their sources
    assert "<script" not in b.body_html
    tg_text = tg.calls[0].request.content.decode()
    assert "Blog Rete" in tg_text and "parse_mode" in tg_text


def test_telegram_ok_false_is_not_marked_sent(db, tenant):
    setup_site(db, tenant)
    add_recipients(db, tenant)
    FakeSMTP.sent = []
    with respx.mock() as router:
        tg = router.post("https://api.telegram.org/botT0K/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": False, "description": "Bad Request: chat not found"}))
        sender = briefing.Sender(SETTINGS, smtp_factory=FakeSMTP)
        bid = briefing.build(db, tenant, date(2026, 10, 9))
        result = sender.send(db, tenant, bid)
        assert tg.call_count >= 1
        assert result["telegram"] == 0 and result["errors"]
    with db.tenant(tenant) as s:
        row = s.execute(text("SELECT sent_email_at, sent_telegram_at, send_error FROM editor.briefings WHERE id = :i"),
                        {"i": bid}).one()
    assert row.sent_email_at and row.sent_telegram_at is None and "telegram" in (row.send_error or "")


def test_unconfigured_channels_are_errors_not_silence(db, tenant):
    add_recipients(db, tenant)
    sender = briefing.Sender(Settings())
    bid = briefing.build(db, tenant, date(2026, 10, 9))
    result = sender.send(db, tenant, bid)
    assert result == {"email": 0, "telegram": 0,
                      "errors": ["email: smtp is not configured", "telegram: token is not configured"]}
    with db.tenant(tenant) as s:
        row = s.execute(text("SELECT sent_email_at, sent_telegram_at, send_error FROM editor.briefings "
                             "WHERE id = :i"), {"i": bid}).one()
    assert row.sent_email_at is None and row.sent_telegram_at is None and row.send_error


def test_html_is_escaped(db, tenant):
    with db.tenant(tenant) as s:
        repo.ensure_site(s, tenant, "x.example", "<script>alert(1)</script>")
    bid = briefing.build(db, tenant, date(2026, 10, 10))
    with db.tenant(tenant) as s:
        page = s.execute(text("SELECT body_html FROM editor.briefings WHERE id = :i"), {"i": bid}).scalar()
    assert "<script>alert" not in page and "&lt;script&gt;" in page


def test_tick_runs_every_tenant_and_briefs_at_eight(db, tenant):
    setup_site(db, tenant)
    runner = Runner(db, Settings(), http_factory=lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    seven_rome = datetime(2026, 10, 9, 5, tzinfo=UTC)
    eight_rome = datetime(2026, 10, 9, 6, tzinfo=UTC)
    r1 = runner.tick(seven_rome)
    assert tenant in r1.tenants and not any(x.startswith("briefing") for x in r1.tenants[tenant])
    r2 = runner.tick(eight_rome)
    assert "briefing 2026-10-09" in r2.tenants[tenant]
    r3 = runner.tick(datetime(2026, 10, 9, 7, tzinfo=UTC))
    assert not any(x.startswith("briefing") for x in r3.tenants[tenant])


def test_maintenance_and_review_run_when_due_even_if_a_tick_is_late():
    from zoneinfo import ZoneInfo

    from editor.schedule import maintenance_due, review_due

    rome = ZoneInfo("Europe/Rome")
    monday_5 = datetime(2026, 10, 12, 5, tzinfo=rome)
    assert maintenance_due(monday_5, None)
    assert not maintenance_due(monday_5, datetime(2026, 10, 12, 3, 10, tzinfo=rome))
    assert maintenance_due(monday_5, datetime(2026, 10, 11, 3, 10, tzinfo=rome))  # the 03:00 tick was missed
    assert not maintenance_due(datetime(2026, 10, 12, 2, tzinfo=rome), None)
    assert review_due(monday_5, None)
    assert not review_due(monday_5, datetime(2026, 10, 12, 4, 30, tzinfo=rome))
    assert review_due(monday_5, datetime(2026, 10, 5, 4, 30, tzinfo=rome))
    tuesday = datetime(2026, 10, 13, 9, tzinfo=rome)
    assert not review_due(tuesday, datetime(2026, 10, 6, 4, 30, tzinfo=rome))  # done last week, wait for Monday
    assert review_due(tuesday, datetime(2026, 10, 5, 4, 30, tzinfo=rome))  # Monday was missed


def test_a_failing_step_does_not_stop_the_briefing_and_is_recorded(db, tenant, monkeypatch):
    from editor import schedule

    setup_site(db, tenant)

    def broken(*a, **k):
        raise RuntimeError("feed server down")

    monkeypatch.setattr(schedule, "collect_tenant", broken)
    runner = Runner(db, Settings(), http_factory=lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    report = runner.tick(datetime(2026, 10, 9, 6, tzinfo=UTC))
    assert "briefing 2026-10-09" in report.tenants[tenant]
    assert "collect failed" in report.errors[tenant]
    with db.tenant(tenant) as s:
        job = s.execute(text("SELECT status, error FROM editor.jobs WHERE kind = 'collect'")).one()
    assert job.status == "failed" and "feed server down" in job.error


def test_an_undelivered_briefing_is_sent_again(db, tenant):
    setup_site(db, tenant)
    add_recipients(db, tenant)

    class Flaky:
        calls = 0

        def send(self, db_, tenant_, bid):
            if tenant_ != tenant:  # the runner visits every tenant in the database
                return
            Flaky.calls += 1
            values = {"send_error": "email: connection refused"} if Flaky.calls == 1 else {"send_error": None}
            with db_.tenant(tenant_) as s:
                s.execute(text("UPDATE editor.briefings SET send_error = :e WHERE id = :i"),
                          {"e": values["send_error"], "i": bid})

    runner = Runner(db, Settings(), sender=Flaky(),
                    http_factory=lambda: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    runner.tick(datetime(2026, 10, 9, 6, tzinfo=UTC))
    r = runner.tick(datetime(2026, 10, 9, 7, tzinfo=UTC))
    assert "briefing 2026-10-09 resent" in r.tenants[tenant] and Flaky.calls == 2
    r = runner.tick(datetime(2026, 10, 9, 8, tzinfo=UTC))
    assert Flaky.calls == 2  # delivered: nothing more to send
