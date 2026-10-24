import json

import httpx
import respx
from sqlalchemy import select, text

from editor import drafts, models as m, opportunity as opp, proposals as props
from editor.core_client import CoreClient
from test_proposals import setup_site

ARTICLE_PAGE = """<html><head><title>Bari</title></head><body><article>
<p>Open Fiber ha completato a Bari la rete FTTH che raggiunge 120.000 unità immobiliari, con velocità fino a 10 Gbit/s.</p>
<p>I lavori sono durati 18 mesi e hanno coinvolto 3 quartieri della città, secondo quanto comunicato dall'azienda.</p>
</article></body></html>"""


def _article_doc(**over):
    claims = [{"text": f"Claim number {i} about fiber networks", "sources": [7]} for i in range(3)]
    doc = {"title": "Fiber rollout accelerates in southern Italy",
           "subtitle": "The network is complete",
           "sections": [{"heading": "Facts", "claims": claims}]}
    doc.update(over)
    return doc


def test_null_subtitle_becomes_empty():
    assert drafts.parse_article(_article_doc(subtitle=None), {7}).subtitle == ""


def test_missing_subtitle_becomes_empty():
    doc = _article_doc()
    del doc["subtitle"]
    assert drafts.parse_article(doc, {7}).subtitle == ""


def test_numbers_and_copy_checks():
    assert drafts.numbers("Copre 120.000 case al 35% e 3,5 Gbit") == {"120000", "35", "3.5"}
    assert drafts.numbers("1,200 utenti") == {"1200"}
    src = "Open Fiber ha completato a Bari la rete FTTH che raggiunge centoventimila unità immobiliari con velocità alte"
    assert drafts.copied_run("Secondo il comunicato Open Fiber ha completato a Bari la rete FTTH che raggiunge centoventimila unità", src)
    assert not drafts.copied_run("A Bari la fibra arriva in molte case", src)


def accepted_proposal(db, tenant):
    site_id = setup_site(db, tenant)
    ops = opp.score_site(db, tenant, site_id)
    pid = props.propose(db, tenant, site_id, ops, None)[0]
    props.decide(db, tenant, pid, True, "francesco")
    with db.tenant(tenant) as s:
        cited = list(s.execute(text("SELECT item_id FROM editor.proposal_citations WHERE proposal_id = :p"), {"p": pid}).scalars())
    return pid, cited


def chat(content):
    return httpx.Response(200, json={"content": content, "tool_calls": [], "usage": {"input_tokens": 3000, "output_tokens": 800}, "cost": 0.01})


def test_draft_every_claim_has_a_source(db, tenant):
    pid, cited = accepted_proposal(db, tenant)
    first = cited[0]
    bad = json.dumps({"title": "Bari in fibra", "sections": [{"heading": None, "claims": [
        {"text": "Open Fiber ha cablato Bari.", "sources": [first]},
        {"text": "La rete raggiunge 500.000 case.", "sources": [first]},  # number not in the sources
        {"text": "Un'opinione senza fonte.", "sources": []},
    ]}]})
    good = json.dumps({"title": "Bari cablata in fibra: cosa cambia", "subtitle": "La rete FTTH di Open Fiber è completa",
                       "sections": [
                           {"heading": None, "claims": [
                               {"text": "A Bari la rete in fibra fino a casa di Open Fiber è stata completata.", "sources": [first]},
                               {"text": "La copertura riguarda 120.000 unità immobiliari.", "sources": [first]}]},
                           {"heading": "I lavori", "claims": [
                               {"text": "Il cantiere è durato 18 mesi in 3 quartieri.", "sources": [first]},
                               {"text": "Le velocità annunciate arrivano a 10 Gbit/s.", "sources": [first]}]}]})
    # Far under the target length: asked once to expand, and the longer one is kept.
    extra = [{"text": t, "sources": [first]} for t in (
        "Il piano di Open Fiber per la città riguarda le aree dove la rete mancava.",
        "Le famiglie dei quartieri coperti possono chiedere l'attivazione a un operatore.",
        "Il cantiere ha toccato strade e cortili di tutti i quartieri previsti.")]
    longer = json.loads(good)
    longer["sections"].append({"heading": "Il contesto", "claims": extra})
    answers = iter([bad, good, json.dumps(longer)])
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://(news|rete)\.example/.*").mock(return_value=httpx.Response(200, text=ARTICLE_PAGE, headers={"content-type": "text/html"}))
        llm = router.post("http://core.test/v1/llm/chat").mock(side_effect=lambda r: chat(next(answers)))
        with httpx.Client() as client:
            did = drafts.write_draft(db, tenant, pid, CoreClient("http://core.test", "tok"), client)
        retry_prompt = json.loads(llm.calls[1].request.content)["messages"][-1]["content"]
        assert "claim without sources" in retry_prompt
        expand_prompt = json.loads(llm.calls[2].request.content)["messages"][-1]["content"]
        assert f"the target is about {drafts.TARGET_WORDS}" in expand_prompt
    with db.tenant(tenant) as s:
        d = s.execute(select(m.drafts).where(m.drafts.c.id == did)).one()
        claims = s.execute(text("SELECT c.ordinal, count(cs.item_id) FROM editor.draft_claims c "
                                "LEFT JOIN editor.draft_claim_sources cs ON cs.claim_id = c.id WHERE c.draft_id = :d "
                                "GROUP BY c.ordinal ORDER BY c.ordinal"), {"d": did}).all()
        pstatus = s.execute(text("SELECT status FROM editor.proposals WHERE id = :p"), {"p": pid}).scalar()
        content = s.execute(text("SELECT content FROM editor.source_items WHERE id = :i"), {"i": first}).scalar()
        usage = s.execute(text("SELECT count(*) FROM editor.llm_usage WHERE purpose = 'draft'")).scalar()
    assert d.status == "draft" and d.flags == [] and d.version == 1
    assert len(claims) == 7 and all(n >= 1 for _, n in claims)
    assert "## Il contesto" in d.body_md and "## I lavori" in d.body_md and "[^1]" in d.body_md and "## Fonti" in d.body_md and "https://news.example/" in d.body_md
    assert pstatus == "drafted" and "120.000" in content and usage == 3


