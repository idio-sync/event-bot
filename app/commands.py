"""Discord slash-command logic, independent of discord.py (the bot module is thin glue).

Every handler returns a Reply; autocomplete helpers return (name, value) pairs where
value is a film key ('tmdb:<id>' or 'title:<normalized>').
"""

from __future__ import annotations

import re
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from app import classify, db, listing
from app.config import Settings
from app.integrations import discord
from app.sources import THEATERS, fetchers
from app.timefmt import clock, day
from app.tmdb import Tmdb

WHEN = {
    "today": "Today",
    "tomorrow": "Tomorrow",
    "weekend": "This weekend",
    "week": "Next 7 days",
    "month": "Next 30 days",
}
KIND_TITLES = {"event": "Special events", "rerelease": "Re-releases", "coming": "Coming up"}
LABEL_TEXT = {"rerelease": "Re-release", "event": "Special event", "advance": "Advance", "new": "New"}
MAX_CHOICES = 25
DESC_LIMIT = 3800
_KEY = re.compile(r"^(tmdb:\d+|title:[a-z0-9]+)$")


@dataclass
class Reply:
    content: str = ""
    embeds: list[dict[str, Any]] = field(default_factory=list)
    ephemeral: bool = False


# ---- time windows -------------------------------------------------------------

def window(when: str, today: date) -> tuple[date, date]:
    """Inclusive start, exclusive end (local dates)."""
    if when == "today":
        return today, today + timedelta(days=1)
    if when == "tomorrow":
        return today + timedelta(days=1), today + timedelta(days=2)
    if when == "weekend":
        # Fri–Sun; on a weekend day, the rest of this weekend.
        friday = today + timedelta(days=(4 - today.weekday()) % 7) if today.weekday() < 4 else today
        sunday = today + timedelta(days=6 - today.weekday())
        return friday, sunday + timedelta(days=1)
    if when == "month":
        return today, today + timedelta(days=30)
    return today, today + timedelta(days=7)


# ---- data helpers --------------------------------------------------------------

def _enabled(settings: Settings) -> list[str]:
    return [t for t, fetch in fetchers(settings).items() if fetch]


def _shows(conn: sqlite3.Connection, settings: Settings, theater: str | None = None,
           kinds: list[str] | None = None, range_: str = "all") -> list[dict[str, Any]]:
    theaters = [theater] if theater and theater in _enabled(settings) else _enabled(settings)
    return listing.load(conn, settings, listing.Filters(theaters=theaters, range=range_, kinds=kinds or []))


def playing(conn: sqlite3.Connection, settings: Settings) -> OrderedDict[str, dict[str, Any]]:
    """Films with upcoming showings: film_key -> {title, year}."""
    out: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for s in _shows(conn, settings):
        out.setdefault(s["film_key"], {"title": s["title"], "year": s["year"]})
    return out


def _label(title: str, year: int | None, suffix: str = "") -> str:
    text = f"{title} ({year})" if year else title
    return (text + suffix)[:100]


def autocomplete_playing(conn: sqlite3.Connection, settings: Settings, text: str) -> list[tuple[str, str]]:
    needle = text.strip().lower()
    films = playing(conn, settings)
    hits = [(k, f) for k, f in films.items() if needle in f["title"].lower()]
    hits.sort(key=lambda kf: (not kf[1]["title"].lower().startswith(needle), kf[1]["title"]))
    return [(_label(f["title"], f["year"]), k) for k, f in hits[:MAX_CHOICES]]


async def autocomplete_follow(conn: sqlite3.Connection, settings: Settings, http: httpx.AsyncClient,
                              text: str) -> list[tuple[str, str]]:
    """Playing films first, then anything on TMDB (to follow films that aren't showing yet)."""
    choices = [(name + " · playing", key) for name, key in autocomplete_playing(conn, settings, text)]
    seen = {key for _, key in choices}
    if settings.tmdb_key and len(text.strip()) >= 2:
        try:
            data = await Tmdb(http, settings.tmdb_key)._get("/search/movie", query=text.strip(), include_adult="false")
        except httpx.HTTPError:
            data = {}
        for r in (data.get("results") or [])[:MAX_CHOICES]:
            key = f"tmdb:{r['id']}"
            if key in seen:
                continue
            year = int(r["release_date"][:4]) if (r.get("release_date") or "")[:4].isdigit() else None
            choices.append((_label(r.get("title") or "?", year), key))
            seen.add(key)
    return choices[:MAX_CHOICES]


