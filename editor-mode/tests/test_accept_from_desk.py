"""Accepting proposals from the Desk and from Trend e piano: every page
still opens while the drafts are being written."""

import json

from fastapi.testclient import TestClient
from sqlalchemy import text
from editor import auth, picks
from editor.api import create_app
from editor.core_client import CoreClient
from test_proposals import setup_site, NOW, PROFILE


def test_pages_open_while_accepted_proposals_are_written(db, tenant):
    site_id = setup_site(db, tenant)
    with db.tenant(tenant) as s:
        s.execute(text("UPDATE editor.editorial_profiles SET body = :b"),
                  {"b": json.dumps({**PROFILE, "settings": {**PROFILE["settings"], "proposals_per_day": 3}})})
    ids = picks.fill(db, tenant, site_id, 3, None, now=NOW)
    token = auth.create_token(db, tenant, "owner-user", "owner")
    c = TestClient(create_app(db, core=CoreClient("http://core.test", "tok")))
    c.post("/login", data={"token": token}, follow_redirects=False)
    for back in ("/", "/trends"):
        assert c.get(back).status_code == 200
    for i, back in zip(ids, ("/", "/trends", "/")):
        r = c.post(f"/ui/proposals/{i}", data={"accept": "1", "back": back})
        assert r.status_code == 200, r.text[:300]
    for p in ("/", "/trends", "/drafts", "/activity", "/index", "/sources", "/site"):
        assert c.get(p).status_code == 200, p
