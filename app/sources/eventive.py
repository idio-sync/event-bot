"""Eventive events API — SNF Parkway.

Uses an explicit key override or discovers the browser key published on the
Parkway website for each fetch. Key values are kept out of URLs and errors.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any

import httpx

from app.models import FetchResult, Showing

PARKWAY_HOME = "https://snfparkway.org/"
API = "https://api.eventive.org/event_buckets/{bucket}/events"
PARKWAY_BUCKET = "67fb1b7ba2fc1ffee7cc9d0c"
SCHEDULE_URL = "https://parkway.eventive.org/schedule/{event_id}"

# Tags that mean "this is a screening"; events with none of these and no
# attached film (live arts, summits, resource fairs) are skipped.
FILM_TAGS = {"film", "feature film", "short film", "documentary", "open captions"}


class _InlineScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_script = False
        self.scripts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = not dict(attrs).get("src")

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.scripts.append(data)


def discover_key(html: str) -> str:
    """Read only literal apiKey properties in inline scripts; never evaluate JS."""
    page = _InlineScripts()
    page.feed(html)
    pattern = r"(?<![\w$])apiKey\s*:\s*(['\"])([A-Za-z0-9_-]{8,256})\1\s*[,}]"
    keys = {match.group(2) for script in page.scripts for match in re.finditer(pattern, script)}
    if len(keys) != 1:
        raise ValueError("Parkway browser-key discovery failed: expected one published key; set PARKWAY_EVENTIVE_KEY")
    return keys.pop()


def parse(data: Any, theater: str) -> list[Showing]:
    events = data.get("events") if isinstance(data, dict) else data
    if not isinstance(events, list):
        raise ValueError("Eventive returned an unexpected events envelope")
    out: list[Showing] = []
    for ev in events or []:
        name = (ev.get("name") or "").strip()
        start_raw = ev.get("start_time")
        if not name or not start_raw or ev.get("is_virtual"):
            continue
        tags = [t.get("name", "").strip() for t in ev.get("tags") or [] if t.get("name")]
        films = ev.get("films") or []
        if not films and not any(t.lower() in FILM_TAGS for t in tags):
            continue
        film = films[0] if len(films) == 1 else None
        year = None
        if film:
            raw_year = str((film.get("details") or {}).get("year") or "")
            year = int(raw_year) if raw_year.isdigit() else None
        if ev.get("id") is None or not str(ev["id"]).strip():
            raise ValueError("Eventive screening has no ID")
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        if start.tzinfo is None:
            raise ValueError("Eventive screening has no timezone")
        out.append(
            Showing(
                theater=theater,
                source_id=str(ev.get("id")),
                title=name,
                match_title=((film.get("name") or "").strip() or None) if film else None,
                start=start,
                release_date=date(year, 1, 1) if year else None,
                tags=tags,
                is_event=True,
                ticket_url=SCHEDULE_URL.format(event_id=ev.get("id")),
                synopsis=ev.get("short_description") or None,
                sold_out=ev.get("tickets_available") is False,
            )
        )
    return out


async def fetch(
    client: httpx.AsyncClient, theater: str, api_key: str = "", bucket: str = PARKWAY_BUCKET,
) -> FetchResult:
    if not api_key:
        page = await client.get(PARKWAY_HOME)
        page.raise_for_status()
        api_key = discover_key(page.text)
    resp = await client.get(
        API.format(bucket=bucket), params={"upcoming_only": "true"},
        auth=httpx.BasicAuth(api_key, ""),
        headers={"Accept-Version": "~1", "Content-Type": "application/json"},
    )
    resp.raise_for_status()
    # Eventive's public resource guide does not define pagination guarantees.
    # Treat this endpoint as partial until authenticated coverage is validated.
    return FetchResult(parse(resp.json(), theater), complete=False,
                       warning="Partial schedule: Eventive completeness unverified; missing listings are retained")
