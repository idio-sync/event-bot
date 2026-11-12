"""Plex: server library (owned / watched) and the plex.tv watchlist, both keyed by TMDB id."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

DISCOVER = "https://discover.provider.plex.tv"
PAGE = 100
_TMDB_GUID = re.compile(r"^tmdb://(\d+)$")


def _headers(token: str) -> dict[str, str]:
    return {
        "X-Plex-Token": token,
        "Accept": "application/json",
        "X-Plex-Client-Identifier": "baltimore-showtimes",
        "X-Plex-Product": "Baltimore Showtimes",
    }


def tmdb_from_guids(item: dict[str, Any]) -> int | None:
    for guid in item.get("Guid") or []:
        m = _TMDB_GUID.match(str(guid.get("id", "")))
        if m:
            return int(m.group(1))
    return None


async def library(client: httpx.AsyncClient, url: str, token: str) -> dict[str, Any]:
    """{'machine': id, 'movies': {tmdb_id: {'watched': bool, 'rating_key': str}}}"""
    h = _headers(token)
    ident = (await client.get(f"{url}/identity", headers=h)).json()
    machine = (ident.get("MediaContainer") or {}).get("machineIdentifier")
    resp = await client.get(f"{url}/library/sections", headers=h)
    resp.raise_for_status()
    sections = [d for d in (resp.json().get("MediaContainer") or {}).get("Directory") or [] if d.get("type") == "movie"]
    movies: dict[int, dict[str, Any]] = {}
    for section in sections:
        r = await client.get(f"{url}/library/sections/{section['key']}/all", params={"includeGuids": 1}, headers=h)
        r.raise_for_status()
        for item in (r.json().get("MediaContainer") or {}).get("Metadata") or []:
            tmdb_id = tmdb_from_guids(item)
            if tmdb_id:
                prev = movies.get(tmdb_id, {})
                movies[tmdb_id] = {
                    "watched": prev.get("watched", False) or bool(item.get("viewCount")),
                    "rating_key": prev.get("rating_key") or str(item.get("ratingKey")),
                }
    return {"machine": machine, "movies": {str(k): v for k, v in movies.items()}}


async def watchlist(client: httpx.AsyncClient, token: str, guid_cache: dict[str, int]) -> dict[str, Any]:
    """Movies on the account's watchlist: {tmdb_id: {'title', 'year'}}.

    The watchlist listing does not always carry external ids, so unknown items are
    looked up one by one; `guid_cache` (ratingKey -> tmdb id) is updated in place.
    """
    h = _headers(token)
    items: list[dict[str, Any]] = []
    start = 0
    while True:
        resp = await client.get(
            f"{DISCOVER}/library/sections/watchlist/all",
            params={"includeGuids": 1, "X-Plex-Container-Start": start, "X-Plex-Container-Size": PAGE},
            headers=h,
        )
        resp.raise_for_status()
        container = resp.json().get("MediaContainer") or {}
        page = container.get("Metadata") or []
        items.extend(page)
        start += len(page)
        if not page or start >= int(container.get("totalSize") or 0):
            break
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        if item.get("type") != "movie":
            continue
        rating_key = str(item.get("ratingKey"))
        tmdb_id = tmdb_from_guids(item) or guid_cache.get(rating_key)
        if tmdb_id is None:
            try:
                r = await client.get(f"{DISCOVER}/library/metadata/{rating_key}", headers=h)
                r.raise_for_status()
                meta = ((r.json().get("MediaContainer") or {}).get("Metadata") or [{}])[0]
                tmdb_id = tmdb_from_guids(meta)
            except httpx.HTTPError as exc:
                log.warning("Plex metadata lookup failed for %s: %s", item.get("title"), exc)
        if tmdb_id:
            guid_cache[rating_key] = tmdb_id
            out[str(tmdb_id)] = {"title": item.get("title"), "year": item.get("year")}
    return out
