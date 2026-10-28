"""The site page has the site's numbers at the top, from profile to
publications, and the number of proposals a day changes from there."""

from fastapi.testclient import TestClient
from sqlalchemy import select

from editor import auth, models as m, picks
from editor.api import create_app
from test_proposals import setup_site, NOW


def test_the_site_page_counts_everything_and_sets_the_proposals_a_day(db, tenant):
    site_id = setup_site(db, tenant)
    picks.fill(db, tenant, site_id, 2, None, now=NOW)
    token = auth.create_token(db, tenant, "owner-user", "owner")
    c = TestClient(create_app(db))
    c.post("/login", data={"token": token}, follow_redirects=False)
    page = c.get("/site").text
    for label in ("Fonti attive", "Notizie in 24 ore", "Proposte da decidere", "Bozze da rileggere", "Pubblicazioni"):
        assert label in page
    assert 'action="/ui/sites/%d/per-day"' % site_id in page and 'value="2"' in page
    assert "Da ora 9 proposte" in c.post(f"/ui/sites/{site_id}/per-day", data={"n": "9"}).text
    with db.tenant(tenant) as s:
        body = s.execute(select(m.editorial_profiles.c.body).where(m.editorial_profiles.c.site_id == site_id)).scalar()
    assert body["settings"]["proposals_per_day"] == 9 and body["settings"]["exclude"] == ["calcio"]


def test_the_site_page_sets_the_cover_style(db, tenant):
    site_id = setup_site(db, tenant)
    token = auth.create_token(db, tenant, "owner-user", "owner")
    c = TestClient(create_app(db))
    c.post("/login", data={"token": token}, follow_redirects=False)
    page = c.get("/site").text
    assert 'action="/ui/sites/%d/cover"' % site_id in page and 'value="impact" selected' in page
    assert "Stile delle copertine salvato" in c.post(f"/ui/sites/{site_id}/cover",
                                                      data={"style": "editorial", "text": "1"}).text
    with db.tenant(tenant) as s:
        body = s.execute(select(m.editorial_profiles.c.body).where(m.editorial_profiles.c.site_id == site_id)).scalar()
    assert body["settings"]["cover_style"] == "editorial" and body["settings"]["cover_text"] is False
    assert c.post(f"/ui/sites/{site_id}/cover", data={"style": "boh"}).status_code == 400
