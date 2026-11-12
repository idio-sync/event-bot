"""Eventive events API — SNF Parkway.

Only runs when PARKWAY_EVENTIVE_KEY is set; see docs/design.md "Open decisions".
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import httpx

from app.models import Showing

API = "https://api.eventive.org/event_buckets/{bucket}/events"
PARKWAY_BUCKET = "67fb1b7ba2fc1ffee7cc9d0c"
SCHEDULE_URL = "https://parkway.eventive.org/schedule/{event_id}"

# Tags that mean "this is a screening"; events with none of these and no
# attached film (live arts, summits, resource fairs) are skipped.
FILM_TAGS = {"film", "feature film", "short film", "documentary", "open captions"}


def parse(data: Any, theater: str) -> list[Showing]:
    events = data.get("events", []) if isinstance(data, dict) else data
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
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
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


async def fetch(client: httpx.AsyncClient, theater: str, api_key: str) -> list[Showing]:
    resp = await client.get(
        API.format(bucket=PARKWAY_BUCKET), params={"api_key": api_key, "upcoming_only": "true"}
    )
    resp.raise_for_status()
    return parse(resp.json(), theater)
