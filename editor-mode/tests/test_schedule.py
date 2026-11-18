from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from editor.config import Settings
from editor.schedule import Runner, briefing_due

ROME = "Europe/Rome"


def fire_times(start: datetime, hours: int):
    sent, fired = set(), []
    for h in range(hours):
        now = start + timedelta(hours=h)
        day = briefing_due(now, ROME, 8, sent)
        if day:
            sent.add(day)
            fired.append(now)
    return fired


def test_once_a_day_at_eight_across_spring_forward():
    # Italy moves to CEST on Sunday 29 March 2026.
    fired = fire_times(datetime(2026, 3, 27, 0, tzinfo=UTC), 24 * 4)
    assert [t.astimezone(ZoneInfo(ROME)).strftime("%m-%d %H:%M") for t in fired] == [
        "03-27 08:00", "03-28 08:00", "03-29 08:00", "03-30 08:00"]
    assert [t.hour for t in fired] == [7, 7, 6, 6]  # UTC


def test_once_a_day_at_eight_across_fall_back():
    # Back to CET on Sunday 25 October 2026.
    fired = fire_times(datetime(2026, 10, 23, 0, tzinfo=UTC), 24 * 4)
    assert [t.astimezone(ZoneInfo(ROME)).strftime("%m-%d %H:%M") for t in fired] == [
        "10-23 08:00", "10-24 08:00", "10-25 08:00", "10-26 08:00"]
    assert [t.hour for t in fired] == [6, 6, 7, 7]


def test_late_worker_still_sends_within_the_window_but_not_later():
    sent = set()
    nine = datetime(2026, 10, 9, 7, tzinfo=UTC)  # 09:00 in Rome
    assert briefing_due(nine, ROME, 8, sent) is not None
    evening = datetime(2026, 10, 9, 18, tzinfo=UTC)
    assert briefing_due(evening, ROME, 8, sent) is None
    assert briefing_due(nine, ROME, 8, {nine.astimezone(ZoneInfo(ROME)).date()}) is None


def test_a_late_briefing_window_wraps_past_midnight():
    tick = datetime(2026, 10, 10, 0, 30, tzinfo=UTC)
    assert briefing_due(tick, "UTC", 23, set()) == date(2026, 10, 9)
    assert briefing_due(tick, "UTC", 23, {date(2026, 10, 9)}) is None
    assert briefing_due(datetime(2026, 10, 10, 2, 30, tzinfo=UTC), "UTC", 23, set()) is None
    assert briefing_due(tick, "UTC", 8, set()) is None


def test_invalid_tenant_timezone_falls_back_to_default(db, pg_urls, tenant):
    import psycopg

    with psycopg.connect(pg_urls[0], autocommit=True) as conn:
        conn.execute("UPDATE editor.tenants SET timezone = 'Mars/Olympus' WHERE tenant_id = %s", (tenant,))
    runner = Runner(db, Settings(), http_factory=lambda: None)
    assert runner.tenant_settings(tenant)["timezone"] == Settings().timezone
