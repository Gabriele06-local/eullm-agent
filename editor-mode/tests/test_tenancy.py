"""F6: two tenants on the same database see nothing of each other, cannot
change their own plan, and are held to their limits."""

import json
import socket

import httpx
import psycopg
import pytest
import respx
from sqlalchemy import text

from editor import auth, briefing, quotas, repo
from editor.collectors import Item
from editor.core_client import CoreClient, CoreError
from editor.ingest import SourceReport, store_items
from editor.quotas import QuotaExceeded


def tenant_tables(admin_url):
    with psycopg.connect(admin_url) as conn:
        return [r[0] for r in conn.execute(
            "SELECT c.table_name FROM information_schema.columns c JOIN information_schema.tables t "
            "USING (table_schema, table_name) WHERE c.table_schema = 'editor' AND c.column_name = 'tenant_id' "
            "AND t.table_type = 'BASE TABLE' ORDER BY 1").fetchall()]


def set_plan(admin_url, tenant, **limits):
    sets = ", ".join(f"{k} = %({k})s" for k in limits)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f"UPDATE editor.tenants SET {sets} WHERE tenant_id = %(t)s", limits | {"t": tenant})


def populate(db, tenant, monkeypatch):
    """Rows in (almost) every table for one tenant, through the real code paths."""
    from test_publishing import approved_draft, public_resolver
    from editor import publishing, proposals

    did, site_id = approved_draft(db, tenant)
    monkeypatch.setattr(socket, "getaddrinfo", public_resolver)
    target = publishing.add_target(db, tenant, site_id, "webhook", "h", {"url": "https://hooks.example/in"}, "X")
    publishing.request(db, tenant, did, target, "draft", "redazione")
    auth.create_token(db, tenant, "owner", "owner")
    with db.tenant(tenant) as s:
        s.execute(text("INSERT INTO editor.recipients (tenant_id, channel, address) VALUES (:t, 'email', 'a@b.it')"), {"t": tenant})
        s.execute(text("INSERT INTO editor.site_analyses (tenant_id, site_id, status, snapshot) VALUES (:t, :s, 'partial', '{}')"),
                  {"t": tenant, "s": site_id})
        s.execute(text("INSERT INTO editor.site_posts (tenant_id, site_id, url, url_hash, title, minhash) "
                       "VALUES (:t, :s, 'https://x/1', 'h', 'x', '{1}')"), {"t": tenant, "s": site_id})
        s.execute(text("INSERT INTO editor.profile_reviews (tenant_id, site_id, drift, significant) VALUES (:t, :s, '{}', false)"),
                  {"t": tenant, "s": site_id})
        proposals.record_feedback(s, tenant, "source", 1, "up", "x")
    briefing.build(db, tenant, __import__("datetime").date(2026, 10, 9))
    from editor import jobs
    with jobs.track(db, tenant, "analysis", "x"):
        pass


def test_two_tenants_are_fully_isolated(db, pg_urls, tenant, monkeypatch):
    populate(db, tenant, monkeypatch)
    other = f"{tenant}-b"
    with db.tenant(other) as s:
        repo.ensure_tenant(s, other, "B")
    tables = tenant_tables(pg_urls[0])
    assert len(tables) >= 25
    filled = []
    for table in tables:
        with db.tenant(tenant) as s:
            if s.execute(text(f"SELECT count(*) FROM editor.{table}")).scalar():
                filled.append(table)
        with db.tenant(other) as s:
            assert s.execute(text(f"SELECT count(*) FROM editor.{table} WHERE tenant_id = :a"), {"a": tenant}).scalar() == 0, table
            assert s.execute(text(f"UPDATE editor.{table} SET tenant_id = tenant_id WHERE tenant_id = :a"), {"a": tenant}).rowcount == 0 \
                if table != "tenants" else True
            if table not in ("tenants",):
                assert s.execute(text(f"DELETE FROM editor.{table} WHERE tenant_id = :a"), {"a": tenant}).rowcount == 0, table
    # the test is meaningful only if tenant A really has data almost everywhere
    assert len(filled) >= len(tables) - 1, set(tables) - set(filled)
    # B's token does not open A's data, and A's token does not open B's
    with db.tenant(tenant) as s:
        assert s.execute(text("SELECT count(*) FROM editor.access_tokens")).scalar() == 1
    tok_b = auth.create_token(db, other, "b", "owner")
    assert auth.resolve(db, tok_b).tenant_id == other


