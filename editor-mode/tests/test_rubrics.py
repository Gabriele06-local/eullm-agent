"""Rubrics the newsroom decides each week from the news of the week before
(Francesco 2026-11-19): weights, one rubric more at most, the owner's
rubrics never removed, every change undone or switched off by the owner."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, select

from editor import autopilot as ap, models as m, profile as prof, repo, rubrics, weekly

from conftest import set_plan
from test_proposals import setup_site
from test_ui import logged_in

NOW = datetime(2026, 11, 23, 4, 30, tzinfo=UTC)  # a Monday


class Core:
    def __init__(self, value):
        self.value, self.prompts = value, []

    def chat_json(self, system, user, validate, **kw):
        self.prompts.append(user)
        return validate(self.value), []


def snapshots(db, tenant, topics, days=5):
    """`topics`: (id, label, hype); each in the trend `days` days (eight snapshots a day)."""
    archive = repo.archive_of(db, tenant)
    with db.tenant(archive) as s:
        for d in range(days):
            for h in range(0, 24, 3):
                s.execute(insert(m.trend_snapshots).values(
                    tenant_id=archive, taken_at=NOW.replace(hour=0, minute=0) - timedelta(days=d + 1) + timedelta(hours=h),
                    topics=[{"topic_id": i, "label": label, "summary": "", "hype": hype} for i, label, hype in topics]))


def site(db, tenant, **auto):
    site_id = setup_site(db, tenant)
    set_plan(db, tenant, autopilot=True)
    with db.tenant(tenant) as s:
        assert ap.save(s, site_id, {**ap.DEFAULTS, "enabled": True, "sections": ["Mondo", "Tecnologia", "Sport"],
                                    "avoid": ["calcio"], **auto})
    return site_id


def body(db, tenant, site_id):
    with db.tenant(tenant) as s:
        return prof.approved_profile(s, site_id).body


TOPICS = [(1, "Elezioni", 80), (2, "Vertice Nato", 60), (3, "Chip", 20), (4, "Smartphone", 10),
          (5, "Tennis", 5), (6, "Ciclismo", 5), (7, "Spazio: missione su Marte", 40)]
ANSWER = {"topics": {"1": "Mondo", "2": "Mondo", "3": "Tecnologia", "4": "Tecnologia", "5": "Sport",
                     "6": "Sport", "7": None},
          "new": {"name": "Spazio", "keywords": ["marte", "nasa", "razzo"], "topics": [7]}}


def test_the_week_weighs_the_rubrics_and_adds_one(db, tenant):
    site_id = site(db, tenant)
    snapshots(db, tenant, TOPICS)
    core = Core(ANSWER)
    entry = rubrics.review(db, tenant, site_id, core, NOW)
    assert entry and entry["added"] == "Spazio"
    assert "Vertice Nato" in core.prompts[0] and "Tecnologia" in core.prompts[0]
    b = body(db, tenant, site_id)
    rb = rubrics.state(b)
    # Mondo 140 of 180 → top; Tecnologia 30 → half; Sport 10: never below the floor.
    assert rb["weights"]["Mondo"] == rubrics.TOP and rb["weights"]["Sport"] == rubrics.FLOOR
    assert rb["weights"]["Tecnologia"] == 0.5
    cfg = ap.settings_of(b)
    assert cfg["sections"] == ["Mondo", "Tecnologia", "Sport"]  # the owner's stay as they are
    assert ap.sections_of(cfg) == ["Mondo", "Tecnologia", "Sport", "Spazio"]
    assert any(x["name"] == "Spazio" and x.get("by") == "rubrics" for x in b["subtopics"])
    # The places a day: the share times the weight, at least one.
    base = ap.section_cap(cfg)
    assert ap.section_cap(cfg, "Mondo") == round(base * 2) and ap.section_cap(cfg, "Sport") == max(1, round(base / 4))
    assert ap.section_of(cfg, "spazio") == "Spazio"
    # Once a week.
    assert rubrics.review(db, tenant, site_id, core, NOW + timedelta(hours=2)) is None
    assert len(core.prompts) == 1


def test_a_new_rubric_needs_three_days_and_stays_inside_the_limits(db, tenant):
    site_id = site(db, tenant)
    snapshots(db, tenant, TOPICS, days=2)
    rubrics.review(db, tenant, site_id, Core(ANSWER), NOW)
    assert rubrics.state(body(db, tenant, site_id))["added"] == []  # two days in the trend: not yet
    # A subject to avoid never becomes a rubric.
    with db.tenant(tenant) as s:
        rubrics._store(s, site_id, {**rubrics.state(body(db, tenant, site_id)), "week": None})
    snapshots(db, tenant, TOPICS, days=5)
    rubrics.review(db, tenant, site_id, Core({**ANSWER, "new": {"name": "Calcio", "keywords": ["calcio"],
                                                                 "topics": [7]}}), NOW)
    assert rubrics.state(body(db, tenant, site_id))["added"] == []


def test_the_owner_undoes_takes_out_and_switches_off(db, tenant):
    site_id = site(db, tenant)
    snapshots(db, tenant, TOPICS)
    rubrics.review(db, tenant, site_id, Core(ANSWER), NOW)
    c = logged_in(db, tenant, "owner")
    page = c.get("/site?tab=auto").text
    assert "Rubriche decise dalla redazione" in page and "Spazio" in page and "aggiunta dalla redazione" in page
    r = c.post(f"/ui/sites/{site_id}/rubrics", data={"action": "undo"}, follow_redirects=False)
    assert r.status_code == 303
    b = body(db, tenant, site_id)
    rb = rubrics.state(b)
    assert rb["weights"] == {} and rb["added"] == [] and rb["refused"] == ["Spazio"]
    assert not any(x["name"] == "Spazio" for x in b["subtopics"])
    # Next week the same subject is not offered again.
    rubrics.review(db, tenant, site_id, Core(ANSWER), NOW + timedelta(days=7))
    assert rubrics.state(body(db, tenant, site_id))["added"] == []
    # Off: every rubric weighs the same.
    c.post(f"/ui/sites/{site_id}/rubrics", data={"action": "off"})
    cfg = ap.settings_of(body(db, tenant, site_id))
    assert cfg["section_weights"] == {} and not rubrics.is_on(body(db, tenant, site_id))
    assert "Spente per questo sito" in c.get("/site?tab=auto").text


def test_take_out_a_rubric_the_newsroom_added(db, tenant):
    site_id = site(db, tenant)
    snapshots(db, tenant, TOPICS)
    rubrics.review(db, tenant, site_id, Core(ANSWER), NOW)
    with db.tenant(tenant) as s:
        assert not rubrics.take_out(s, site_id, "Mondo")  # the owner's: not here
        assert rubrics.take_out(s, site_id, "spazio")
    b = body(db, tenant, site_id)
    assert ap.sections_of(ap.settings_of(b)) == ["Mondo", "Tecnologia", "Sport"]
    assert not any(x["name"] == "Spazio" for x in b["subtopics"])


def test_without_the_automatic_newsroom_nothing_is_decided(db, tenant):
    site_id = site(db, tenant, enabled=False)
    snapshots(db, tenant, TOPICS)
    core = Core(ANSWER)
    assert rubrics.review(db, tenant, site_id, core, NOW) is None and not core.prompts


def test_the_monday_report_tells_the_change(db, tenant):
    site_id = site(db, tenant)
    snapshots(db, tenant, TOPICS)
    rubrics.review(db, tenant, site_id, Core(ANSWER), NOW)
    settings = type("S", (), {"timezone": "Europe/Rome", "public_url": ""})()
    _, w, (subject, text_, html) = weekly.build(db, tenant, weekly.week_of(NOW), settings, store=False)
    assert w.sites[0].rubrics and "nuova rubrica «Spazio»" in text_ and "Mondo più spazio" in text_
