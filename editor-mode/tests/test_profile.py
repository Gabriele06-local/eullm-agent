import json

import pytest

import httpx
import respx
from sqlalchemy import select, text, update

import fakesite
from editor import models as m
from editor import profile as prof
from editor import core_client
from editor.core_client import CoreClient
from editor.site import SiteCrawler


def snapshot(articles=fakesite.ARTICLES):
    with respx.mock(assert_all_called=False) as router:
        fakesite.mount(router, articles=articles)
        with httpx.Client() as c:
            return SiteCrawler(c).analyse("blog.example")


def fake_core(router, answer):
    def reply(request):
        body = json.loads(request.content)
        content = answer(body) if callable(answer) else answer
        return httpx.Response(200, json={"content": content, "tool_calls": [],
                                         "usage": {"input_tokens": 900, "output_tokens": 200}, "cost": 0.002})
    router.post("http://core.test/v1/llm/chat").mock(side_effect=reply)
    return CoreClient("http://core.test", "tok")


def test_baseline_cites_articles_and_leaves_sector_empty():
    snap = snapshot()
    body, calls = prof.build_profile(snap, None)
    assert calls == []
    assert body["language"] == "it" and body["sector"]["value"] is None
    names = [s["name"] for s in body["subtopics"]]
    assert "Fibra" in names and "Wi-Fi" in names
    fibra = next(s for s in body["subtopics"] if s["name"] == "Fibra")
    assert fibra["share"] > 0 and all(u.startswith("https://blog.example/") for u in fibra["evidence"])
    assert any("model analysis not run" in g for g in body["gaps"])


def test_model_statements_need_evidence_from_the_site():
    snap = snapshot()
    real = snap.posts[0].url
    answer = json.dumps({
        "sector": {"value": "Connettività e telecomunicazioni", "evidence": [real]},
        "audience": {"value": "Ingegneri della NASA", "evidence": ["https://invented.example/x"]},
        "style": {"tone": "divulgativo", "formats": ["news", "guide"], "evidence": [real]},
        "subtopics": [
            {"name": "Fibra ottica", "keywords": ["fibra", "ftth"], "evidence": [real]},
            {"name": "Criptovalute", "keywords": ["bitcoin"], "evidence": ["https://invented.example/y"]},
        ],
        "gaps": [],
    })
    with respx.mock(assert_all_called=False) as router:
        core = fake_core(router, answer)
        body, calls = prof.build_profile(snap, core)
    assert len(calls) == 1
    assert body["sector"] == {"value": "Connettività e telecomunicazioni", "evidence": [real], "by": "model"}
    assert body["audience"]["value"] is None  # invented evidence: dropped
    assert any("audience proposed without valid evidence" in g for g in body["gaps"])
    assert [s["name"] for s in body["subtopics"]] == ["Fibra ottica"]
    assert body["subtopics"][0]["share"] > 0  # measured on the articles, not taken from the model
    assert body["style"]["tone"] == "divulgativo"


def test_each_article_counts_once_and_words_on_every_article_weigh_nothing():
    from types import SimpleNamespace as P

    posts = [
        P(url="u1", title="EuLLM: RAG on-premise per documenti", categories=[]),
        P(url="u2", title="EuLLM e la sovranità digitale europea", categories=[]),
        P(url="u3", title="EuLLM: modelli decisionali locali", categories=[]),
        P(url="u4", title="Infrastruttura sovranità digitale EuLLM", categories=[]),
    ]
    subs = [
        {"name": "RAG", "keywords": ["eullm", "rag", "documenti"]},
        {"name": "Sovranità", "keywords": ["eullm", "sovranità", "digitale"]},
        {"name": "Decisioni", "keywords": ["eullm", "decisionali"]},
        {"name": "Solo marchio", "keywords": ["eullm"]},
    ]
    out = prof.assign_shares(subs, posts)
    assert [h for _, h in out] == [["u1"], ["u2", "u4"], ["u3"], []]
    assert [sh for sh, _ in out] == [0.25, 0.5, 0.25, 0.0]
    assert prof.assign_shares(subs, []) == [(0.0, [])] * 4


