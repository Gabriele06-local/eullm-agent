import json
import re

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



def test_the_checks_findings_are_shown_in_the_readers_language():
    found = drafts.NUMBERS_MISSING + "35, 3.5"
    assert drafts.flag_text(found, "it") == "numeri che non si trovano nelle fonti citate: 35, 3.5"
    assert drafts.flag_text(found, "en") == "numbers not found in the cited sources: 35, 3.5"
    assert drafts.flag_text(drafts.NO_SOURCE, "de") != drafts.NO_SOURCE
    assert drafts.flag_text(drafts.COPIED, "it") == "copia parola per parola un passaggio di una fonte"
    assert drafts.flag_text("something else", "it") == "something else"


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
    # A short article stays short: it is never sent back to be padded.
    answers = iter([bad, good])
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://(news|rete)\.example/.*").mock(return_value=httpx.Response(200, text=ARTICLE_PAGE, headers={"content-type": "text/html"}))
        llm = router.post("http://core.test/v1/llm/chat").mock(side_effect=lambda r: chat(next(answers)))
        with httpx.Client() as client:
            did = drafts.write_draft(db, tenant, pid, CoreClient("http://core.test", "tok"), client)
        retry_prompt = json.loads(llm.calls[1].request.content)["messages"][-1]["content"]
        assert "claim without sources" in retry_prompt and len(llm.calls) == 2
        first_prompt = json.loads(llm.calls[0].request.content)["messages"][-1]["content"]
        assert "MAIN" in first_prompt and "Nuova ricetta della pizza" not in first_prompt
    with db.tenant(tenant) as s:
        d = s.execute(select(m.drafts).where(m.drafts.c.id == did)).one()
        claims = s.execute(text("SELECT c.ordinal, count(cs.item_id) FROM editor.draft_claims c "
                                "LEFT JOIN editor.draft_claim_sources cs ON cs.claim_id = c.id WHERE c.draft_id = :d "
                                "GROUP BY c.ordinal ORDER BY c.ordinal"), {"d": did}).all()
        pstatus = s.execute(text("SELECT status FROM editor.proposals WHERE id = :p"), {"p": pid}).scalar()
        content = s.execute(text("SELECT content FROM editor.source_items WHERE id = :i"), {"i": first}).scalar()
        usage = s.execute(text("SELECT count(*) FROM editor.llm_usage WHERE purpose = 'draft'")).scalar()
    assert d.status == "draft" and d.flags == [] and d.version == 1
    assert len(claims) == 4 and all(n >= 1 for _, n in claims)
    assert "## I lavori" in d.body_md and "[^1]" in d.body_md and "## Fonti" in d.body_md and "https://news.example/" in d.body_md
    assert pstatus == "drafted" and "120.000" in content and usage == 2


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


def test_article_text_is_not_read_where_the_publisher_reserves_it_against_ai(db, tenant):
    """A publisher that shuts out the AI assistants' readers, or marks its page
    tdm-reservation, gets written from its feed summary only."""
    _, cited = accepted_proposal(db, tenant)
    with db.tenant(tenant) as s:
        urls = [s.execute(text("SELECT url FROM editor.source_items WHERE id = :i"), {"i": i}).scalar() for i in cited[:2]]
    hosts = {httpx.URL(u).host for u in urls}
    marked = ARTICLE_PAGE.replace("<head>", '<head><meta name="tdm-reservation" content="1">', 1)
    with respx.mock(assert_all_called=False) as router, httpx.Client() as c:
        for h in hosts:
            router.get(f"https://{h}/.well-known/tdmrep.json").mock(return_value=httpx.Response(404))
        router.get(f"https://{httpx.URL(urls[0]).host}/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: PerplexityBot\nDisallow: /\n"))
        first = router.get(urls[0]).mock(return_value=httpx.Response(200, text=ARTICLE_PAGE, headers={"content-type": "text/html"}))
        drafts.fetch_contents(db, tenant, cited[:1], c)
        assert not first.called
    with respx.mock(assert_all_called=False) as router, httpx.Client() as c:
        for h in hosts:
            router.get(f"https://{h}/robots.txt").mock(return_value=httpx.Response(404))
            router.get(f"https://{h}/.well-known/tdmrep.json").mock(return_value=httpx.Response(404))
        second = router.get(urls[1]).mock(return_value=httpx.Response(200, text=marked, headers={"content-type": "text/html"}))
        drafts.fetch_contents(db, tenant, cited[1:2], c)
        assert second.called
    with db.tenant(tenant) as s:
        rows = [s.execute(text("SELECT content, content_fetched_at FROM editor.source_items WHERE id = :i"), {"i": i}).one()
                for i in cited[:2]]
    assert len(rows) == 2 and all(r.content is None and r.content_fetched_at is not None for r in rows)


