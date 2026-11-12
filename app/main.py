"""FastAPI app: page, feeds, Radarr actions, background refresh loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections import OrderedDict
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import bot, config, db, listing, prefs as prefs_mod, refresh, timefmt
from app.bot import ShowtimesBot
from app.integrations import radarr
from app.sources import THEATERS, fetchers

log = logging.getLogger(__name__)
HERE = Path(__file__).parent

LABELS = {
    "rerelease": "Re-release",
    "event": "Special event",
    "advance": "Advance",
    "new": "New",
    "watchlist": "Watchlist",
}


def create_app(settings: config.Settings | None = None, run_loop: bool = True) -> FastAPI:
    settings = settings or config.load()
    os.makedirs(settings.data_dir, exist_ok=True)
    with contextlib.closing(db.connect(settings.db_path)) as conn:
        db.init(conn)

    discord_bot = ShowtimesBot(settings) if settings.discord_bot_token and run_loop else None

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if discord_bot:
            tasks.append(asyncio.create_task(bot.run(discord_bot, settings.discord_bot_token)))
        if run_loop:
            tasks.append(asyncio.create_task(_loop(settings, discord_bot)))
        yield
        if discord_bot:
            await discord_bot.close()
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Baltimore Showtimes", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        clock=timefmt.clock, day=timefmt.day, day_long=timefmt.day_long, stamp=timefmt.stamp,
        LABELS=LABELS, KINDS=listing.KINDS, THEATERS=THEATERS,
    )
    configured = fetchers(settings)
    enabled = [t for t, fetch in configured.items() if fetch]  # shown on the page
    listed = list(configured)  # shown on /status, including "not configured" ones

    def conn():
        return contextlib.closing(db.connect(settings.db_path))

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        filters = listing.Filters.from_query(request.query_params, enabled)
        now = datetime.now(UTC)
        today = now.astimezone(settings.tz).date()
        with conn() as c:
            shows = listing.load(c, settings, filters, now)
            first_dates = listing.first_showing_dates(c, settings)
            last = db.kv_get(c, "last_refresh")
            problems = [THEATERS[t].short for t, r in db.sources(c).items() if r["last_error"] and t in enabled]
        grouped = listing.by_film(shows, first_dates, today) if filters.view == "film" else listing.by_date(shows)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "filters": filters,
                "enabled": enabled,
                "grouped": grouped,
                "count": len(shows),
                "settings": settings,
                "today": today,
                "last_refresh": datetime.fromisoformat(last).astimezone(settings.tz) if last else None,
                "problems": problems,
                "query": str(request.url.query),
            },
        )

    @app.get("/status", response_class=HTMLResponse)
    def status(request: Request):
        with conn() as c:
            srcs = db.sources(c)
            integrations = {
                name: db.kv_get(c, f"status:{name}")
                for name, on in (
                    ("plex_library", settings.plex_enabled),
                    ("plex_watchlist", settings.plex_watchlist_enabled),
                    ("radarr", settings.radarr_enabled),
                )
                if on
            }
            last = db.kv_get(c, "last_refresh")
        local = lambda iso: datetime.fromisoformat(iso).astimezone(settings.tz) if iso else None  # noqa: E731
        return templates.TemplateResponse(
            request,
            "status.html",
            {
                "enabled": listed,
                "sources": srcs,
                "integrations": integrations,
                "local": local,
                "settings": settings,
                "last_refresh": local(last),
                "missing": [t for t in THEATERS if t not in listed],
            },
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        with conn() as c:
            p = prefs_mod.load(c, settings)
            users = db.users(c)
            follows = db.all_follows(c)
            discord_status = db.kv_get(c, "status:discord")
        if settings.discord_bot_token:
            mode = "bot"
        elif settings.discord_webhook:
            mode = "webhook"
        else:
            mode = "off"
        bot_info = None
        if discord_bot:
            ready = discord_bot.is_ready()
            bot_info = {
                "ready": ready,
                "user": str(discord_bot.user) if ready else None,
                "invite": discord_bot.invite_url(),
                "channels": discord_bot.text_channels() if ready else [],
                "error": discord_bot.last_error,
            }
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "settings": settings, "p": p, "mode": mode, "bot": bot_info, "users": users,
                "follows": follows, "discord_status": discord_status,
                "local": lambda iso: datetime.fromisoformat(iso).astimezone(settings.tz) if iso else None,
            },
        )

    @app.post("/settings")
    async def save_settings(request: Request):
        form = await request.form()
        with conn() as c:
            p = prefs_mod.load(c, settings)
            p.default_delivery = str(form.get("default_delivery", p.default_delivery))
            p.channel_id = str(form.get("channel_id", p.channel_id))
            p.channel_kinds = [str(k) for k in form.getlist("channel_kinds")]
            p.plex_delivery = str(form.get("plex_delivery", p.plex_delivery))
            p.admin_user_id = str(form.get("admin_user_id", p.admin_user_id))
            try:
                p.return_days = int(str(form.get("return_days", p.return_days)))
            except ValueError:
                pass
            prefs_mod.save(c, p)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/unfollow")
    async def admin_unfollow(request: Request):
        form = await request.form()
        with conn() as c:
            db.unfollow(c, str(form.get("user_id", "")), str(form.get("film_key", "")))
        return RedirectResponse("/settings#follows", status_code=303)

    @app.post("/refresh")
    async def manual_refresh():
        asyncio.create_task(refresh.run(settings, discord_bot))
        return RedirectResponse("/status?started=1", status_code=303)

    @app.post("/radarr/add/{tmdb_id}")
    async def radarr_add(tmdb_id: int, request: Request):
        if not settings.radarr_enabled:
            raise HTTPException(404, "Radarr is not configured")
        back = _safe_back(request.query_params.get("next"))
        try:
            async with refresh.client() as http:
                movie = await radarr.add(
                    http, settings.radarr_url, settings.radarr_api_key, tmdb_id,
                    settings.radarr_quality_profile, settings.radarr_root_folder,
                )
        except (httpx.HTTPError, RuntimeError) as exc:
            log.warning("Radarr add failed for %s: %s", tmdb_id, exc)
            return RedirectResponse(_with_flag(back, "radarr_error"), status_code=303)
        with conn() as c:
            current = db.kv_get(c, "radarr", {})
            current[str(tmdb_id)] = {
                "monitored": True, "has_file": False,
                "digital": (movie.get("digitalRelease") or "")[:10] or None,
                "physical": (movie.get("physicalRelease") or "")[:10] or None,
                "link": f"{settings.radarr_url}/movie/{movie.get('titleSlug') or tmdb_id}",
            }
            db.kv_set(c, "radarr", current)
        return RedirectResponse(_with_flag(back, "radarr_added"), status_code=303)

    @app.get("/radarr/list.json")
    def radarr_list(request: Request):
        params = request.query_params
        filters = listing.Filters.from_query(params, enabled)
        if "range" not in params:
            filters.range = "90"
        with conn() as c:
            shows = listing.load(c, settings, filters)
        seen: OrderedDict[str, dict] = OrderedDict()
        for s in shows:
            if s["tmdb_id"] and s["tmdb_id"] not in seen:
                tid = int(s["tmdb_id"])
                seen[s["tmdb_id"]] = {
                    "id": tid, "tmdbId": tid, "tmdb_id": tid, "title": s["title"], "year": s["year"],
                    "imdb_id": None, "poster_url": s["poster"],
                }
        with conn() as c:
            for tid, item in seen.items():
                row = c.execute("SELECT imdb_id FROM films WHERE tmdb_id = ?", (int(tid),)).fetchone()
                item["imdb_id"] = row["imdb_id"] if row else None
        return JSONResponse(list(seen.values()))

    @app.get("/feed.xml")
    def feed(request: Request):
        params = request.query_params
        filters = listing.Filters.from_query(params, enabled)
        if "range" not in params:
            filters.range = "all"
        with conn() as c:
            shows = [s for s in listing.load(c, settings, filters) if "new" in s["labels"]]
            first_seen = {r["key"]: r["first_seen"] for r in c.execute("SELECT key, first_seen FROM showings")}
        groups: OrderedDict[tuple[str, str], list] = OrderedDict()
        for s in shows:
            groups.setdefault((s["film_key"], s["theater"]), []).append(s)
        base = settings.public_url or str(request.base_url).rstrip("/")
        items = []
        for (film_key, theater), group in groups.items():
            first = group[0]
            seen_at = max(datetime.fromisoformat(first_seen[s["key"]]) for s in group)
            times = ", ".join(timefmt.stamp(s["start"]) for s in group[:12])
            more = f" and {len(group) - 12} more" if len(group) > 12 else ""
            labels = ", ".join(LABELS[label] for label in first["labels"] if label != "new")
            title = f"{first['title']} at {THEATERS[theater].name}"
            desc = (first["event_name"] + " — " if first["event_name"] else "") + times + more
            if labels:
                desc += f" ({labels})"
            items.append(
                "<item>"
                f"<title>{escape(title)}</title>"
                f"<link>{escape(first['ticket_url'])}</link>"
                f"<guid isPermaLink=\"false\">{escape(film_key)}|{theater}|{seen_at.date().isoformat()}</guid>"
                f"<pubDate>{format_datetime(seen_at)}</pubDate>"
                f"<description>{escape(desc)}</description>"
                "</item>"
            )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            "<title>Baltimore Showtimes — new listings</title>"
            f"<link>{escape(base)}/</link>"
            "<description>Newly listed screenings at your theaters</description>"
            + "".join(items)
            + "</channel></rss>"
        )
        return Response(xml, media_type="application/rss+xml")

    @app.get("/calendar.ics")
    def calendar(request: Request):
        filters = listing.Filters.from_query(request.query_params, enabled)
        if "range" not in request.query_params:
            filters.range = "30"
        with conn() as c:
            shows = listing.load(c, settings, filters)[:1000]
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//baltimore-showtimes//EN",
                 "X-WR-CALNAME:Baltimore Showtimes", "CALSCALE:GREGORIAN"]
        for s in shows:
            start = s["start"].astimezone(UTC)
            minutes = s["runtime"] or 120
            end = start.timestamp() + minutes * 60
            th = THEATERS[s["theater"]]
            summary = f"{s['title']} ({th.short})"
            desc = " · ".join([s["event_name"] or "", *[LABELS[x] for x in s["labels"]], *s["formats"]]).strip(" ·")
            lines += [
                "BEGIN:VEVENT",
                f"UID:{s['key']}@baltimore-showtimes",
                f"DTSTAMP:{stamp}",
                f"DTSTART:{start:%Y%m%dT%H%M%SZ}",
                f"DTEND:{datetime.fromtimestamp(end, UTC):%Y%m%dT%H%M%SZ}",
                f"SUMMARY:{_ics(summary)}",
                f"LOCATION:{_ics(th.name + ', ' + th.address)}",
                f"URL:{s['ticket_url']}",
                f"DESCRIPTION:{_ics(desc)}",
                "END:VEVENT",
            ]
        lines.append("END:VCALENDAR")
        body = "\r\n".join(_fold(line) for line in lines) + "\r\n"
        return Response(body, media_type="text/calendar; charset=utf-8")

    @app.get("/health")
    def health():
        return PlainTextResponse("ok")

    return app


def _ics(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """Fold to 75 octets per RFC 5545."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts, chunk = [], b""
    for ch in line:
        b = ch.encode()
        if len(chunk) + len(b) > (75 if not parts else 74):
            parts.append(chunk.decode())
            chunk = b""
        chunk += b
    parts.append(chunk.decode())
    return "\r\n ".join(parts)


def _safe_back(target: str | None) -> str:
    """Only allow redirects back into this app."""
    if not target:
        return "/"
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/"):
        return "/"
    return target


def _with_flag(url: str, flag: str) -> str:
    return f"{url}{'&' if '?' in url else '?'}{flag}=1"


async def _loop(settings: config.Settings, discord_bot=None) -> None:
    if discord_bot:
        # Give the bot a moment to connect so the first refresh can deliver alerts.
        for _ in range(30):
            if discord_bot.is_ready():
                break
            await asyncio.sleep(1)
    while True:
        try:
            await refresh.run(settings, discord_bot)
        except Exception:
            log.exception("Refresh failed")
        await asyncio.sleep(settings.refresh_minutes * 60)


def app_factory() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()