def test_insufficient_site_gets_no_model_call():
    snap = snapshot()
    snap.status, snap.posts, snap.categories = "insufficient", snap.posts[:2], {}
    with respx.mock(assert_all_called=False) as router:
        core = fake_core(router, "{}")
        body, calls = prof.build_profile(snap, core)
        assert not router.calls
    assert calls == [] and body["sector"]["value"] is None
    assert any("not enough material" in g for g in body["gaps"])


def test_drift_finds_the_new_direction():
    old, _ = prof.build_profile(snapshot(), None)
    new, _ = prof.build_profile(snapshot(fakesite.SHIFTED), None)
    changes = prof.drift(old, new)
    kinds = {(c["kind"], c.get("subtopic")) for c in changes}
    assert ("emerging", "Sicurezza") in kinds
    assert any(k == "declining" for k, _ in kinds)
    assert prof.drift(old, old) == []


def test_site_analysis_and_review_lifecycle(db, tenant):
    def crawler(articles):
        class C:
            def analyse(self, domain):
                return snapshot(articles)
        return C()

    first = prof.analyse_site(db, tenant, "blog.example", crawler(fakesite.ARTICLES))
    assert first["profile_id"] and first["status"] == "partial"
    with db.tenant(tenant) as s:
        status, origin, version = s.execute(text("SELECT status, origin, version FROM editor.editorial_profiles")).one()
        posts = s.execute(text("SELECT count(*) FROM editor.site_posts")).scalar()
    assert (status, origin, version) == ("draft", "analysis", 1) and posts == 8

    # Nothing is approved yet: the owner decides.
    with db.tenant(tenant) as s:
        assert prof.approved_profile(s, first["site_id"]) is None
        assert prof.approve(s, first["site_id"], 1, "francesco")

    # Same site, no change: a review is recorded, no new draft.
    same = prof.analyse_site(db, tenant, "blog.example", crawler(fakesite.ARTICLES))
    assert same["significant"] is False and same["profile_id"] is None

    # The site moved towards security and cloud: a draft is proposed, the
    # approved profile stays in charge.
    moved = prof.analyse_site(db, tenant, "blog.example", crawler(fakesite.SHIFTED))
    assert moved["significant"] and moved["profile_id"]
    with db.tenant(tenant) as s:
        p = m.editorial_profiles
        rows = s.execute(select(p.c.version, p.c.status, p.c.origin, p.c.based_on, p.c.changes)
                         .order_by(p.c.version)).all()
        reviews = s.execute(text("SELECT count(*), count(*) FILTER (WHERE significant) FROM editor.profile_reviews")).one()
        posts = s.execute(text("SELECT count(*) FROM editor.site_posts")).scalar()
    assert [(r.version, r.status, r.origin) for r in rows] == [(1, "approved", "analysis"), (2, "draft", "reanalysis")]
    assert rows[1].based_on == 1 and any(c["subtopic"] == "Sicurezza" for c in rows[1].changes if "subtopic" in c)
    assert tuple(reviews) == (2, 1)
    assert posts == 15  # history keeps the old articles

    with db.tenant(tenant) as s:
        assert prof.approve(s, first["site_id"], 2, "francesco")
        statuses = s.execute(text("SELECT version, status FROM editor.editorial_profiles ORDER BY version")).all()
    assert [tuple(r) for r in statuses] == [(1, "retired"), (2, "approved")]


def test_approving_a_draft_retires_the_older_drafts(db, tenant):
    class C:
        def analyse(self, domain):
            return snapshot()

    first = prof.analyse_site(db, tenant, "blog.example", C())
    site_id = first["site_id"]
    with db.tenant(tenant) as s:
        body = s.execute(select(m.editorial_profiles.c.body)).scalar()
        prof.save_draft(s, tenant, site_id, body, "analysis")
        prof.save_draft(s, tenant, site_id, body, "analysis")
        assert prof.approve(s, site_id, 2, "francesco")
        statuses = s.execute(text("SELECT version, status FROM editor.editorial_profiles ORDER BY version")).all()
    # The newer draft still asks for a decision.
    assert [tuple(r) for r in statuses] == [(1, "retired"), (2, "approved"), (3, "draft")]


