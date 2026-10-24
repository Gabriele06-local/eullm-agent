import json

import httpx
import respx
from sqlalchemy import text

from editor import core_client, drafts
from editor.core_client import CoreClient, CoreError
from test_drafts import ARTICLE_PAGE, accepted_proposal, chat


def rows(db, tenant):
    with db.tenant(tenant) as s:
        return s.execute(text("SELECT purpose, model, input_tokens, output_tokens, site_id, ref, duration_ms, error "
                              "FROM editor.llm_usage ORDER BY id")).all()


def good_article(first):
    return json.dumps({"title": "Bari cablata in fibra: cosa cambia", "subtitle": "La rete FTTH di Open Fiber è completa",
                       "sections": [
                           {"heading": None, "claims": [
                               {"text": "A Bari la rete in fibra fino a casa di Open Fiber è stata completata.", "sources": [first]},
                               {"text": "La copertura riguarda 120.000 unità immobiliari.", "sources": [first]}]},
                           {"heading": "I lavori", "claims": [
                               {"text": "Il cantiere è durato 18 mesi in 3 quartieri.", "sources": [first]},
                               {"text": "Le velocità annunciate arrivano a 10 Gbit/s.", "sources": [first]}]}]})


def test_every_call_is_recorded_when_it_happens_even_when_the_work_fails(db, tenant, monkeypatch):
    pid, cited = accepted_proposal(db, tenant)
    monkeypatch.setattr(core_client, "RETRY_WAITS", (0,))
    answers = iter([httpx.Response(502, json={"error": "openai error 500 Internal Server Error"}),
                    chat("not json"), chat("still not json"), chat("{}")])
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://(news|rete)\.example/.*").mock(
            return_value=httpx.Response(200, text=ARTICLE_PAGE, headers={"content-type": "text/html"}))
        router.post("http://core.test/v1/llm/chat").mock(side_effect=lambda r: next(answers))
        with httpx.Client() as client:
            try:
                drafts.write_draft(db, tenant, pid, CoreClient("http://core.test", "tok"), client)
            except CoreError:
                pass
            else:
                raise AssertionError("the draft should have failed")
    got = rows(db, tenant)
    # The provider error, then three answers that were paid for although no draft came out.
    assert [r.error is not None for r in got] == [True, False, False, False]
    assert "openai error 500" in got[0].error and got[0].input_tokens is None
    assert all(r.purpose == "draft" and r.ref == f"proposal:{pid}" and r.site_id for r in got)
    assert [r.input_tokens for r in got[1:]] == [3000, 3000, 3000] and all(r.duration_ms is not None for r in got)

    # Calls made outside a tagged block (the command line, a test) are not recorded.
    with respx.mock() as router:
        router.post("http://core.test/v1/llm/chat").mock(return_value=chat("ok"))
        CoreClient("http://core.test", "tok").chat([{"role": "user", "content": "hi"}])
    assert len(rows(db, tenant)) == 4


def test_the_console_shows_tokens_per_step_model_site_and_article(db, tenant):
    from editor import accounts
    from editor.db import Database
    from test_billing import client, unique

    pid, cited = accepted_proposal(db, tenant)
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://(news|rete)\.example/.*").mock(
            return_value=httpx.Response(200, text=ARTICLE_PAGE, headers={"content-type": "text/html"}))
        router.post("http://core.test/v1/llm/chat").mock(return_value=chat(good_article(cited[0])))
        with httpx.Client() as http:
            drafts.write_draft(db, tenant, pid, CoreClient("http://core.test", "tok"), http)

    admin = Database.from_url(db.admin_url)
    c, _ = client(db)
    op = f"{unique('op')}@example.com"
    accounts.create_user(admin, op, password="password operatore", operator=True)
    c.post("/login", data={"email": op, "password": "password operatore"})
    page = c.get("/admin/usage?days=7").text
    assert 'action="/logout"' in page
    assert "Consumi del modello" in page and "Scrittura delle bozze" in page and "6.000" in page
    assert "Bari cablata in fibra" in page and "senza prezzo" in page

    # The model behind the fake Core has no price yet; with one, the cost is estimated.
    model = rows(db, tenant)[0].model
    page = c.post("/admin/usage/prices", data={"model": model, "input_per_mtok": "0,40", "output_per_mtok": "2"}).text
    assert "salvato" in page
    # Two calls (the draft is short, it is asked once to expand, the same answer is kept):
    # 6000 tokens in at 0.40 €/M + 1600 out at 2 €/M = 0.0056 €
    assert "€ 0,0056" in page
    assert "servono due numeri" in c.post("/admin/usage/prices", data={"model": model, "input_per_mtok": "x",
                                                                         "output_per_mtok": "1"}).text
    csv = c.get("/admin/usage.csv?days=7")
    assert csv.status_code == 200 and "draft" in csv.text and f"proposal:{pid}" in csv.text
    assert "tolto" in c.post("/admin/usage/prices/delete", data={"model": model}).text
