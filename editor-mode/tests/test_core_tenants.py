"""With a delegating Core token, every Editor Mode tenant is a Core tenant of
its own: model calls and fetches carry X-EuLLM-Tenant, and the plan's model
budget becomes the tenant's limit in the Core."""

import httpx
import pytest
import respx

from editor import queue
from editor.core_client import TENANT_HEADER, CoreClient, CoreError
from editor.core_fetch import acting_for, core_http_client


def answer():
    return httpx.Response(200, json={"content": "ok", "usage": None, "cost": None})


def test_model_calls_name_the_tenant_only_with_a_delegating_token(db, tenant):
    with respx.mock() as router:
        call = router.post("http://core.test/v1/llm/chat").mock(return_value=answer())
        CoreClient("http://core.test", "tok", delegate=True).for_tenant(db, tenant).chat([])
        assert call.calls.last.request.headers[TENANT_HEADER] == tenant
        CoreClient("http://core.test", "tok").for_tenant(db, tenant).chat([])
        assert TENANT_HEADER not in call.calls.last.request.headers


def test_a_tenant_with_its_own_token_does_not_act_through_the_shared_one(db, tenant, monkeypatch):
    from conftest import set_plan

    set_plan(db, tenant, core_token_env="CORE_TOKEN_OWN")
    monkeypatch.setenv("CORE_TOKEN_OWN", "own-token")
    with respx.mock() as router:
        call = router.post("http://core.test/v1/llm/chat").mock(return_value=answer())
        CoreClient("http://core.test", "tok", delegate=True).for_tenant(db, tenant).chat([])
        req = call.calls.last.request
        assert req.headers["authorization"] == "Bearer own-token" and TENANT_HEADER not in req.headers


def fetched():
    return httpx.Response(200, json={"status": 200, "url": "https://site.example/", "body_base64": ""})


def test_fetches_are_made_as_the_tenant_of_the_work():
    with respx.mock() as router:
        fetch = router.post("http://core.test/v1/fetch").mock(return_value=fetched())
        with core_http_client("http://core.test", "tok", delegate=True) as client:
            with acting_for("acme"):
                client.get("https://site.example/")
            assert fetch.calls.last.request.headers[TENANT_HEADER] == "acme"
            client.get("https://site.example/")  # outside any tenant's work
            assert TENANT_HEADER not in fetch.calls.last.request.headers
            with acting_for("_archive"):  # the Core refuses names starting with a symbol
                client.get("https://site.example/")
            assert fetch.calls.last.request.headers[TENANT_HEADER] == "shared.archive"
        with core_http_client("http://core.test", "tok") as client, acting_for("acme"):
            client.get("https://site.example/")
            assert TENANT_HEADER not in fetch.calls.last.request.headers


def test_queued_work_runs_as_its_tenant(db, tenant):
    seen = []
    with respx.mock() as router:
        fetch = router.post("http://core.test/v1/fetch").mock(return_value=fetched())

        def analysis(t, subject, args):
            with core_http_client("http://core.test", "tok", delegate=True) as client:
                client.get("https://site.example/")
            seen.append(fetch.calls.last.request.headers.get(TENANT_HEADER))

        queue.enqueue(db, tenant, "analysis", "site.example")
        queue.drain(db, {"analysis": analysis}, tenant)
    assert seen == [tenant]


def test_the_plan_budget_becomes_the_core_limit():
    core = CoreClient("http://core.test", "tok", delegate=True)
    with respx.mock() as router:
        put = router.put("http://core.test/v1/tenants/acme/limits").mock(return_value=httpx.Response(
            200, json={"tenant": "editor/acme", "limits": {"max_cost_per_month": 12.5},
                       "effective": {"max_cost_per_month": 12.5}}))
        assert core.set_limits("acme", {"max_cost_per_month": 12.5})["tenant"] == "editor/acme"
        assert put.calls.last.request.read() == b'{"max_cost_per_month":12.5}'
        router.put("http://core.test/v1/tenants/beta/limits").mock(
            return_value=httpx.Response(403, json={"error": "only a delegating token sets the limits"}))
        with pytest.raises(CoreError, match="403: only a delegating token"):
            core.set_limits("beta", {})
