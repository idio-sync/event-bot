"""One refresh cycle: fetch theaters, match films, sync Plex/Radarr, send alerts."""

from __future__ import annotations

import asyncio
import logging

import httpx

from app import alerts, db, tmdb
from app.config import Settings
from app.integrations import plex, radarr
from app.sources import fetchers

log = logging.getLogger(__name__)

USER_AGENT = "BaltimoreShowtimes/1.0 (self-hosted personal showtimes page)"

_lock = asyncio.Lock()


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT})


async def run(settings: Settings, bot=None) -> None:
    """`bot` is the running Discord bot (app.bot.ShowtimesBot) when DISCORD_BOT_TOKEN is set."""
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
            sent = await alerts.send(settings, conn, _notifier(settings, bot, http))
            if sent:
                log.info("Sent %d Discord alerts", sent)
            db.kv_set(conn, "last_refresh", db.now_iso())
        finally:
            conn.close()


def _notifier(settings: Settings, bot, http: httpx.AsyncClient):
    if settings.discord_bot_token:
        # Bot mode. If it isn't connected yet, wait for the next refresh rather than
        # falling back to the webhook (which can't DM).
        return bot.notifier() if bot is not None and bot.is_ready() else None
    if settings.discord_webhook:
        return alerts.WebhookNotifier(http, settings.discord_webhook)
    return None


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