def test_an_article_tells_one_event(db, tenant):
    """Other outlets' reports of the same event come in; news of the same
    sector does not, nor a cited item about something else."""
    from sqlalchemy import text as sql

    from editor.drafts import event_items
    from test_proposals import setup_site

    setup_site(db, tenant)
    with db.tenant(tenant) as s:
        ids = {r.url: r.id for r in s.execute(sql("SELECT id, url FROM editor.source_items"))}
        main, same = event_items(s, [ids["https://news.example/3"], ids["https://news.example/1"]],
                                 "Bari cablata in fibra da Open Fiber")
    assert main == [ids["https://news.example/1"]]  # Starlink's prices are another story
    assert ids["https://rete.example/2"] in same
    assert ids["https://news.example/5"] not in same and ids["https://news.example/3"] not in same


def test_same_event_is_most_of_a_title_not_a_shared_word():
    from editor.drafts import _same_event
    from editor.text import item_terms, tokens

    anchor = set(tokens("Pil Usa, crescita del 3,4% nel terzo trimestre"))
    assert _same_event(anchor, item_terms("Usa, il Pil cresce del 3,4% nel terzo trimestre"))
    assert not _same_event(anchor, item_terms("Aramco, utile in calo nel terzo trimestre"))
    trump = set(tokens("Trump firma il decreto sui dazi ai chip"))
    assert not _same_event(trump, item_terms("Amazon licenzia 14.000 dipendenti, Trump commenta"))


def test_two_letter_acronyms_are_content_words():
    from editor.text import tokens

    assert {"ai", "eu", "us", "uk"} <= set(tokens("AI act, EU rules, US and UK elections"))
    assert "ai" not in tokens("Gli aiuti vanno ai comuni")
    assert not {"si", "ci", "ne", "el", "en"} & set(tokens("Se ne parla, ci si vede; el pueblo en fiesta"))


def test_arxiv_is_read_in_full():
    assert drafts.reading_url("https://arxiv.org/abs/2611.01234v2") == "https://arxiv.org/html/2611.01234v2"
    assert drafts.reading_url("https://arxiv.org/pdf/2611.01234") == "https://arxiv.org/html/2611.01234"
    assert drafts.reading_url("https://news.example/1") == "https://news.example/1"


def test_machine_marks_are_taken_out_of_the_text():
    from editor.text import humanize
    assert humanize("Il titolo — dice tutto") == "Il titolo, dice tutto"
    assert humanize("Dal 2020–2024 i dati -- e non solo... crescono…") == "Dal 2020-2024 i dati, e non solo, crescono."
    assert humanize("Un modello open-source, ben fatto") == "Un modello open-source, ben fatto"
    assert humanize("Fine — .") == "Fine."


