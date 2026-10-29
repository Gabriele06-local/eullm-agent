"""The automatic newsroom: its settings and guardrails, the hourly choice
of articles, the AI editor-in-chief, the label, the publication with the
translations, the profile written from a brief and the switch on the page."""

import json
import socket
from datetime import UTC, datetime

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from editor import auth, autopilot as ap, drafts, models as m, proposals as props, publishing as pub, repo
from editor import opportunity as opp, profile as prof, translate as tr
from editor.api import create_app
from editor.core_client import CoreClient

from test_drafts import accepted_proposal, chat
from test_proposals import setup_site
from test_publishing import public_resolver
from test_translate import ENGLISH

CORE = "http://core.test/v1/llm/chat"


def core():
    return CoreClient("http://core.test", "tok")


def settings(db, tenant, site_id, **values):
    with db.tenant(tenant) as s:
        assert ap.save(s, site_id, {**ap.DEFAULTS, "enabled": True, "min_opportunity": 0, "hours": [0, 24],
                                    **values})


def test_settings_and_guardrails():
    with pytest.raises(ValueError, match="per_day"):
        ap.check_settings({"per_day": 0})
    with pytest.raises(ValueError, match="hours"):
        ap.check_settings({"hours": [9, 9]})
    cfg = ap.check_settings({"avoid": [" Calcio ", "calcio", ""], "sections": ["Mondo", " "]})
    assert cfg["avoid"] == ["calcio"] and cfg["sections"] == ["Mondo"] and cfg["credit"] is True
    assert ap.avoided("Il CALCIO in tv", ["calcio"]) == "calcio" and ap.avoided("calciomercato", ["calcio"]) is None
    night = {"hours": [22, 6]}
    assert ap.in_hours(night, datetime(2026, 10, 28, 23)) and not ap.in_hours(night, datetime(2026, 10, 28, 12))
    assert ap.hours_left(night, datetime(2026, 10, 28, 23)) == 7
    body = "# T\n\nTesto.[^1]\n\n## Fonti\n\n[^1]: [A](https://a.example), A\n"
    labelled = ap.add_label(body, "it")
    assert labelled.index("senza revisione umana") < labelled.index("## Fonti")
    assert "[Redazia](https://redazia.com)" in labelled and ap.add_label(labelled, "it") == labelled
    assert "redazia.com" not in ap.add_label(body, "en", credit=False)
    lines = ap.brief_lines({"settings": {"auto": {"brief": "Notizie dal mondo", "avoid": ["calcio"]}}})
    assert lines == ["Newsroom brief: Notizie dal mondo", "Never write about: calcio"]


def test_each_hour_takes_its_share_of_the_day(db, tenant):
    site_id = setup_site(db, tenant)
    props.propose(db, tenant, site_id, opp.score_site(db, tenant, site_id), None, limit=5)
    settings(db, tenant, site_id, per_day=1, avoid=["starlink"])
    queued = []
    now = datetime(2026, 10, 28, 11, tzinfo=UTC)
    with db.tenant(tenant) as s:
        s.execute(text("UPDATE editor.proposals SET created_at = :t"), {"t": now})
    out = ap.run(db, tenant, site_id, None, now, "Europe/Rome", queued.append)
    assert out["accepted"] == queued and len(queued) == 1
    with db.tenant(tenant) as s:
        chosen = s.execute(select(m.proposals).where(m.proposals.c.id == queued[0])).one()
        assert chosen.status == "accepted" and chosen.decided_by == ap.AUTO and ap.source_count(s, chosen.id) >= 2
        left = s.execute(select(m.proposals.c.title, m.proposals.c.status)).all()
    assert all(r.status == "rejected" for r in left if "starlink" in r.title.lower())
    # The day's one article is taken: nothing more until tomorrow.
    assert ap.run(db, tenant, site_id, None, now, "Europe/Rome", queued.append) == {"done": 1}
    settings(db, tenant, site_id, hours=[3, 4])
    assert ap.run(db, tenant, site_id, None, now, "Europe/Rome", queued.append) == {"waiting": "hours"}
    settings(db, tenant, site_id, enabled=False)
    assert ap.run(db, tenant, site_id, None, now, "Europe/Rome", queued.append) == {}


def _written(db, tenant, again=None):
    pid, cited = again or accepted_proposal(db, tenant)
    answer = json.dumps({"title": "Bari in fibra", "subtitle": "La rete è completa", "sections": [
        {"heading": "Fatti", "claims": [{"text": "Open Fiber ha lavorato a Bari.", "sources": [cited[0]]},
                                        {"text": "La rete in fibra è completa.", "sources": [cited[0]]},
                                        {"text": "I lavori sono finiti.", "sources": [cited[0]]}]}]})
    with respx.mock() as router:
        router.post(CORE).mock(return_value=chat(answer))
        did = drafts.write_draft(db, tenant, pid, core(), None)
    with db.tenant(tenant) as s:
        site_id = s.execute(select(m.drafts.c.site_id).where(m.drafts.c.id == did)).scalar()
    return did, site_id, (pid, cited)