def resolve_playing(conn: sqlite3.Connection, settings: Settings, value: str) -> str | None:
    """Film key for an autocomplete value or typed title, among films that are playing."""
    films = playing(conn, settings)
    if _KEY.match(value):
        return value if value in films else None
    want = classify.normalize_key(value)
    exact = [k for k, f in films.items() if classify.normalize_key(f["title"]) == want]
    if exact:
        return exact[0]
    loose = [k for k, f in films.items() if want and want in classify.normalize_key(f["title"])]
    return loose[0] if len(loose) == 1 else None


# ---- formatting ------------------------------------------------------------------

def _schedule_lines(theaters: OrderedDict, today: date) -> list[str]:
    lines = []
    for theater, days in theaters.items():
        parts = []
        for d, shows in days.items():
            label = "Today" if d == today else day(d)
            times = ", ".join(clock(s["start"]) + (" (sold out)" if s["sold_out"] else "") for s in shows)
            parts.append(f"{label} {times}")
        lines.append(f"{THEATERS[theater].short}: " + " · ".join(parts))
    return lines


def _film_block(film: dict[str, Any], today: date) -> str:
    year = f" ({film['year']})" if film.get("year") else ""
    labels = [LABEL_TEXT[label] for label in film["labels"] if label in LABEL_TEXT and label != "new"]
    head = f"**{film['title']}**{year}" + (" · " + ", ".join(labels) if labels else "")
    events = [f"★ {e}" for e in film.get("events", [])[:2]]
    return "\n".join([head, *events, *_schedule_lines(film["theaters"], today)])


def _listing_embed(title: str, films: list[dict[str, Any]], today: date, settings: Settings) -> dict[str, Any]:
    blocks, used = [], 0
    for i, film in enumerate(films):
        block = _film_block(film, today)
        if used + len(block) > DESC_LIMIT:
            more = len(films) - i
            tail = f"\n…and {more} more film{'s' if more != 1 else ''}"
            if settings.public_url:
                tail += f" — {settings.public_url}/"
            blocks.append(tail)
            break
        blocks.append(block)
        used += len(block) + 2
    return discord.embed(title, settings.public_url or None, "\n\n".join(blocks) or "Nothing found.", None)


def _now(settings: Settings) -> tuple[datetime, date]:
    now = datetime.now(UTC)
    return now, now.astimezone(settings.tz).date()


# ---- commands ----------------------------------------------------------------------

def showtimes(conn: sqlite3.Connection, settings: Settings, theater: str | None, when: str) -> Reply:
    _, today = _now(settings)
    start, end = window(when, today)
    shows = [s for s in _shows(conn, settings, theater, range_="30") if start <= s["start"].date() < end]
    films = listing.by_film(shows, {}, today)["now"]
    where = THEATERS[theater].name if theater in THEATERS else "all theaters"
    title = f"{WHEN.get(when, 'Next 7 days')} at {where}"
    if not films:
        return Reply(f"Nothing listed {WHEN.get(when, 'next 7 days').lower()} at {where}.")
    return Reply(embeds=[_listing_embed(title, films, today, settings)])


def by_kind(conn: sqlite3.Connection, settings: Settings, kind: str, theater: str | None = None) -> Reply:
    _, today = _now(settings)
    if kind == "coming":
        shows = _shows(conn, settings, theater, range_="90")
        films = listing.by_film(shows, listing.first_showing_dates(conn, settings), today)["soon"]
    else:
        shows = _shows(conn, settings, theater, kinds=[kind], range_="90")
        films = listing.by_film(shows, {}, today)["now"]
        films.sort(key=lambda f: f["start"])
    if not films:
        return Reply(f"No {KIND_TITLES[kind].lower()} listed in the next 3 months.")
    return Reply(embeds=[_listing_embed(f"{KIND_TITLES[kind]} — next 3 months", films, today, settings)])


def film(conn: sqlite3.Connection, settings: Settings, value: str) -> Reply:
    key = resolve_playing(conn, settings, value)
    if not key:
        return Reply(f"“{value}” isn't playing at any of the theaters. Try `/follow` to hear when it does.",
                     ephemeral=True)
    _, today = _now(settings)
    shows = [s for s in _shows(conn, settings) if s["film_key"] == key]
    grouped = listing.by_film(shows, {}, today)
    f = (grouped["now"] or grouped["soon"])[0]
    labels = [LABEL_TEXT[x] for x in f["labels"] if x in LABEL_TEXT and x != "new"]
    parts = []
    if labels:
        parts.append("**" + " · ".join(labels) + "**")
    if f.get("overview"):
        parts.append(f["overview"][:600])
    parts += [f"★ {e}" for e in f.get("events", [])[:3]]
    parts.append("\n".join(_schedule_lines(f["theaters"], today)))
    desc = "\n\n".join(parts)
    return Reply(embeds=[discord.embed(_label(f["title"], f["year"]), f["ticket_url"], desc[:DESC_LIMIT], f.get("poster"))])