def test_the_original_document_is_read_and_can_be_cited(db, tenant):
    """A news story linking an official document: the document is read and
    given to the model as ORIGINAL, citable, so its figure wins over the
    outlet's (the US GDP article of 2026-11-03)."""
    pid, cited = accepted_proposal(db, tenant)
    page = ARTICLE_PAGE.replace("</article>", '<p>Il dato del <a href="https://www.bea.gov/news/2026/gdp-q3">'
                                "Bureau of Economic Analysis</a>.</p></article>")
    official = "<html><head><title>GDP, Third Quarter 2026</title></head><body><article><p>" + \
        "Real gross domestic product increased at an annual rate of 3.4 percent in the third quarter of 2026. " * 12 + \
        "</p></article></body></html>"
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://(news|rete)\.example/.*").mock(
            return_value=httpx.Response(200, text=page, headers={"content-type": "text/html"}))
        router.get(url__regex=r"https://www\.bea\.gov/robots\.txt").mock(return_value=httpx.Response(404))
        router.get("https://www.bea.gov/.well-known/tdmrep.json").mock(return_value=httpx.Response(404))
        router.get("https://www.bea.gov/news/2026/gdp-q3").mock(
            return_value=httpx.Response(200, text=official, headers={"content-type": "text/html"}))
        holder = {}

        def answer(request):
            prompt = json.loads(request.content)["messages"][1]["content"]
            doc = int(re.search(r"\[(\d+)\] ORIGINAL GDP, Third Quarter 2026", prompt).group(1))
            holder["doc"] = doc
            return chat(json.dumps({"title": "Pil Usa, crescita del 3,4% annualizzato", "sections": [{"heading": None, "claims": [
                {"text": "Il Pil reale degli Stati Uniti è cresciuto del 3.4 per cento annualizzato nel terzo trimestre.",
                 "sources": [doc]},
                {"text": "Open Fiber ha completato la rete a Bari.", "sources": [cited[0]]},
                {"text": "I lavori sono durati 18 mesi.", "sources": [cited[0]]}]}]}))

        router.post("http://core.test/v1/llm/chat").mock(side_effect=answer)
        with httpx.Client() as client:
            did = drafts.write_draft(db, tenant, pid, CoreClient("http://core.test", "tok"), client)
    with db.tenant(tenant) as s:
        d = s.execute(select(m.drafts).where(m.drafts.c.id == did)).one()
        dup = s.execute(text("SELECT duplicate_of FROM editor.source_items WHERE id = :i"), {"i": holder["doc"]}).scalar()
    assert d.flags == [] and "bea.gov" in d.body_md and dup in cited


def test_document_links_are_official_and_in_the_text():
    from editor.html_extract import parse_html

    page = parse_html('<nav><a href="https://www.istat.it/menu">Istat</a></nav><article><p>Secondo '
                      '<a href="https://www.istat.it/comunicato/pil">Istat</a> e <a href="https://blog.example/x">un blog</a>, '
                      '<a href="https://eur-lex.europa.eu/eli/reg/2024/1689">l\'AI Act</a>.</p></article>', "https://news.example/1")
    assert drafts.document_links(page, "https://news.example/1") == [
        "https://www.istat.it/comunicato/pil", "https://eur-lex.europa.eu/eli/reg/2024/1689"]


def test_published_articles_can_be_written_again_to_compare(db, tenant, monkeypatch, capsys):
    import pytest

    from editor import cli, runtime

    if not db.admin_url.startswith("postgres"):
        pytest.skip("the command line opens its own connection: PostgreSQL only")
    pid, cited = accepted_proposal(db, tenant)
    good = json.dumps({"title": "Bari cablata in fibra", "sections": [{"heading": None, "claims": [
        {"text": "A Bari la rete in fibra di Open Fiber è completa.", "sources": [cited[0]]},
        {"text": "La copertura riguarda 120.000 unità immobiliari.", "sources": [cited[0]]},
        {"text": "I lavori sono durati 18 mesi.", "sources": [cited[0]]}]}]})
    with respx.mock(assert_all_called=False) as router:
        router.post("http://core.test/v1/llm/chat").mock(return_value=chat(good))
        did = drafts.write_draft(db, tenant, pid, CoreClient("http://core.test", "tok"), None)
    drafts.decide(db, tenant, did, True, "redazione automatica")
    monkeypatch.setenv("EDITOR_DATABASE_URL", db.admin_url)
    runtime.settings.cache_clear()
    try:
        cli.main(["redraft", "blog.example", "--tenant", tenant, "--since", "2026-01-01"])
    finally:
        runtime.settings.cache_clear()
    assert f"proposal {pid}: Bari cablata in fibra" in capsys.readouterr().out
    with db.tenant(tenant) as s:
        job = s.execute(text("SELECT kind, args FROM editor.jobs WHERE subject = :s"), {"s": f"proposal {pid}"}).one()
    args = job.args if isinstance(job.args, dict) else json.loads(job.args)
    assert job.kind == "draft" and args["compare"] is True


