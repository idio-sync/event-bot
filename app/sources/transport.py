"""Small async boundary for public pages requiring urllib's HTTP transport.

No browser impersonation or challenge solving. Requests run outside the event loop.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

USER_AGENT = "BaltimoreShowtimes/1.0 (self-hosted personal showtimes page)"
TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 10 * 1024 * 1024


class SourceHTTPError(RuntimeError):
    """An HTTP status without a potentially sensitive request URL."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"Source HTTP {status_code}")


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            raise RuntimeError("Cross-origin source redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


urlopen = build_opener(SameOriginRedirect()).open


def _get_text(url: str, params: dict[str, str] | None) -> str:
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise RuntimeError("Source response exceeds size limit")
            return body.decode("utf-8")
    except HTTPError as exc:
        code = exc.code
        exc.close()
        raise SourceHTTPError(code) from None
    except (URLError, TimeoutError, OSError, UnicodeError):
        raise RuntimeError("Source request failed or timed out") from None


async def get_text(url: str, params: dict[str, str] | None = None) -> str:
    return await asyncio.to_thread(_get_text, url, params)


async def get_json(url: str, params: dict[str, str] | None = None) -> Any:
    try:
        return json.loads(await get_text(url, params))
    except json.JSONDecodeError:
        raise ValueError("Source returned invalid JSON") from None