def test_manual_edit_keeps_owner_settings(db, tenant):
    def crawler(articles):
        class C:
            def analyse(self, domain):
                return snapshot(articles)
        return C()

    first = prof.analyse_site(db, tenant, "blog.example", crawler(fakesite.ARTICLES))
    with db.tenant(tenant) as s:
        assert prof.approve(s, first["site_id"], 1, "francesco")
        p = m.editorial_profiles
        approved = (p.c.site_id == first["site_id"]) & (p.c.status == "approved")
        body = s.execute(select(p.c.body).where(approved)).scalar()
        body.setdefault("settings", {})["exclude"] = ["calcio"]
        s.execute(update(p).where(approved).values(body=body))
        pid = prof.edit(s, tenant, first["site_id"], {"subtopics": [{"name": "Fibra", "keywords": ["fibra"]}]})
        body = s.execute(select(m.editorial_profiles.c.body).where(m.editorial_profiles.c.id == pid)).scalar()
    assert body["settings"]["exclude"] == ["calcio"]


def test_manual_edit_keeps_explicit_settings(db, tenant):
    def crawler(articles):
        class C:
            def analyse(self, domain):
                return snapshot(articles)
        return C()

    first = prof.analyse_site(db, tenant, "blog.example", crawler(fakesite.ARTICLES))
    settings = {"proposals_per_day": 9, "min_relevance": 0.5, "exclude": []}
    with db.tenant(tenant) as s:
        assert prof.approve(s, first["site_id"], 1, "francesco")
        pid = prof.edit(s, tenant, first["site_id"], {"subtopics": [{"name": "Fibra", "keywords": ["fibra"]}],
                                                      "settings": settings})
        body = s.execute(select(m.editorial_profiles.c.body).where(m.editorial_profiles.c.id == pid)).scalar()
    assert body["settings"] == settings


def test_validate_edit_keeps_unknown_parts_and_unset_settings():
    body = {"subtopics": [{"name": " Fibra ", "keywords": ["ftth"], "note": "x"}], "gaps": ["g"]}
    out = prof.validate_edit(body)
    assert out == {"subtopics": [{"name": "Fibra", "keywords": ["ftth"], "note": "x"}], "gaps": ["g"]}
    assert "settings" not in out  # so the owner's approved settings are kept
    with pytest.raises(ValueError, match="subtopics"):
        prof.validate_edit({"subtopics": "fibra"})


def test_a_newer_analysis_replaces_the_draft_still_waiting(db, tenant):
    class C:
        def analyse(self, domain):
            return snapshot(fakesite.ARTICLES)

    prof.analyse_site(db, tenant, "blog.example", C())
    prof.analyse_site(db, tenant, "blog.example", C())
    with db.tenant(tenant) as s:
        rows = s.execute(text("SELECT version, status FROM editor.editorial_profiles ORDER BY version")).all()
    # One profile of the site waits for a decision, not two.
    assert [tuple(r) for r in rows] == [(1, "retired"), (2, "draft")]


def test_a_better_method_proposes_a_new_profile_even_when_approved(db, tenant):
    class C:
        def analyse(self, domain):
            return snapshot(fakesite.ARTICLES)

    first = prof.analyse_site(db, tenant, "blog.example", C())
    with db.tenant(tenant) as s:
        assert prof.approve(s, first["site_id"], 1, "francesco")
        # A profile approved before the current way of reading the site.
        p = m.editorial_profiles
        body = s.execute(select(p.c.body)).scalar()
        body.pop("method", None)
        s.execute(update(p).values(body=body))
    again = prof.analyse_site(db, tenant, "blog.example", C())
    assert again["significant"] and again["profile_id"]
    with db.tenant(tenant) as s:
        rows = s.execute(select(m.editorial_profiles.c.status, m.editorial_profiles.c.changes)
                         .order_by(m.editorial_profiles.c.version)).all()
    assert [r.status for r in rows] == ["approved", "draft"] and rows[1].changes == [{"kind": "method"}]


