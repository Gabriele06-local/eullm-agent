from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import update

from editor import activity, models as m, progress, queue
from editor.ui import problem
from test_publishing import approved_draft
from test_ui import logged_in

ROME = ZoneInfo("Europe/Rome")


def test_a_job_shows_its_step_while_running_and_its_result_after(db, tenant):
    _, site_id = approved_draft(db, tenant)
    seen = {}

    def propose(tenant_id, subject, args):
        progress.step("Lettura delle fonti: {n} di {total} ({name})", n=3, total=9, name="Fonte")
        with db.tenant(tenant_id) as s:
            seen.update(activity.recent(s, ROME, problem))
        return {"site_id": args["site_id"], "proposals": 0, "why": "no_news"}

    queue.enqueue(db, tenant, "proposals", f"site {site_id}", {"site_id": site_id})
    with db.tenant(tenant) as s:
        queued = activity.recent(s, ROME, problem)
    assert queued["active"] and queued["items"][0]["state"].startswith("In coda")
    assert queue.drain(db, {"proposals": propose}, tenant_id=tenant) == 1
    running = seen["items"][0]
    assert running["status"] == "running" and running["state"].startswith("In corso")
    assert running["step"] == "Lettura delle fonti: 3 di 9 (Fonte)" and running["where"] == "blog.example"
    with db.tenant(tenant) as s:
        ended = activity.recent(s, ROME, problem)
    item = ended["items"][0]
    # No news at all in 72 hours is a stopped collection: it looks like a failure.
    assert not ended["active"] and item["status"] == "failed" and item["state"].startswith("Finito alle")
    assert "Nessuna notizia raccolta nelle ultime 72 ore" in item["lines"][0]


def test_a_failed_job_says_why_and_where_it_stopped(db, tenant):
    approved_draft(db, tenant)

    def collect(tenant_id, subject, args):
        progress.step("Raggruppo le notizie in temi")
        raise RuntimeError("the model did not answer")

    queue.enqueue(db, tenant, "collect", "now")
    queue.drain(db, {"collect": collect}, tenant_id=tenant)
    with db.tenant(tenant) as s:
        item = activity.recent(s, ROME, problem)["items"][0]
    assert item["status"] == "failed" and item["where"] == "tutti i siti"
    assert "the model did not answer" in item["lines"][0] and "Raggruppo le notizie in temi" in item["lines"][1]


def test_work_waiting_with_nothing_running_is_reported(db, tenant):
    approved_draft(db, tenant)
    jid = queue.enqueue(db, tenant, "collect", "now")
    with db.tenant(tenant) as s:
        s.execute(update(m.jobs).where(m.jobs.c.id == jid).values(run_after=datetime.now(UTC) - timedelta(minutes=5)))
        out = activity.recent(s, ROME, problem)
    assert out["stalled"] and out["items"][0]["state"] == "In coda da 5 min"


def test_desk_shows_every_site_and_the_activity(db, tenant):
    _, site_id = approved_draft(db, tenant)
    c = logged_in(db, tenant, "owner")
    page = c.get("/").text
    assert "<h1>Desk</h1>" in page and 'id="activity"' in page and "blog.example" in page
    assert f'/ui/sites/{site_id}/review?back=/' in page and "Prossimo passo" in page
    r = c.post(f"/ui/sites/{site_id}/review?back=/", follow_redirects=False)
    assert r.headers["location"] == "/"
    box = c.get("/ui/activity").text
    assert box.lstrip().startswith('<section id="activity"') and 'data-active="1"' in box and "Analisi del sito" in box
    r = c.post(f"/ui/proposals/now?back=/&site={site_id}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    # Every page carries the box while work runs.
    assert "Analisi del sito" in c.get("/sources").text