def test_inactive_tenant_loses_access(db, pg_urls, tenant):
    token = auth.create_token(db, tenant, "owner", "owner")
    assert auth.resolve(db, token).tenant_id == tenant
    set_plan(pg_urls[0], tenant, active=False)
    assert auth.resolve(db, token) is None
    set_plan(pg_urls[0], tenant, active=True)
    assert auth.resolve(db, token).tenant_id == tenant


def test_tenant_cannot_change_its_plan(db, tenant):
    with pytest.raises(Exception, match="permission denied"):
        with db.tenant(tenant) as s:
            s.execute(text("UPDATE editor.tenants SET max_sites = 1000"))
    with pytest.raises(Exception, match="permission denied"):
        with db.tenant(tenant) as s:
            s.execute(text("INSERT INTO editor.tenants (tenant_id, name, max_sites) VALUES (:t, 'x', 99)"), {"t": tenant + "x"})


def test_site_and_item_limits(db, pg_urls, tenant):
    set_plan(pg_urls[0], tenant, max_sites=1, max_items_per_day=3)
    with db.tenant(tenant) as s:
        repo.ensure_site(s, tenant, "a.example", "A")
        repo.ensure_site(s, tenant, "a.example", "A again")  # same site: fine
    with pytest.raises(QuotaExceeded, match="sites limit"):
        with db.tenant(tenant) as s:
            repo.ensure_site(s, tenant, "b.example", "B")
    with db.tenant(tenant) as s:
        sid = repo.upsert_source(s, tenant, kind="rss", name="x", url="https://x.example/f")
        rep = SourceReport(sid, "x")
        store_items(s, tenant, sid, [Item(url=f"https://x.example/{i}", title=f"Titolo numero {i} diverso {i * 7}") for i in range(5)], rep)
    assert (rep.new, rep.over_quota) == (3, 2)
    with db.tenant(tenant) as s:
        u = quotas.usage(s)
    assert u.used["items_today"] == 3 and u.remaining("items") == 0


def test_model_budget_stops_calls_before_they_reach_the_core(db, pg_urls, tenant):
    set_plan(pg_urls[0], tenant, max_llm_cost_month=0.01)
    core = CoreClient("http://core.test", "tok").for_tenant(db, tenant)
    with respx.mock() as router:
        llm = router.post("http://core.test/v1/llm/chat").mock(return_value=httpx.Response(
            200, json={"content": "{}", "tool_calls": [], "usage": None, "cost": 0.02}))
        res = core.chat([{"role": "user", "content": "x"}])
        with db.tenant(tenant) as s:
            repo.record_llm_usage(s, tenant, "test", res.model, res.usage, res.cost)
        with pytest.raises(CoreError, match="llm_cost limit"):
            core.chat([{"role": "user", "content": "x"}])
        assert llm.call_count == 1


def test_tenant_core_token(db, pg_urls, tenant, monkeypatch):
    set_plan(pg_urls[0], tenant, core_token_env="CORE_TOKEN_T1")
    monkeypatch.setenv("CORE_TOKEN_T1", "tenant-token")
    core = CoreClient("http://core.test", "shared").for_tenant(db, tenant)
    with respx.mock() as router:
        llm = router.post("http://core.test/v1/llm/chat").mock(return_value=httpx.Response(
            200, json={"content": "ok", "tool_calls": [], "usage": None, "cost": None}))
        core.chat([{"role": "user", "content": "x"}])
    assert llm.calls[0].request.headers["authorization"] == "Bearer tenant-token"


def test_active_source_limit_keeps_new_ones_as_candidates(db, pg_urls, tenant, monkeypatch):
    from conftest import fixture
    from editor import sources
    from test_proposals import setup_site

    site_id = setup_site(db, tenant)  # one active source already
    set_plan(pg_urls[0], tenant, max_active_sources=1)
    monkeypatch.setattr(sources, "api_candidates", lambda body: [sources.Candidate(
        "hackernews", "HN: fibra", "https://hn.algolia.com/api/v1/search", {"query": "fibra"}, "api_query")])
    monkeypatch.setattr(sources, "rate", lambda items, kws, **kw: {
        "version": "test", "components": {}, "score": 0.9, "status": "active", "sample": len(items)})
    with respx.mock() as router:
        router.get("https://hn.algolia.com/api/v1/search").mock(return_value=httpx.Response(200, content=fixture("hn.json")))
        with httpx.Client() as c:
            rep = sources.discover(db, tenant, site_id, c, None)
    assert (rep.active, rep.candidate) == (0, 1)
    assert any("limit reached" in n for n in rep.notes)
    with db.tenant(tenant) as s:
        st = s.execute(text("SELECT status FROM editor.sources WHERE kind = 'hackernews'")).scalar()
    assert st == "candidate"