def test_owner_takes_a_topic_out_and_it_stays_out(db, tenant):
    class C:
        def analyse(self, domain):
            return snapshot(fakesite.ARTICLES)

    first = prof.analyse_site(db, tenant, "blog.example", C())
    site_id = first["site_id"]
    with db.tenant(tenant) as s:
        body = s.execute(select(m.editorial_profiles.c.body)).scalar()
        name = body["subtopics"][0]["name"]
        # On a draft the change is made in place.
        assert prof.remove_topic(s, tenant, site_id, name, "francesco") == 1
        assert prof.approve(s, site_id, 1, "francesco")
        body = s.execute(select(m.editorial_profiles.c.body)).scalar()
    assert name not in [x["name"] for x in body["subtopics"]]
    assert [x["name"] for x in body["settings"]["removed"]] == [name]

    # A later analysis finds the topic again: it is not proposed back.
    prof.analyse_site(db, tenant, "blog.example", C())
    with db.tenant(tenant) as s:
        bodies = s.execute(select(m.editorial_profiles.c.body).order_by(m.editorial_profiles.c.version)).scalars().all()
        assert all(name not in [x["name"] for x in b["subtopics"]] for b in bodies)
        # On an approved profile the owner's choice is a new approved version.
        version = prof.restore_topic(s, tenant, site_id, name, "francesco")
        row = s.execute(select(m.editorial_profiles).where(m.editorial_profiles.c.version == version)).one()
    assert row.status == "approved" and name in [x["name"] for x in row.body["subtopics"]]
    assert row.body["settings"]["removed"] == []


def test_versions_not_in_use_can_be_deleted(db, tenant):
    class C:
        def analyse(self, domain):
            return snapshot(fakesite.ARTICLES)

    site_id = prof.analyse_site(db, tenant, "blog.example", C())["site_id"]
    with db.tenant(tenant) as s:
        assert prof.approve(s, site_id, 1, "francesco")
        body = s.execute(select(m.editorial_profiles.c.body)).scalar()
        prof.save_draft(s, tenant, site_id, body, "manual")
        assert prof.delete_version(s, site_id, 1) == "approved"
        assert prof.delete_version(s, site_id, 2) is None
        assert prof.delete_version(s, site_id, 2) == "not found"
        versions = s.execute(select(m.editorial_profiles.c.version)).scalars().all()
    assert versions == [1]


def test_compare_says_what_changes_against_what():
    old = {"sector": {"value": None}, "subtopics": [{"name": "EuLLM", "keywords": ["eullm"], "share": 1.0},
                                                    {"name": "RAG Enterprise", "keywords": ["rag", "enterprise"], "share": 1.0}]}
    new = {"sector": {"value": "AI enterprise"}, "summary": "s",
           "subtopics": [{"name": "Branding", "keywords": ["branding", "grafica"], "share": 0.28}]}
    rows = prof.compare(old, new)
    by = {r["what"]: r for r in rows}
    assert by["Settore"]["before"] == "non determinato" and by["Settore"]["after"] == "AI enterprise"
    assert by["Tema «Branding»"]["before"] == "non c'era"
    assert by["Tema «EuLLM»"]["after"] == "esce dal profilo"
    assert any(r.get("note") for r in rows)  # why the 100% topics leave
    removed = prof.compare({"summary": "s", "subtopics": []}, {"summary": None, "subtopics": []})
    row = next(r for r in removed if r["key"] == "summary")
    assert row["before"] != row["after"] and "assente" in row["after"]


def test_a_newer_reanalysis_replaces_the_proposal_still_waiting(db, tenant):
    class C:
        def __init__(self, articles):
            self.articles = articles

        def analyse(self, domain):
            return snapshot(self.articles)

    site_id = prof.analyse_site(db, tenant, "blog.example", C(fakesite.ARTICLES))["site_id"]
    with db.tenant(tenant) as s:
        assert prof.approve(s, site_id, 1, "francesco")
    prof.analyse_site(db, tenant, "blog.example", C(fakesite.SHIFTED))
    prof.analyse_site(db, tenant, "blog.example", C(fakesite.SHIFTED))
    with db.tenant(tenant) as s:
        rows = s.execute(text("SELECT version, status FROM editor.editorial_profiles ORDER BY version")).all()
    assert [tuple(r) for r in rows] == [(1, "approved"), (2, "retired"), (3, "draft")]


