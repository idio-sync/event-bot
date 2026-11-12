"""One refresh cycle: fetch theaters, match films, sync Plex/Radarr, send alerts."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import httpx

from app import db, tmdb
from app.config import Settings
from app.integrations import discord, plex, radarr
from app.sources import THEATERS, fetchers
from app.timefmt import stamp

log = logging.getLogger(__name__)

USER_AGENT = "BaltimoreShowtimes/1.0 (self-hosted personal showtimes page)"
ALERT_HORIZON_DAYS = 90

_lock = asyncio.Lock()


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT})


async def run(settings: Settings) -> None:
    if _lock.locked():
        log.info("Refresh already running")
        return
    async with _lock, client() as http:
        conn = db.connect(settings.db_path)
        try:
            await _theaters(settings, conn, http)
            matched = await tmdb.resolve(conn, http, settings.tmdb_key)
            log.info("Matched %d new titles to TMDB", matched)
            await _integrations(settings, conn, http)
            await _alerts(settings, conn, http)
            db.kv_set(conn, "last_refresh", db.now_iso())
        finally:
            conn.close()


async def _theaters(settings: Settings, conn, http: httpx.AsyncClient) -> None:
    for theater, fetch in fetchers(settings).items():
        if fetch is None:
            db.record_source(conn, theater, False, error="Not configured")
            continue
        try:
            showings = await fetch(http)
        except Exception as exc:  # keep last good data, report on /status
            log.exception("Fetching %s failed", theater)
            db.record_source(conn, theater, False, error=f"{type(exc).__name__}: {exc}"[:500])
            continue
        if not showings:
            # An empty schedule is far more likely a broken source than a closed theater.
            db.record_source(conn, theater, False, error="Source returned no showings")
            continue
        new = db.save_showings(conn, theater, showings)
        db.record_source(conn, theater, True, count=len(showings))
        log.info("%s: %d showings, %d new", theater, len(showings), new)


async def _integrations(settings: Settings, conn, http: httpx.AsyncClient) -> None:
    async def step(name: str, coro_factory) -> None:
        try:
            db.kv_set(conn, name, await coro_factory())
            db.kv_set(conn, f"status:{name}", {"ok": True, "at": db.now_iso()})
        except Exception as exc:
            log.exception("%s sync failed", name)
            db.kv_set(conn, f"status:{name}", {"ok": False, "at": db.now_iso(), "error": f"{type(exc).__name__}: {exc}"[:500]})

    if settings.plex_enabled:
        await step("plex_library", lambda: plex.library(http, settings.plex_url, settings.plex_token))
    if settings.plex_watchlist_enabled:
        cache = {k: int(v) for k, v in db.kv_get(conn, "plex_guid_cache", {}).items()}

        async def wl():
            result = await plex.watchlist(http, settings.plex_token, cache)
            db.kv_set(conn, "plex_guid_cache", cache)
            return result

        await step("plex_watchlist", wl)
    if settings.radarr_enabled:
        await step("radarr", lambda: radarr.movies(http, settings.radarr_url, settings.radarr_api_key))


async def _alerts(settings: Settings, conn, http: httpx.AsyncClient) -> None:
    if not settings.discord_webhook:
        return
    watch = db.kv_get(conn, "plex_watchlist", {})
    if not watch:
        return
    now = datetime.now(UTC)
    rows = db.upcoming(
        conn, now.isoformat(timespec="seconds"), (now + timedelta(days=ALERT_HORIZON_DAYS)).isoformat(timespec="seconds"),
        list(THEATERS),
    )
    done = db.alerted(conn)
    groups: dict[tuple[int, str], list] = defaultdict(list)
    for r in rows:
        if r["tmdb_id"] and str(r["tmdb_id"]) in watch and (r["tmdb_id"], r["theater"]) not in done:
            groups[(r["tmdb_id"], r["theater"])].append(r)
    if not groups:
        return
    embeds = []
    for (tmdb_id, theater), shows in groups.items():
        first = shows[0]
        times = [stamp(datetime.fromisoformat(s["start_utc"]).astimezone(settings.tz)) for s in shows[:8]]
        more = f"\n…and {len(shows) - 8} more" if len(shows) > 8 else ""
        th = THEATERS.get(theater)
        embeds.append(
            discord.embed(
                f"{first['film_title'] or first['raw_title']} — {th.name if th else theater}",
                first["ticket_url"] or (th.url if th else None),
                "\n".join(times) + more,
                first["film_poster"] or first["poster_url"],
            )
        )
    n = len(embeds)
    content = f"🎬 {n} showing{'s' if n != 1 else ''} of films on your Plex watchlist"
    if settings.public_url:
        content += f" — {settings.public_url}/?kinds=watchlist&view=film"
    try:
        await discord.send(http, settings.discord_webhook, content, embeds)
    except httpx.HTTPError:
        log.exception("Discord alert failed; will retry next refresh")
        return
    db.mark_alerted(conn, groups.keys())
