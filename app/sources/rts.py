"""Ready Theatre Systems (formovietickets.com) schedule JSON — The Charles, The Senator."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.models import Showing

SCHEDULE_URL = "https://app.formovietickets.com/schedules/scheduleV1/L{rtn}.json"

_TMDB_RE = re.compile(r"themoviedb\.org/movie/(\d+)")
_IMDB_RE = re.compile(r"imdb\.com/title/(tt\d+)")


def _date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def parse(data: dict[str, Any], theater: str, rtn: str, tz: ZoneInfo) -> list[Showing]:
    location = data.get("location") or {}
    ticket_url = f"https://{rtn}.formovietickets.com/"
    out: list[Showing] = []
    for title in location.get("Titles") or []:
        name = (title.get("title") or "").strip()
        if not name:
            continue
        site = title.get("webSite") or ""
        tmdb = _TMDB_RE.search(site)
        imdb = _IMDB_RE.search(site)
        release = _date(title.get("ReleaseUS"))
        secs = title.get("lengthsecs") or 0
        for show in title.get("Shows") or []:
            raw_time = show.get("time")
            if not raw_time:
                continue
            start = datetime.fromisoformat(raw_time).replace(tzinfo=tz)
            out.append(
                Showing(
                    theater=theater,
                    source_id=str(show.get("Id")),
                    title=name,
                    start=start,
                    tmdb_id=int(tmdb.group(1)) if tmdb else None,
                    imdb_id=imdb.group(1) if imdb else None,
                    release_date=release,
                    ticket_url=ticket_url,
                    runtime_min=round(secs / 60) if secs else None,
                    synopsis=title.get("synopsis") or None,
                    sold_out=bool(show.get("so")),
                )
            )
    return out


async def fetch(client: httpx.AsyncClient, theater: str, rtn: str, tz: ZoneInfo) -> list[Showing]:
    resp = await client.get(SCHEDULE_URL.format(rtn=rtn))
    resp.raise_for_status()
    return parse(resp.json(), theater, rtn, tz)