def test_a_model_that_fails_for_a_moment_is_asked_again(monkeypatch):
    snap = snapshot()
    real = snap.posts[0].url
    answer = json.dumps({
        "sector": {"value": "Connettività", "evidence": [real]},
        "audience": {"value": "Chi usa la rete", "evidence": [real]},
        "style": {"tone": "divulgativo", "formats": ["news"], "evidence": [real]},
        "subtopics": [], "gaps": [],
    })
    monkeypatch.setattr(core_client, "RETRY_WAITS", (0, 0))
    with respx.mock(assert_all_called=False) as router:
        router.post("http://core.test/v1/llm/chat").mock(side_effect=[
            httpx.Response(502, json={"error": "model call failed: openai error 500 Internal Server Error"}),
            httpx.Response(200, json={"content": answer, "tool_calls": [], "usage": {}, "cost": 0}),
        ])
        body, _ = prof.build_profile(snap, CoreClient("http://core.test", "tok"))
    assert body["sector"]["value"] == "Connettività"

    # A refusal (a spending limit) is not retried: the reason is kept.
    with respx.mock(assert_all_called=False) as router:
        route = router.post("http://core.test/v1/llm/chat").mock(
            return_value=httpx.Response(403, json={"error": "llm_cost limit reached"}))
        body, _ = prof.build_profile(snap, CoreClient("http://core.test", "tok"))
        assert route.call_count == 1
    assert body["sector"]["value"] is None
    assert any("llm_cost limit" in g for g in body["gaps"])


def test_an_analysis_the_model_left_incomplete_is_tried_again(db, tenant, monkeypatch):
    from datetime import UTC, datetime, timedelta

    class C:
        def analyse(self, domain):
            return snapshot()

    monkeypatch.setattr(core_client, "RETRY_WAITS", ())
    down = respx.mock(assert_all_called=False)
    with down as router:
        router.post("http://core.test/v1/llm/chat").mock(
            return_value=httpx.Response(502, json={"error": "openai error 500 Internal Server Error"}))
        first = prof.analyse_site(db, tenant, "blog.example", C(), CoreClient("http://core.test", "tok"))
    assert first["model_failed"] and first["profile_id"]
    now = datetime.now(UTC)
    with db.tenant(tenant) as s:
        assert not prof.retry_due(s, first["site_id"], now)  # just tried
        assert prof.retry_due(s, first["site_id"], now + timedelta(hours=1))
        # Approved anyway: a review where the model does not answer keeps the
        # line in use, proposes nothing and is tried again.
        assert prof.approve(s, first["site_id"], 1, "francesco")
    with respx.mock(assert_all_called=False) as router:
        router.post("http://core.test/v1/llm/chat").mock(
            return_value=httpx.Response(503, json={"error": "busy"}))
        again = prof.analyse_site(db, tenant, "blog.example", C(), CoreClient("http://core.test", "tok"))
    assert again["profile_id"] is None and again["changes"] == []
    with db.tenant(tenant) as s:
        assert prof.retry_due(s, first["site_id"], now + timedelta(hours=3))
    # The model answers: nothing more to retry.
    real = snapshot().posts[0].url
    answer = json.dumps({"sector": {"value": "Reti", "evidence": [real]}, "audience": {"value": "Tecnici", "evidence": [real]},
                         "style": {"tone": "tecnico", "formats": ["news"], "evidence": [real]}, "subtopics": [], "gaps": []})
    with respx.mock(assert_all_called=False) as router:
        prof_core = fake_core(router, answer)
        done = prof.analyse_site(db, tenant, "blog.example", C(), prof_core)
    assert not done["model_failed"] and done["profile_id"]  # a better reading of the same site
    with db.tenant(tenant) as s:
        assert not prof.retry_due(s, first["site_id"], now + timedelta(hours=5))


def test_the_hourly_tick_queues_the_incomplete_analysis_again(db, tenant):
    from datetime import UTC, datetime, timedelta

    from editor import models as m
    from editor.config import Settings
    from editor.schedule import Runner

    class C:
        def analyse(self, domain):
            return snapshot()

    with respx.mock(assert_all_called=False) as router:
        router.post("http://core.test/v1/llm/chat").mock(return_value=httpx.Response(500, json={"error": "boom"}))
        prof.analyse_site(db, tenant, "blog.example", C(), CoreClient("http://core.test", "tok"))
    runner = Runner(db, Settings(), http_factory=lambda: None)
    runner.core_for = lambda t: object()  # a model is configured
    assert runner.retry_profiles(tenant, datetime.now(UTC)) == []
    assert runner.retry_profiles(tenant, datetime.now(UTC) + timedelta(hours=1)) == ["blog.example"]
    with db.tenant(tenant) as s:
        job = s.execute(select(m.jobs.c.kind, m.jobs.c.subject, m.jobs.c.status)).one()
    assert tuple(job) == ("analysis", "blog.example", "queued")