def test_the_editor_in_chief_lets_it_through_or_stops_it(db, tenant, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    did, site_id, prop = _written(db, tenant)
    settings(db, tenant, site_id, min_sources=1, sections=["Rete", "Mondo"])
    main = pub.add_target(db, tenant, site_id, "webhook", "sito", {"url": "https://hooks.example/it"}, None)
    english = pub.add_target(db, tenant, site_id, "webhook", "site en", {"url": "https://hooks.example/en",
                                                                         "language": "en"}, None)
    tr.set_languages(db, tenant, site_id, ["en"])
    sent, translations = [], []
    with respx.mock() as router:
        check = router.post(CORE).mock(return_value=chat(json.dumps({"publish": True, "problems": [], "section": "rete"})))
        out = ap.finish(db, tenant, did, core(), sent.append, translations.append)
        assert "editor-in-chief" in json.loads(check.calls[0].request.content)["messages"][0]["content"]
    assert out["target"] == "sito" and translations == ["en"] and sent == [out["publication_id"]]
    with db.tenant(tenant) as s:
        d = s.execute(select(m.drafts).where(m.drafts.c.id == did)).one()
        p = s.execute(select(m.publications).where(m.publications.c.id == sent[0])).one()
    assert d.status == "approved" and d.decided_by == ap.AUTO and "senza revisione umana" in d.body_md
    assert p.status == "approved" and p.target_id == main and p.mode == "publish" and p.requested_by == ap.AUTO
    assert "senza revisione umana" in p.payload["markdown"] and p.payload["section"] == "Rete"
    # The translation goes to the English destination, with the English label.
    with respx.mock() as router:
        router.post(CORE).mock(return_value=chat(json.dumps(ENGLISH)))
        tid = tr.translate(db, tenant, did, "en", core())
    out = ap.finish_translation(db, tenant, tid, sent.append)
    with db.tenant(tenant) as s:
        p = s.execute(select(m.publications).where(m.publications.c.id == sent[-1])).one()
    assert out["target"] == "site en" and p.target_id == english and "without human review" in p.payload["markdown"]
    assert p.payload["section"] == "Rete"  # the translation is in its original's section

    # The next one breaks a rule: it stays a draft, with the reason.
    did2, _, _ = _written(db, tenant, prop)
    with respx.mock() as router:
        router.post(CORE).mock(return_value=chat(json.dumps(
            {"publish": False, "problems": ["accuses a named company of fraud without an official source"]})))
        out = ap.finish(db, tenant, did2, core(), sent.append)
    assert out["held"] == ["accuses a named company of fraud without an official source"]
    with db.tenant(tenant) as s:
        assert s.execute(select(m.drafts.c.status).where(m.drafts.c.id == did2)).scalar() == "draft"
        s.execute(text("UPDATE editor.proposals SET decided_by = :a"), {"a": ap.AUTO})
        day = ap.today(s, site_id, datetime(2000, 1, 1, tzinfo=UTC))
    assert day["held"] == 1 and len(sent) == 2
    # Too few sources: stopped before asking the model.
    settings(db, tenant, site_id, min_sources=3)
    did3, _, _ = _written(db, tenant, prop)
    with respx.mock(assert_all_called=False) as router:
        llm = router.post(CORE).mock(return_value=chat(json.dumps({"publish": True})))
        out = ap.finish(db, tenant, did3, core(), sent.append)
    assert not llm.calls and "1 sources cited, 3 needed" in out["held"]


def test_a_new_site_gets_its_profile_from_the_brief(db, tenant):
    with db.tenant(tenant) as s:
        site_id = repo.add_site(s, tenant, "aitoday.example")
    cfg = {**ap.DEFAULTS, "brief": "Notizie dal mondo curate dall'AI", "sections": ["Mondo", "Tecnologia"]}
    answer = {"name": "AI Today", "summary": "Notizie dal mondo.", "sector": "Informazione generale",
              "audience": "Lettori italiani", "tone": "chiaro", "formats": ["news", "bogus"],
              "subtopics": [{"name": "Mondo", "keywords": ["esteri", "world"]},
                            {"name": "Tecnologia", "keywords": ["tecnologia", "technology"]}]}
    with respx.mock() as router:
        router.post(CORE).mock(return_value=chat(json.dumps(answer)))
        version = ap.profile_from_brief(db, tenant, site_id, "it", core(), cfg)
    with db.tenant(tenant) as s:
        p = s.execute(select(m.editorial_profiles).where(m.editorial_profiles.c.site_id == site_id)).one()
        site = s.execute(select(m.sites).where(m.sites.c.id == site_id)).one()
    assert version == 1 and p.status == "draft" and p.body["sector"]["value"] == "Informazione generale"
    assert [x["name"] for x in p.body["subtopics"]] == ["Mondo", "Tecnologia"] and p.body["style"]["formats"] == ["news"]
    assert p.body["settings"]["auto"]["sections"] == ["Mondo", "Tecnologia"] and p.body["settings"]["proposals_per_day"]
    assert site.name == "AI Today" and site.language == "it"


def test_the_switch_on_the_site_page(db, tenant):
    site_id = setup_site(db, tenant)
    token = auth.create_token(db, tenant, "owner-user", "owner")
    c = TestClient(create_app(db, core=core()))
    assert c.post("/login", data={"token": token}, follow_redirects=False).status_code == 303
    assert "Redazione automatica" in c.get("/site").text
    r = c.post(f"/ui/sites/{site_id}/auto", data={
        "brief": "Notizie sulla rete", "sections": "Fibra\nSatellite", "avoid": "calcio, politica", "per_day": "4",
        "min_opportunity": "40", "min_sources": "2", "start": "7", "end": "23", "enabled": "1", "credit": "1"})
    assert "Redazione automatica accesa" in r.text and "Ferma subito" in r.text
    with db.tenant(tenant) as s:
        cfg = ap.settings_of(prof_body(s, site_id))
    assert cfg["enabled"] and cfg["avoid"] == ["calcio", "politica"] and cfg["sections"] == ["Fibra", "Satellite"]
    assert "Redazione automatica fermata" in c.post(f"/ui/sites/{site_id}/auto/stop").text
    with db.tenant(tenant) as s:
        assert not ap.settings_of(prof_body(s, site_id))["enabled"]
    r = c.post(f"/ui/sites/{site_id}/auto", data={"brief": "Notizie sulla rete", "action": "profile", "language": "it"})
    with db.tenant(tenant) as s:
        job = s.execute(text("SELECT kind, args FROM editor.jobs WHERE kind = 'brief'")).one()
    args = job.args if isinstance(job.args, dict) else json.loads(job.args)
    assert args["site_id"] == site_id and args["auto"]["brief"] == "Notizie sulla rete"


def prof_body(s, site_id):
    return s.execute(select(m.editorial_profiles.c.body).where(
        m.editorial_profiles.c.site_id == site_id, m.editorial_profiles.c.status == "approved")).scalar()


def test_sources_shown_discreetly():
    body = ("# T\n\nPrima frase.[^1] Seconda.[^2][^3]\n\n## Fonti\n\n"
            "[^1]: [Rete completa](https://a.example/1), Open Fiber\n"
            "[^2]: [Altro](https://a.example/2), Open Fiber\n"
            "[^3]: [Notizia](https://b.example/x)\n")
    out = drafts.discreet_sources(body)
    assert "[^" not in out and "Prima frase. Seconda." in out
    assert out.endswith("**Fonti:** [Open Fiber](https://a.example/1) · [Notizia](https://b.example/x)\n")
    assert drafts.discreet_sources("# T\n\nTesto.\n") == "# T\n\nTesto.\n"


def test_a_site_with_discreet_sources_publishes_them_in_one_line(db, tenant, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    did, site_id, _ = _written(db, tenant)
    settings(db, tenant, site_id, min_sources=1)
    pub.add_target(db, tenant, site_id, "webhook", "sito", {"url": "https://hooks.example/it"}, None)
    with db.tenant(tenant) as s:
        assert prof.set_sources_style(s, site_id, "list")
    sent = []
    with respx.mock() as router:
        router.post(CORE).mock(return_value=chat(json.dumps({"publish": True, "problems": []})))
        ap.finish(db, tenant, did, core(), sent.append)
    with db.tenant(tenant) as s:
        p = s.execute(select(m.publications).where(m.publications.c.id == sent[0])).one()
        d = s.execute(select(m.drafts.c.body_md).where(m.drafts.c.id == did)).scalar()
    assert "[^1]" in d  # the draft keeps its notes
    assert "[^" not in p.payload["markdown"] and "**Fonti:** [" in p.payload["markdown"]
    assert "senza revisione umana" in p.payload["markdown"]
    token = auth.create_token(db, tenant, "owner-user", "owner")
    c = TestClient(create_app(db, core=core()))
    c.post("/login", data={"token": token}, follow_redirects=False)
    assert "Fonti negli articoli salvate" in c.post(f"/ui/sites/{site_id}/sources-style", data={"style": "notes"}).text
    with db.tenant(tenant) as s:
        assert prof_body(s, site_id)["settings"]["sources_style"] == "notes"
