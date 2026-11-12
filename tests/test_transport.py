import asyncio
from urllib.error import HTTPError

import pytest


def test_transport_encodes_params_and_reads_text(monkeypatch):
    from app.sources import transport
    class Response:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size=-1): return b'{"ok": true}'
    def open_request(request, timeout):
        assert request.full_url == "https://example.test/data?timeZone=America%2FNew_York"
        assert timeout == 30
        assert "BaltimoreShowtimes" in request.get_header("User-agent")
        return Response()
    monkeypatch.setattr(transport, "urlopen", open_request)
    assert asyncio.run(transport.get_json("https://example.test/data", {"timeZone": "America/New_York"})) == {"ok": True}


def test_transport_does_not_expose_sensitive_request_urls(monkeypatch):
    from app.sources import transport
    def fail(*args, **kwargs):
        raise HTTPError("https://example.test/?api_key=secret", 403, "secret", {}, None)
    monkeypatch.setattr(transport, "urlopen", fail)
    with pytest.raises(RuntimeError) as error:
        asyncio.run(transport.get_text("https://example.test/?api_key=secret"))
    assert "403" in str(error.value)
    assert "secret" not in str(error.value)


def test_transport_blocks_cross_origin_redirect():
    from app.sources.transport import SameOriginRedirect
    from urllib.request import Request
    with pytest.raises(RuntimeError, match="Cross-origin"):
        SameOriginRedirect().redirect_request(Request("https://example.test/data"), None, 302, "Found", {}, "https://elsewhere.test/data")


def test_transport_response_limit(monkeypatch):
    from app.sources import transport
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size=-1): return b"x" * size
    monkeypatch.setattr(transport, "urlopen", lambda *args, **kwargs: Response())
    with pytest.raises(RuntimeError, match="size limit"):
        asyncio.run(transport.get_text("https://example.test/data"))


def test_transport_http_error_retains_safe_status(monkeypatch):
    from app.sources import transport
    def fail(*args, **kwargs):
        raise HTTPError("https://example.test/?api_key=secret", 429, "secret", {}, None)
    monkeypatch.setattr(transport, "urlopen", fail)
    with pytest.raises(RuntimeError) as error:
        asyncio.run(transport.get_text("https://example.test/?api_key=secret"))
    assert getattr(error.value, "status_code", None) == 429