V2 = {"sector": {"value": "Tecnologia", "evidence": ["https://blog.example/a"], "by": "model"},
      "audience": {"value": "Professionisti", "evidence": [], "by": "model"},
      "style": {"tone": "sobrio", "formats": ["news"]},
      "settings": {"proposals_per_day": 6, "min_relevance": 0.15},
      "subtopics": [{"name": "Fibra", "keywords": ["fibra", "ftth"], "share": 0.4},
                    {"name": "Wi-Fi", "keywords": ["wifi", "router"], "share": 0.3},
                    {"name": "Satelliti", "keywords": ["satellite", "starlink"], "share": 0.2}]}
V4 = {"sector": {"value": "Informazione generalista", "evidence": ["https://blog.example/b"], "by": "model"},
      "audience": {"value": "Professionisti", "evidence": [], "by": "model"},
      "style": {"tone": "informativo", "formats": ["news"]},
      "settings": {"proposals_per_day": 6, "min_relevance": 0.15},
      "subtopics": [{"name": "Fibra", "keywords": ["fibra", "ftth"], "share": 0.7},
                    {"name": "Wi-Fi", "keywords": ["wifi", "router"], "share": 0.3},
                    {"name": "Politica", "keywords": ["governo", "elezioni"], "share": 0.25}]}


def test_every_row_of_the_comparison_can_be_chosen():
    rows = prof.compare(V2, V4)
    keys = {r["key"] for r in rows if r.get("key")}
    assert keys == {"sector", "tone", "topic:Fibra", "topic:Politica", "gone:Satelliti"}


def test_mix_keeps_the_unticked_rows_as_approved():
    # Everything from the proposal but the sector, the new topic and the topic that leaves.
    body = prof.mix(V2, V4, {"sector", "topic:Politica", "gone:Satelliti"})
    assert body["sector"]["value"] == "Tecnologia"
    assert body["style"]["tone"] == "informativo"
    names = {s["name"]: s["share"] for s in body["subtopics"]}
    assert names == {"Fibra": 0.7, "Wi-Fi": 0.3, "Satelliti": 0.2}  # Politica not added, Satelliti stays
    body = prof.mix(V2, V4, {"topic:Fibra", "tone"})
    assert {s["name"]: s["share"] for s in body["subtopics"]}["Fibra"] == 0.4
    assert body["style"]["tone"] == "sobrio" and body["sector"]["value"] == "Informazione generalista"
    with pytest.raises(ValueError):
        prof.mix(V2, V4, {"audience"})  # not a difference: nothing to choose
    assert V4["subtopics"][0]["share"] == 0.7  # the proposal itself is not touched


def _two_versions(db, tenant):
    from editor import repo
    with db.tenant(tenant) as s:
        site_id = repo.ensure_site(s, tenant, "blog.example", "Blog")
        prof.save_draft(s, tenant, site_id, dict(V2), "analysis")
        assert prof.approve(s, site_id, 1, "francesco")
        prof.save_draft(s, tenant, site_id, dict(V4), "reanalysis", based_on=1)
    return site_id


def test_owner_choices_become_a_new_approved_version(db, tenant):
    site_id = _two_versions(db, tenant)
    with db.tenant(tenant) as s:
        version, how = prof.approve_choice(s, tenant, site_id, 2, 1, {"tone", "topic:Fibra", "topic:Politica"}, "francesco")
        rows = s.execute(text("SELECT version, status FROM editor.editorial_profiles ORDER BY version")).all()
        body = prof.approved_profile(s, site_id).body
    assert (version, how) == (3, "mixed")
    assert [tuple(r) for r in rows] == [(1, "retired"), (2, "retired"), (3, "approved")]
    assert body["sector"]["value"] == "Tecnologia" and body["style"]["tone"] == "informativo"
    assert {s["name"] for s in body["subtopics"]} == {"Fibra", "Wi-Fi", "Politica", "Satelliti"}


