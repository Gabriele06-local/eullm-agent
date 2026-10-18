"""HTTP through the Core. Every request Editor Mode makes to a third-party
site goes through `POST /v1/fetch`, so the Core's address checks (no private
networks, every redirect re-checked), size limit, per-host pace and audit
apply to crawling and collection alike.

`CoreTransport` plugs into httpx: collectors and the crawler use a normal
`httpx.Client` and never know the difference. Only GET is possible, and only
the headers the Core accepts are forwarded.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

from .core_client import TENANT_HEADER

FORWARDED = ("accept", "accept-language", "if-none-match", "if-modified-since")


# The Editor Mode tenant the current work is for; with a delegating Core
# token every fetch made meanwhile is counted and limited as that tenant's.
_acting_for: ContextVar[str | None] = ContextVar("editor_core_tenant", default=None)


@contextmanager
def acting_for(tenant_id: str) -> Iterator[None]:
    token = _acting_for.set(tenant_id)
    try:
        yield
    finally:
        _acting_for.reset(token)


class FetchRefused(httpx.TransportError):
    """The Core refused the address (private network, scheme, header)."""


class CoreTransport(httpx.BaseTransport):
    def __init__(self, base_url: str, token: str, client: httpx.Client | None = None, delegate: bool = False):
        self.endpoint = base_url.rstrip("/") + "/v1/fetch"
        self.token = token
        self.http = client or httpx.Client(timeout=60)
        self.delegate = delegate

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            raise FetchRefused(f"only GET goes through the Core, not {request.method}", request=request)
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARDED}
        auth = {"Authorization": f"Bearer {self.token}"}
        tenant = _acting_for.get()
        if self.delegate and tenant:
            auth[TENANT_HEADER] = tenant
        try:
            r = self.http.post(
                self.endpoint,
                headers=auth,
                json={"url": str(request.url), "headers": headers},
            )
        except httpx.HTTPError as e:
            raise httpx.ConnectError(f"Core unreachable: {e}", request=request) from e
        if r.status_code == 403:
            raise FetchRefused(_error(r), request=request)
        if r.status_code != 200:
            raise httpx.ConnectError(f"Core fetch failed ({r.status_code}): {_error(r)}", request=request)
        body = r.json()
        out_headers = {
            k: body[f]
            for k, f in (("content-type", "content_type"), ("etag", "etag"), ("last-modified", "last_modified"))
            if body.get(f)
        }
        if body.get("truncated"):
            out_headers["x-truncated"] = "1"
        out_headers["x-final-url"] = body.get("url", str(request.url))
        return httpx.Response(
            body["status"],
            headers=out_headers,
            content=base64.b64decode(body.get("body_base64", "")),
            request=request,
        )


def _error(r: httpx.Response) -> str:
    try:
        return r.json().get("error", r.text)
    except ValueError:
        return r.text


def core_http_client(base_url: str, token: str, delegate: bool = False, **kwargs) -> httpx.Client:
    """An httpx client whose every request goes through the Core; with a
    delegating token, as the tenant of `acting_for`."""
    # Redirects are followed by the Core, which checks every hop.
    return httpx.Client(transport=CoreTransport(base_url, token, delegate=delegate), follow_redirects=False,
                        timeout=90, **kwargs)
