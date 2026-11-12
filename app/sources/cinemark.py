"""Cinemark's own site JSON (papi) — Cinemark Towson and XD."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.models import Showing

BASE = "https://www.cinemark.com"
MAX_DAYS_AHEAD = 120
REQUEST_GAP_SECONDS = 0.4


def _date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def parse_day(movies: list[dict[str, Any]], theater: str) -> list[Showing]:
    out: list[Showing] = []
    for movie in movies:
        title = (movie.get("movieTitle") or "").strip()
        if not title:
            continue
        movie_url = movie.get("movieUrl")
        for print_type in movie.get("showtimesByPrintType") or []:
            tags = [print_type.get("printTypeName") or ""]
            for attr in (print_type.get("showtimeAttributes") or []) + (print_type.get("auditoriumAttributes") or []):
                if attr.get("displayName"):
                    tags.append(attr["displayName"])
            tags = [t for t in dict.fromkeys(tags) if t]
            for show in print_type.get("showtimes") or []:
                if show.get("isPrivateWatchParty"):
                    continue
                start = datetime.fromisoformat(show["startTime"].replace("Z", "+00:00"))
                out.append(
                    Showing(
                        theater=theater,
                        source_id=str(show.get("id")),
                        title=title,
                        start=start,
                        release_date=_date(movie.get("movieReleaseDate")),
                        tags=tags,
                        ticket_url=f"{BASE}{movie_url}" if movie_url else None,
                        runtime_min=movie.get("movieRuntimeMinutes") or None,
                        synopsis=movie.get("movieSynopsis") or None,
                        poster_url=movie.get("moviePosterUrl") or None,
                        sold_out=show.get("seatsAvailable") == 0,
                    )
                )
    return out


async def fetch(client: httpx.AsyncClient, theater: str, theater_id: int, tz: ZoneInfo) -> list[Showing]:
    params = {"timeZone": str(tz)}
    resp = await client.get(f"{BASE}/papi/theaters/{theater_id}/showtime-dates", params=params)
    resp.raise_for_status()
    horizon = datetime.now(tz).date() + timedelta(days=MAX_DAYS_AHEAD)
    days = [d for d in resp.json() if _date(d) and _date(d) <= horizon]
    out: list[Showing] = []
    for day in days:
        r = await client.get(f"{BASE}/papi/theaters/{theater_id}/showtimes", params={"date": day, **params})
        r.raise_for_status()
        out.extend(parse_day(r.json(), theater))
        await asyncio.sleep(REQUEST_GAP_SECONDS)
    return out
