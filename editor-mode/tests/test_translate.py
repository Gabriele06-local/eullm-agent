"""Translations: the site's languages within the plan, one model call per
language, the same sentences with the same sources and checks, and the
translations queued when an article is approved."""

import json

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from editor import auth, models as m, quotas, translate as tr
from editor.api import create_app
from editor.core_client import CoreClient

from conftest import set_plan
from test_drafts import chat
from test_publishing import approved_draft

ENGLISH = {"title": "Bari gets full fibre", "subtitle": "The network is complete",
           "headings": ["Facts"],
           "sentences": ["Open Fiber has worked in Bari.", "The fibre network is complete.", "The works are over."],
           "seo": {"keyphrase": "bari fibre", "title": "Bari gets full fibre", "description": "x" * 130,
                   "slug": "bari-full-fibre"}}


def test_the_plan_decides_how_many_languages(db, tenant):
    did, site_id = approved_draft(db, tenant)
    assert tr.set_languages(db, tenant, site_id, ["en", "it", "EN"]) == ["en"]  # its own language is not one more
    if db.sqlite:  # one newsroom of its own, no plan
        assert tr.set_languages(db, tenant, site_id, list(tr.LANGUAGES))
        tr.set_languages(db, tenant, site_id, ["en"])
    else:
        set_plan(db, tenant, max_languages=2)
        with pytest.raises(tr.LanguageError, match="2 languages"):
            tr.set_languages(db, tenant, site_id, ["en", "de"])
    with pytest.raises(tr.LanguageError, match="unknown"):
        tr.set_languages(db, tenant, site_id, ["xx"])
    with db.tenant(tenant) as s:
        assert tr.site_languages(s, site_id) == ["en"] and tr.missing(s, did) == ["en"]


def test_a_translation_keeps_sentences_sources_and_checks(db, tenant):
    did, site_id = approved_draft(db, tenant)
    wrong = dict(ENGLISH, sentences=ENGLISH["sentences"][:2])  # one sentence lost: asked again
    answers = iter([json.dumps(wrong), json.dumps(ENGLISH)])
    with respx.mock() as router:
        llm = router.post("http://core.test/v1/llm/chat").mock(side_effect=lambda r: chat(next(answers)))
        tid = tr.translate(db, tenant, did, "en", CoreClient("http://core.test", "tok"))
        assert "expected 3 sentences" in json.loads(llm.calls[1].request.content)["messages"][-1]["content"]
    with db.tenant(tenant) as s:
        d = s.execute(select(m.drafts).where(m.drafts.c.id == tid)).one()
        src = s.execute(text("SELECT c.ordinal, c.section, cs.item_id FROM editor.draft_claims c "
                             "JOIN editor.draft_claim_sources cs ON cs.claim_id = c.id WHERE c.draft_id = :d "
                             "ORDER BY c.ordinal"), {"d": tid}).all()
        orig = s.execute(text("SELECT c.ordinal, cs.item_id FROM editor.draft_claims c "
                              "JOIN editor.draft_claim_sources cs ON cs.claim_id = c.id WHERE c.draft_id = :d "
                              "ORDER BY c.ordinal"), {"d": did}).all()
        purpose = s.execute(text("SELECT count(*) FROM editor.llm_usage WHERE purpose = 'translate'")).scalar()
        used = quotas.usage(s).used["drafts_month"]
    assert d.translation_of == did and d.language == "en" and d.status == "draft" and d.title == "Bari gets full fibre"
    assert [(o, i) for o, _, i in src] == [tuple(r) for r in orig] and {sec for _, sec, _ in src} == {"Facts"}
    assert "## Facts" in d.body_md and "## Sources" in d.body_md and d.seo["slug"] == "bari-full-fibre"
    assert purpose == 2 and used == 2  # the original and its translation: two articles of the plan
    # Asked again from the translation, it translates the original.
    with respx.mock() as router:
        router.post("http://core.test/v1/llm/chat").mock(return_value=chat(json.dumps(ENGLISH)))
        again = tr.translate(db, tenant, tid, "de", CoreClient("http://core.test", "tok"))
    with db.tenant(tenant) as s:
        assert s.execute(select(m.drafts.c.translation_of).where(m.drafts.c.id == again)).scalar() == did


def test_an_invented_number_is_kept_for_review(db, tenant):
    did, _ = approved_draft(db, tenant)
    bad = dict(ENGLISH, sentences=["Open Fiber has worked in Bari.", "The network reaches 999 homes.", "The works are over."])
    with respx.mock() as router:
        router.post("http://core.test/v1/llm/chat").mock(return_value=chat(json.dumps(bad)))
        tid = tr.translate(db, tenant, did, "en", CoreClient("http://core.test", "tok"))
    with db.tenant(tenant) as s:
        status, flags = s.execute(select(m.drafts.c.status, m.drafts.c.flags).where(m.drafts.c.id == tid)).one()
    assert status == "needs_review" and "999" in flags[0]["problems"][0]


def test_approving_queues_the_translations(db, tenant):
    did, site_id = approved_draft(db, tenant)
    with db.tenant(tenant) as s:  # back to be approved from the dashboard
        s.execute(text("UPDATE editor.drafts SET status = 'draft', decided_by = NULL, decided_at = NULL WHERE id = :d"),
                  {"d": did})
    token = auth.create_token(db, tenant, "owner-user", "owner")
    c = TestClient(create_app(db, core=CoreClient("http://core.test", "tok")))
    assert c.post("/login", data={"token": token}, follow_redirects=False).status_code == 303
    page = c.get("/site").text
    assert "Lingue degli articoli" in page and 'value="en"' in page
    c.post(f"/ui/sites/{site_id}/languages", data={"languages": ["en", "de"]})
    assert "Tedesco" not in c.get(f"/drafts/{did}").text  # languages by their own name
    r = c.post(f"/ui/drafts/{did}/decision", data={"accept": "1"})
    assert "Traduzioni in scrittura (English, Deutsch)" in r.text
    with db.tenant(tenant) as s:
        queued = s.execute(text("SELECT subject, args FROM editor.jobs WHERE kind = 'translate' ORDER BY id")).all()
    assert [q.subject for q in queued] == [f"draft {did} en", f"draft {did} de"]
    args = queued[0].args if isinstance(queued[0].args, dict) else json.loads(queued[0].args)
    assert args["site_id"] == site_id
    page = c.get(f"/drafts/{did}").text
    assert "In traduzione" in page and "Deutsch" in page
