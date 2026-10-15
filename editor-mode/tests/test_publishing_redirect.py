"""Redirect guard for publishing (no database needed).

The publisher uses httpx with follow_redirects=False, like the Core which
re-checks every hop. A 3xx response must therefore fail closed instead of
being (mis)treated as success.
"""

import socket

import httpx
import pytest

from editor import publishing as pub


def public_resolver(host, port, proto=None):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def private_resolver(host, port, proto=None):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]


def test_check_public_url_rejects_private_and_http():
    with pytest.raises(pub.PublishError, match="non-public"):
        pub.check_public_url("https://intranet.example", private_resolver)
    pub.check_public_url("https://blog.example", public_resolver)
    with pytest.raises(pub.PublishError, match="https"):
        pub.check_public_url("http://blog.example", public_resolver)


def test_no_redirect_passes_through():
    r = httpx.Response(201, json={"id": 1})
    pub._ensure_no_redirect(r, "https://blog.example", public_resolver)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_to_public_is_refused(status):
    r = httpx.Response(status, headers={"location": "https://cdn.example/x"})
    with pytest.raises(pub.PublishError, match="not followed"):
        pub._ensure_no_redirect(r, "https://blog.example", public_resolver)


def test_redirect_to_private_is_reported():
    r = httpx.Response(302, headers={"location": "https://internal.example/x"})
    with pytest.raises(pub.PublishError, match="non-public"):
        pub._ensure_no_redirect(r, "https://blog.example", private_resolver)


def test_relative_redirect_is_refused():
    r = httpx.Response(302, headers={"location": "/login"})
    with pytest.raises(pub.PublishError, match="not followed"):
        pub._ensure_no_redirect(r, "https://blog.example", public_resolver)


def test_connection_goes_to_the_checked_address(monkeypatch):
    """DNS rebinding: the socket is opened to the address that was checked,
    and a name resolving to a private address is refused at connect time."""
    import httpcore

    opened = []
    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp",
                        lambda self, host, port, *a, **k: opened.append((host, port)))
    public = lambda host, port, **kw: [(2, 1, 6, "", ("93.184.216.34", port))]  # noqa: E731
    private = lambda host, port, **kw: [(2, 1, 6, "", ("169.254.169.254", port))]  # noqa: E731
    pub._PinnedBackend(public).connect_tcp("cms.example", 443)
    assert opened == [("93.184.216.34", 443)]
    with pytest.raises(pub.PublishError, match="non-public"):
        pub._PinnedBackend(private).connect_tcp("cms.example", 443)
    assert len(opened) == 1


def test_publisher_client_is_pinned_and_ignores_proxies():
    client = pub.pinned_client()
    assert isinstance(client._transport, pub._PinnedTransport)
    assert not client._mounts