async def follow(conn: sqlite3.Connection, settings: Settings, http: httpx.AsyncClient,
                 user_id: str, user_name: str, value: str) -> Reply:
    db.upsert_user(conn, user_id, user_name)
    films = playing(conn, settings)
    key = resolve_playing(conn, settings, value)
    title, year = (films[key]["title"], films[key]["year"]) if key else (None, None)
    if not key and settings.tmdb_key:
        api = Tmdb(http, settings.tmdb_key)
        try:
            if not _KEY.match(value):  # typed a title instead of picking a suggestion
                found = await api.search(classify.clean_title(value), None)
                value = f"tmdb:{found}" if found else value
            if value.startswith("tmdb:"):
                movie = await api.movie(int(value.split(":")[1]))
                key, title = value, movie.get("title") or value
                year = int(movie["release_date"][:4]) if (movie.get("release_date") or "")[:4].isdigit() else None
        except httpx.HTTPError:
            return Reply("Couldn't reach TMDB to look that film up. Try again in a bit.", ephemeral=True)
    if not key:
        if _KEY.match(value):
            return Reply("I couldn't find that film.", ephemeral=True)
        # No TMDB match: follow by title so a later listing with the same title still matches.
        title = classify.clean_title(value)
        key = f"title:{classify.normalize_key(title)}"
    added = db.follow(conn, user_id, key, title, year)
    name = _label(title, year)
    if not added:
        return Reply(f"You already follow **{name}**.", ephemeral=True)
    status = "It's playing now — you'll get an alert after the next refresh." if key in films else \
        "You'll get an alert when a theater lists it."
    return Reply(f"Following **{name}**. {status}", ephemeral=True)


def autocomplete_following(conn: sqlite3.Connection, user_id: str, text: str) -> list[tuple[str, str]]:
    needle = text.strip().lower()
    return [(_label(r["title"], r["year"]), r["film_key"]) for r in db.follows_of(conn, user_id)
            if needle in r["title"].lower()][:MAX_CHOICES]


def unfollow(conn: sqlite3.Connection, user_id: str, value: str) -> Reply:
    rows = db.follows_of(conn, user_id)
    match = next((r for r in rows if r["film_key"] == value), None) or next(
        (r for r in rows if classify.normalize_key(r["title"]) == classify.normalize_key(value)), None)
    if not match or not db.unfollow(conn, user_id, match["film_key"]):
        return Reply("You don't follow that film.", ephemeral=True)
    return Reply(f"Stopped following **{_label(match['title'], match['year'])}**.", ephemeral=True)


def following(conn: sqlite3.Connection, settings: Settings, user_id: str) -> Reply:
    rows = db.follows_of(conn, user_id)
    if not rows:
        return Reply("You don't follow any films yet. Use `/follow` to add one.", ephemeral=True)
    _, today = _now(settings)
    shows = _shows(conn, settings)
    next_show: dict[str, dict[str, Any]] = {}
    for s in shows:
        next_show.setdefault(s["film_key"], s)
    lines = []
    for r in rows:
        s = next_show.get(r["film_key"])
        where = f" — next: {THEATERS[s['theater']].short}, {'Today' if s['start'].date() == today else day(s['start'].date())} {clock(s['start'])}" if s else " — not playing yet"
        lines.append(f"• {_label(r['title'], r['year'])}{where}")
    return Reply(embeds=[discord.embed("Films you follow", None, "\n".join(lines), None)], ephemeral=True)


def set_alerts(conn: sqlite3.Connection, settings: Settings, user_id: str, user_name: str, choice: str,
               default: str) -> Reply:
    delivery = choice if choice in ("channel", "dm") else None
    db.set_delivery(conn, user_id, user_name, delivery)
    effective = delivery or default
    how = "by DM" if effective == "dm" else "as an @mention in the alerts channel"
    note = " (server default)" if delivery is None else ""
    return Reply(f"You'll get alerts for films you follow {how}{note}.", ephemeral=True)
