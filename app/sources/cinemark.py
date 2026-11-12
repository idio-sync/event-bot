"""Cinemark's own site JSON (papi) — Cinemark Towson and XD."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo
from urllib.parse import urljoin

import httpx

from app.models import FetchResult, Showing
from app.sources.transport import get_json

BASE = "https://www.cinemark.com"
MAX_DAYS_AHEAD = 365
REQUEST_GAP_SECONDS = 0.4


def _date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def parse_day(movies: list[dict[str, Any]], theater: str) -> list[Showing]:
    if not isinstance(movies, list):
        raise ValueError("Cinemark returned an unexpected movie envelope")
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
                if show.get("id") is None or not str(show["id"]).strip():
                    raise ValueError("Cinemark screening has no ID")
                start = datetime.fromisoformat(show["startTime"].replace("Z", "+00:00"))
                if start.tzinfo is None:
                    raise ValueError("Cinemark screening has no timezone")
                out.append(
                    Showing(
                        theater=theater,
                        source_id=str(show.get("id")),
                        title=title,
                        start=start,
                        release_date=_date(movie.get("movieReleaseDate")),
                        tags=tags,
                        ticket_url=urljoin(BASE, movie_url) if movie_url else None,
                        runtime_min=movie.get("movieRuntimeMinutes") or None,
                        synopsis=movie.get("movieSynopsis") or None,
                        poster_url=urljoin(BASE, movie["moviePosterUrl"]) if movie.get("moviePosterUrl") else None,
                        sold_out=show.get("seatsAvailable") == 0,
                    )
                )
    return out


async def fetch(
    client: httpx.AsyncClient, theater: str, theater_id: int, tz: ZoneInfo,
    days_ahead: int = MAX_DAYS_AHEAD,
) -> FetchResult:
    params = {"timeZone": str(tz)}
    advertised = await get_json(f"{BASE}/papi/theaters/{theater_id}/showtime-dates", params=params)
    if not isinstance(advertised, list) or any(not isinstance(d, str) or _date(d) is None for d in advertised):
        raise ValueError("Cinemark returned an unexpected date list")
    today = datetime.now(tz).date()
    horizon = today + timedelta(days=days_ahead)
    days = sorted({d for d in advertised if today <= _date(d) <= horizon})
    out: list[Showing] = []
    for day in days:
        data = await get_json(f"{BASE}/papi/theaters/{theater_id}/showtimes", params={"date": day, **params})
        out.extend(parse_day(data, theater))
        await asyncio.sleep(REQUEST_GAP_SECONDS)
    return FetchResult(out, complete=False, warning=f"Partial schedule: Cinemark requested horizon through {horizon}; missing listings are retained")