SOURCE_RUN = ("Terna registra oltre 100 gigawatt di richieste di connessione per nuovi data center in tutta Italia "
              "secondo i dati di ottobre")


def _copying(db, tenant, rephrased: str):
    """A draft whose first sentence copies its source, written three times
    the same way, then the rewording asked for it."""
    from editor.db import Database

    pid, cited = accepted_proposal(db, tenant)
    admin = Database.from_url(db.admin_url)
    with admin.engine.begin() as conn:
        conn.execute(text("UPDATE editor.source_items SET summary = :s WHERE id = :i"), {"s": SOURCE_RUN, "i": cited[0]})
    admin.engine.dispose()
    one = [cited[0]]
    article = json.dumps({"title": "Data center e rete elettrica", "subtitle": "Le richieste crescono", "sections": [
        {"heading": None, "claims": [
            {"text": "Terna registra oltre 100 gigawatt di richieste di connessione per nuovi data center in tutta Italia.",
             "sources": one},
            {"text": "Le richieste riguardano i centri dati.", "sources": one},
            {"text": "Il dato si riferisce al mese di ottobre.", "sources": one}]}]})
    asked = []

    def answer(request):
        asked.append(json.loads(request.content)["messages"][-1]["content"])
        if len(asked) <= 3:
            return chat(article)
        return chat(json.dumps({"sentences": {"0": rephrased}}))

    with respx.mock() as router:
        router.post("http://core.test/v1/llm/chat").mock(side_effect=answer)
        did = drafts.write_draft(db, tenant, pid, CoreClient("http://core.test", "tok"), None)
    with db.tenant(tenant) as s:
        d = s.execute(select(m.drafts).where(m.drafts.c.id == did)).one()
    return d, asked


def test_copied_sentences_are_reworded_without_changing_what_they_say(db, tenant):
    """Francesco 2026-11-05: why so many copied sentences, if it knows? And
    "fai in modo che non falsi le frasi". The writer is told the words it
    copied; what it still copies is reworded in place, and a rewording that
    changes a number or drops a name is not taken."""
    good = "In Italia le domande di allaccio di nuovi centri dati alla rete di Terna superano i 100 gigawatt."
    d, asked = _copying(db, tenant, good)
    assert 'these words are the source\'s: "terna registra oltre 100 gigawatt' in asked[1]
    assert len(asked) == 4 and d.status == "draft" and not d.flags and "domande di allaccio" in d.body_md


def test_a_rewording_that_changes_a_figure_or_a_name_is_not_taken(db, tenant):
    d2, _ = _copying(db, tenant, "In Italia le domande di allaccio alla rete di Terna superano i 120 gigawatt.")
    assert d2.status == "needs_review" and "120" not in d2.body_md and "Terna registra oltre 100" in d2.body_md
    assert not drafts.faithful("Lo dice Terna, con 100 GW.", "Lo dicono in 100 GW.", [])  # a name dropped
    src = [type("S", (), {"text": "Secondo Terna le richieste sono 100."})()]
    assert not drafts.faithful("Terna conta 100 richieste.", "Le richieste contate sono 100.", src)  # the subject too
    assert drafts.faithful("Terna conta 100 richieste.", "Per Terna le richieste sono 100 in tutto.", src)