def test_owner_choices_all_or_none(db, tenant):
    site_id = _two_versions(db, tenant)
    with db.tenant(tenant) as s:
        # Versions no longer on screen: nothing changes.
        with pytest.raises(LookupError):
            prof.approve_choice(s, tenant, site_id, 2, 7, set(), "francesco")
        with pytest.raises(ValueError):
            prof.approve_choice(s, tenant, site_id, 2, 1, {"audience"}, "francesco")
        assert prof.approve_choice(s, tenant, site_id, 2, 1, set(), "francesco") == (1, "kept")
        rows = s.execute(text("SELECT version, status FROM editor.editorial_profiles ORDER BY version")).all()
    assert [tuple(r) for r in rows] == [(1, "approved"), (2, "retired")]
    with db.tenant(tenant) as s:
        prof.save_draft(s, tenant, site_id, dict(V4), "reanalysis", based_on=1)
        every = {r["key"] for r in prof.compare(V2, V4) if r.get("key")}
        assert prof.approve_choice(s, tenant, site_id, 3, 1, every, "francesco") == (3, "approved")


def test_owner_writes_sector_audience_and_tone_in_a_form(db, tenant):
    site_id = _two_versions(db, tenant)
    with db.tenant(tenant) as s:
        # A draft waiting is changed in place.
        assert prof.set_basics(s, tenant, site_id, "francesco", "Telecomunicazioni", "Professionisti", "informativo") == 2
        assert prof.set_basics(s, tenant, site_id, "francesco", "Telecomunicazioni", "Professionisti", "informativo") is None
        draft = s.execute(select(m.editorial_profiles.c.body).where(m.editorial_profiles.c.version == 2)).scalar()
    assert draft["sector"] == {"value": "Telecomunicazioni", "evidence": [], "by": "owner"}


def test_a_topic_that_widens_its_name_is_one_row_and_no_approved_topic_disappears():
    """aitoday.it, 2026-11-19: "Intelligenza artificiale" became "Intelligenza
    artificiale e agenti IA" and showed as a new topic plus one leaving."""
    old = {"subtopics": [{"name": n, "keywords": [n.lower()], "share": 0.14}
                         for n in ("Mondo", "Intelligenza artificiale", "Clima", "Cultura")]}
    new = {"subtopics": [{"name": "Intelligenza artificiale e agenti IA", "keywords": ["agenti", "llm"], "share": 0.16},
                         {"name": "Sport, cultura e clima", "keywords": ["sport"], "share": 0.10}]}
    rows = {r["key"]: r for r in prof.compare(old, new) if r.get("key")}
    assert rows["topic:Intelligenza artificiale e agenti IA"]["kind"] == "changed"
    assert "come «Intelligenza artificiale»" in rows["topic:Intelligenza artificiale e agenti IA"]["before"]
    # Clima and Cultura both fit the new topic; one is its continuation, the other still has its row.
    assert rows["topic:Sport, cultura e clima"]["kind"] == "changed"
    gone = {k for k, r in rows.items() if r.get("kind") == "gone"}
    assert "gone:Mondo" in gone and len(gone) == 2
    body = prof.mix(old, new, gone)  # keep every topic that would leave
    assert len(body["subtopics"]) == 4


def test_a_rereading_does_not_learn_from_the_newsrooms_own_articles(db, tenant, monkeypatch):
    """aitoday.it, 2026-11-19: every article is written by the newsroom, so
    the weekly reading turned its own output into the next week's line."""
    class C:
        def __init__(self, articles):
            self.articles = articles

        def analyse(self, domain):
            return snapshot(self.articles)

    site_id = prof.analyse_site(db, tenant, "blog.example", C(fakesite.ARTICLES))["site_id"]
    with db.tenant(tenant) as s:
        assert prof.approve(s, site_id, 1, "francesco")
    # Every article the site shows now was published by the newsroom.
    monkeypatch.setattr(prof, "own_articles", lambda s, sid: ({slug for slug, *_ in fakesite.SHIFTED}, set()))
    out = prof.analyse_site(db, tenant, "blog.example", C(fakesite.SHIFTED))
    assert out["profile_id"] is None and out["own_articles"] == len(fakesite.SHIFTED)
    with db.tenant(tenant) as s:
        rows = s.execute(text("SELECT version, status FROM editor.editorial_profiles ORDER BY version")).all()
        posts = s.execute(text("SELECT count(*) FROM editor.site_posts")).scalar()
    assert [tuple(r) for r in rows] == [(1, "approved")]
    assert posts >= len(fakesite.SHIFTED)  # still known as covered by the site


