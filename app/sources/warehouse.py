"""Warehouse Rotunda's public JSON-LD screenings.

The site publishes a limited selection per page. These snapshots are always
partial: missing shows must never be interpreted as cancellations.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.models import FetchResult, Showing
from app.sources import transport

BASE = "https://rotunda.warehousecinemas.com"
REQUEST_GAP_SECONDS = 0.25
MAX_MOVIE_PAGES = 40


class _Page(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.scripts: list[str] = []
        self.links: list[str] = []
        self._script: list[str] | None = None
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and (values.get("type") or "").lower() == "application/ld+json":
            self._script = []
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            self.scripts.append("".join(self._script))
            self._script = None


def _objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def _kind(obj: dict, kind: str) -> bool:
    value = obj.get("@type")
    return kind in value if isinstance(value, list) else value == kind


def _official(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Warehouse screening lacks a booking URL")
    url = urlsplit(urljoin(BASE + "/", raw))
    if url.scheme != "https" or url.netloc != urlsplit(BASE).netloc:
        raise ValueError("Warehouse URL has an unexpected origin")
    return urlunsplit((url.scheme, url.netloc, url.path, "", ""))


def movie_urls(html: str) -> list[str]:
    urls: set[str] = set()
    for raw in _Page(html).links:
        try:
            url = _official(raw)
        except ValueError:
            continue
        if re.fullmatch(r"/movie/[^/]+/?", urlsplit(url).path):
            urls.add(url.rstrip("/") + "/")
    return sorted(urls)


def _runtime(raw: Any) -> int | None:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", str(raw or ""))
    return (int(match[1] or 0) * 60 + int(match[2] or 0)) or None if match else None


def parse(html: str, theater: str) -> list[Showing]:
    objects: list[dict] = []
    for script in _Page(html).scripts:
        try:
            objects.extend(_objects(json.loads(script)))
        except (ValueError, TypeError) as exc:
            raise ValueError("Warehouse returned invalid structured data") from exc
    movies = {o["@id"]: o for o in objects if _kind(o, "Movie") and o.get("@id")}
    events = [o for o in objects if _kind(o, "ScreeningEvent")]
    if not movies and not events:
        raise ValueError("Warehouse page contains no movie or screening structured data")
    out: dict[str, Showing] = {}
    for event in events:
        ref = event.get("workPresented") or {}
        movie = movies.get(ref.get("@id"), ref) if isinstance(ref, dict) else {}
        title = str(movie.get("name") or event.get("name") or "").strip()
        if not title:
            raise ValueError("Warehouse screening has no title")
        ticket = _official(event.get("url") or event.get("@id"))
        match = re.fullmatch(r"/checkout/showing/[^/]+/([^/]+)/?", urlsplit(ticket).path)
        if not match:
            raise ValueError("Warehouse screening lacks a stable booking ID")
        try:
            start = datetime.fromisoformat(str(event["startDate"]).replace("Z", "+00:00"))
        except (KeyError, ValueError) as exc:
            raise ValueError("Warehouse screening has an invalid start time") from exc
        if start.utcoffset() is None:
            raise ValueError("Warehouse screening start time lacks a timezone")
        image = movie.get("image")
        if isinstance(image, dict):
            image = image.get("url")
        if not isinstance(image, str) or urlsplit(image).scheme not in {"http", "https"}:
            image = None
        offers = event.get("offers") or {}
        offers = offers if isinstance(offers, list) else [offers]
        availability = [str(o.get("availability", "")) for o in offers if isinstance(o, dict)]
        show = Showing(
            theater=theater,
            source_id=match[1],
            title=title,
            start=start,
            ticket_url=ticket,
            runtime_min=_runtime(movie.get("duration")),
            synopsis=movie.get("description") or None,
            poster_url=image,
            sold_out=bool(availability) and all(a.endswith("/SoldOut") for a in availability),
        )
        out[show.key] = show
    return list(out.values())


async def fetch(client: httpx.AsyncClient, theater: str = "warehouse-rotunda") -> FetchResult:
    html = await transport.get_text(BASE + "/home/")
    shows = {s.key: s for s in parse(html, theater)}
    urls = movie_urls(html)
    failures = 0
    for url in urls[:MAX_MOVIE_PAGES]:
        await asyncio.sleep(REQUEST_GAP_SECONDS)
        try:
            for show in parse(await transport.get_text(url), theater):
                shows[show.key] = show
        except Exception:
            # Public pages can disappear or be blocked individually. Never pass
            # response/error text to status pages, and never declare completeness.
            failures += 1
    warning = "Partial schedule: public pages do not guarantee complete future coverage."
    if len(urls) > MAX_MOVIE_PAGES:
        warning += f" Movie discovery limited to {MAX_MOVIE_PAGES} pages."
    if failures:
        warning += f" {failures} movie page(s) unavailable."
    return FetchResult(list(shows.values()), complete=False, warning=warning)