def test_unfixable_draft_is_kept_for_review(db, tenant):
    pid, cited = accepted_proposal(db, tenant)
    invented = json.dumps({"title": "Bari cablata in fibra", "sections": [{"heading": None, "claims": [
        {"text": "La rete copre 999 case.", "sources": [cited[0]]},
        {"text": "Open Fiber ha lavorato a Bari.", "sources": [cited[0]]},
        {"text": "I lavori sono finiti.", "sources": [cited[0]]}]}]})
    with respx.mock(assert_all_called=False) as router:
        router.post("http://core.test/v1/llm/chat").mock(return_value=chat(invented))
        did = drafts.write_draft(db, tenant, pid, CoreClient("http://core.test", "tok"), None)
    with db.tenant(tenant) as s:
        status, flags = s.execute(select(m.drafts.c.status, m.drafts.c.flags).where(m.drafts.c.id == did)).one()
    assert status == "needs_review"
    assert flags[0]["claim"] == 0 and "999" in flags[0]["problems"][0]
    assert drafts.decide(db, tenant, did, False, "francesco")
    assert not drafts.decide(db, tenant, did, True, "francesco")


def test_only_accepted_proposals_get_drafts(db, tenant):
    site_id = setup_site(db, tenant)
    pid = props.propose(db, tenant, site_id, opp.score_site(db, tenant, site_id), None)[0]
    try:
        drafts.write_draft(db, tenant, pid, CoreClient("http://core.test", "tok"))
    except ValueError as e:
        assert "accepted" in str(e)
    else:
        raise AssertionError("draft written for a proposal nobody accepted")


def test_article_text_is_not_read_where_robots_forbid_it(db, tenant):
    _, cited = accepted_proposal(db, tenant)
    with db.tenant(tenant) as s:
        url = s.execute(text("SELECT url FROM editor.source_items WHERE id = :i"), {"i": cited[0]}).scalar()
    host = httpx.URL(url).host
    with respx.mock(assert_all_called=False) as router, httpx.Client() as c:
        router.get(f"https://{host}/robots.txt").mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
        page = router.get(url).mock(return_value=httpx.Response(200, text=ARTICLE_PAGE, headers={"content-type": "text/html"}))
        drafts.fetch_contents(db, tenant, cited[:1], c)
    assert not page.called
    with db.tenant(tenant) as s:
        content, at = s.execute(text("SELECT content, content_fetched_at FROM editor.source_items WHERE id = :i"),
                                {"i": cited[0]}).one()
    assert content is None and at is not None


def test_related_items_give_the_article_context(db, tenant):
    from sqlalchemy import text as sql

    from editor.drafts import related_items
    from test_proposals import setup_site

    setup_site(db, tenant)
    with db.tenant(tenant) as s:
        ids = {r.url: r.id for r in s.execute(sql("SELECT id, url FROM editor.source_items"))}
        found = related_items(s, [ids["https://news.example/1"]])
    assert ids["https://rete.example/2"] in found
    assert ids["https://news.example/5"] not in found  # the pizza recipe has nothing to do with it