def test_a_rereading_adds_topics_but_never_takes_one_away():
    current = {"subtopics": [{"name": "Fibra", "keywords": ["fibra"], "share": 0.5},
                             {"name": "Satelliti", "keywords": ["satellite"], "share": 0.2}],
               "settings": {}}
    body = {"subtopics": [{"name": "Fibra", "keywords": ["fibra"], "share": 0.6},
                          {"name": "Sicurezza", "keywords": ["ransomware"], "share": 0.3}], "settings": {}}
    out = prof._keep_approved_topics(current, body)
    assert [x["name"] for x in out["subtopics"]] == ["Fibra", "Sicurezza", "Satelliti"]
    # A topic the owner took out stays out.
    body = {"subtopics": [{"name": "Fibra", "keywords": ["fibra"], "share": 0.6}],
            "settings": {"removed": [{"name": "Satelliti", "keywords": ["satellite"]}]}}
    assert [x["name"] for x in prof._keep_approved_topics(current, body)["subtopics"]] == ["Fibra"]


def test_without_the_automatic_newsroom_the_owners_choices_weigh_the_topics(db, tenant):
    """Francesco 2026-11-19: an owner who picks 60% politics gives the site
    its direction; the site is not re-read for it."""
    from sqlalchemy import insert

    body = {"subtopics": [{"name": n, "keywords": [n.lower()], "share": 0.25}
                          for n in ("Politica", "Economia", "Sport", "Cultura")], "settings": {}}
    with db.tenant(tenant) as s:
        from editor import repo
        site_id = repo.ensure_site(s, tenant, "blog.example", "Blog")
        pid = prof.save_draft(s, tenant, site_id, body, "analysis")
        assert prof.approve(s, site_id, 1, "francesco")

        def accept(name, n):
            for i in range(n):
                tid = s.execute(insert(m.topics).values(tenant_id=tenant, label=f"{name} {i}", keywords=[name])
                                .returning(m.topics.c.id)).scalar_one()
                s.execute(insert(m.opportunity_scores).values(
                    tenant_id=tenant, topic_id=tid, site_id=site_id, profile_id=pid, formula_version="v",
                    score=50, components={"matched_subtopic": name}, missing=[]))
                s.execute(insert(m.proposals).values(
                    tenant_id=tenant, site_id=site_id, topic_id=tid, profile_id=pid, opportunity=50,
                    title=f"{name} {i}", generated_by="model", status="accepted"))
        accept("Politica", 6)
        accept("Economia", 3)
    assert prof.direction_from_choices(db, tenant, site_id) is None  # 9 choices: too few to say
    with db.tenant(tenant) as s:
        accept("Sport", 1)
    new = prof.direction_from_choices(db, tenant, site_id)
    assert new is not None
    with db.tenant(tenant) as s:
        draft = s.execute(select(m.editorial_profiles).where(m.editorial_profiles.c.id == new)).first()
        shares = {x["name"]: x["share"] for x in draft.body["subtopics"]}
        rows = prof.compare(prof.approved_profile(s, site_id).body, draft.body)
    assert shares == {"Politica": 0.6, "Economia": 0.3, "Sport": 0.1, "Cultura": 0.0}  # Cultura stays
    assert draft.status == "draft" and draft.body["direction"]["accepted"] == 10
    assert not any(r.get("kind") == "gone" for r in rows)
    # With the automatic newsroom the line changes only by the owner's hand.
    with db.tenant(tenant) as s:
        prof.approve(s, site_id, draft.version, "francesco")
        prof._set_settings(s, site_id, auto={"enabled": True})
        accept("Cultura", 20)
    assert prof.direction_from_choices(db, tenant, site_id) is None
