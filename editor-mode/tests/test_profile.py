import json

import pytest

import httpx
import respx
from sqlalchemy import select, text, update

import fakesite
from editor import models as m
from editor import profile as prof
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
