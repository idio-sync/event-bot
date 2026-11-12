"""Turn stored showings into filtered, labelled, grouped view models."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app import classify, db
from app.config import Settings
from app.sources import THEATERS

KINDS = {
    "rerelease": "Re-releases",
    "event": "Special events",
    "advance": "Advance screenings",
    "new": "New listings",
    "watchlist": "On my watchlist",
}
AGE_PRESETS = {"any": None, "5y": 60, "25y": 300}  # None = settings.rerelease_months
RANGES = {"7": 7, "14": 14, "30": 30, "90": 90, "all": 400}
NEW_DAYS = 7
BASELINE_GRACE = timedelta(hours=1)


@dataclass
class Filters:
    theaters: list[str]
    view: str = "film"
    range: str = "14"
    kinds: list[str] = field(default_factory=list)
    age: str = "any"
    q: str = ""
    hide_watched: bool = False
    not_owned: bool = False

    @classmethod
    def from_query(cls, params: Any, enabled: list[str]) -> Filters:
        theaters = [t for t in params.getlist("theater") if t in enabled] or list(enabled)
        kinds = [k for k in params.getlist("kinds") if k in KINDS]
        return cls(
            theaters=theaters,
            view=params.get("view") if params.get("view") in ("film", "date") else "film",
            range=params.get("range") if params.get("range") in RANGES else "14",
            kinds=kinds,
            age=params.get("age") if params.get("age") in AGE_PRESETS else "any",
            q=(params.get("q") or "").strip(),
            hide_watched=params.get("hide_watched") == "1",
            not_owned=params.get("not_owned") == "1",
        )

    def rerelease_months(self, settings: Settings) -> int:
        return AGE_PRESETS[self.age] or settings.rerelease_months


@dataclass
class Context:
    """External state the labels depend on."""

    settings: Settings
    plex_movies: dict[str, Any]
    plex_machine: str | None
    watchlist: dict[str, Any]
    radarr: dict[str, Any]
    baselines: dict[str, datetime] = field(default_factory=dict)  # theater -> first fetch time

    @classmethod
    def load(cls, conn: sqlite3.Connection, settings: Settings) -> Context:
        lib = db.kv_get(conn, "plex_library", {}) if settings.plex_enabled else {}
        return cls(
            baselines={
                r["theater"]: datetime.fromisoformat(r["first"])
                for r in conn.execute("SELECT theater, MIN(first_seen) AS first FROM showings GROUP BY theater")
            },
            settings=settings,
            plex_movies=lib.get("movies", {}),
            plex_machine=lib.get("machine"),
            watchlist=db.kv_get(conn, "plex_watchlist", {}) if settings.plex_watchlist_enabled else {},
            radarr=db.kv_get(conn, "radarr", {}) if settings.radarr_enabled else {},
        )


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _d(raw: str | None) -> date | None:
    try:
        return date.fromisoformat(raw[:10]) if raw else None
    except ValueError:
        return None


def screening(row: sqlite3.Row, ctx: Context, filters: Filters, now: datetime) -> dict[str, Any]:
    tz = ctx.settings.tz
    start = datetime.fromisoformat(row["start_utc"]).astimezone(tz)
    tags = json.loads(row["tags"] or "[]")
    film_release = _d(row["release_date"]) or _d(row["source_release"])
    opening = _d(row["source_release"]) or film_release
    raw = row["raw_title"]
    title = row["film_title"] or classify.clean_title(row["match_title"] or raw)
    event = classify.is_event(raw, tags, bool(row["is_event"]))
    tmdb_id = str(row["tmdb_id"]) if row["tmdb_id"] else None
    plex = ctx.plex_movies.get(tmdb_id) if tmdb_id else None
    labels = []
    if classify.is_rerelease(film_release, start.date(), filters.rerelease_months(ctx.settings)):
        labels.append("rerelease")
    if event:
        labels.append("event")
    if classify.is_advance(opening, start.date()):
        labels.append("advance")
    first_seen = datetime.fromisoformat(row["first_seen"])
    baseline = ctx.baselines.get(row["theater"])
    # Everything is "first seen" on a theater's first fetch; only later arrivals count as new.
    if now - first_seen <= timedelta(days=NEW_DAYS) and baseline and first_seen > baseline + BASELINE_GRACE:
        labels.append("new")
    if tmdb_id and tmdb_id in ctx.watchlist:
        labels.append("watchlist")
    # Show the theater's own wording for events when it says more than the film title (Q&A, panels, ...).
    event_name = None
    if event and _alnum(raw) != _alnum(title):
        event_name = classify.smart_title_case(raw) if raw.isupper() else raw
    return {
        "key": row["key"],
        "theater": row["theater"],
        "start": start,
        "film_key": row["film_key"] or row["match_key"],
        "title": title,
        "event_name": event_name,
        "year": film_release.year if film_release else None,
        "release": film_release,
        "poster": row["film_poster"] or row["poster_url"],
        "overview": row["overview"] or row["synopsis"],
        "runtime": row["film_runtime"] or row["runtime_min"],
        "labels": labels,
        "formats": classify.formats(raw, tags),
        "ticket_url": row["ticket_url"] or THEATERS[row["theater"]].url,
        "sold_out": bool(row["sold_out"]),
        "tmdb_id": tmdb_id,
        "plex": plex,
        "plex_link": (
            f"https://app.plex.tv/desktop/#!/server/{ctx.plex_machine}/details?key=%2Flibrary%2Fmetadata%2F{plex['rating_key']}"
            if plex and ctx.plex_machine else None
        ),
        "radarr": ctx.radarr.get(tmdb_id) if tmdb_id else None,
    }


def _keep(s: dict[str, Any], f: Filters) -> bool:
    if f.kinds and not any(k in s["labels"] for k in f.kinds):
        return False
    if f.q:
        hay = f"{s['title']} {s['event_name'] or ''}".lower()
        if f.q.lower() not in hay:
            return False
    if f.hide_watched and s["plex"] and s["plex"].get("watched"):
        return False
    if f.not_owned and s["plex"]:
        return False
    return True


def load(conn: sqlite3.Connection, settings: Settings, filters: Filters, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    ctx = Context.load(conn, settings)
    local_now = now.astimezone(settings.tz)
    # Include shows that started up to 30 minutes ago (still worth seeing on the page).
    start = (now - timedelta(minutes=30)).isoformat(timespec="seconds")
    end_day = local_now.date() + timedelta(days=RANGES[filters.range])
    end = datetime.combine(end_day, datetime.min.time(), settings.tz).astimezone(UTC).isoformat(timespec="seconds")
    rows = db.upcoming(conn, start, end, filters.theaters)
    shows = [screening(r, ctx, filters, now) for r in rows]
    return [s for s in shows if _keep(s, filters)]


def first_showing_dates(conn: sqlite3.Connection, settings: Settings) -> dict[str, date]:
    """Earliest stored showing per film, in local time (to spot films that haven't opened yet)."""
    out: dict[str, date] = {}
    for r in conn.execute(
        "SELECT COALESCE(film_key, match_key) AS fk, MIN(start_utc) AS first FROM showings WHERE active = 1 GROUP BY fk"
    ):
        out[r["fk"]] = datetime.fromisoformat(r["first"]).astimezone(settings.tz).date()
    return out


def by_film(shows: list[dict[str, Any]], first_dates: dict[str, date], today: date) -> dict[str, list[dict[str, Any]]]:
    """{'now': [film...], 'soon': [film...]} — each film has its screenings grouped by theater then day."""
    films: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for s in shows:
        film = films.get(s["film_key"])
        if film is None:
            film = films[s["film_key"]] = {**s, "labels": set(), "theaters": OrderedDict(), "count": 0, "events": []}
        film["labels"].update(s["labels"])
        if s["event_name"] and s["event_name"] not in film["events"]:
            film["events"].append(s["event_name"])
        film["formats"] = sorted(set(film["formats"]) | set(s["formats"]))
        days = film["theaters"].setdefault(s["theater"], OrderedDict())
        days.setdefault(s["start"].date(), []).append(s)
        film["count"] += 1
    now_list, soon_list = [], []
    for film in films.values():
        film["labels"] = [k for k in KINDS if k in film["labels"]]
        first = first_dates.get(film["film_key"])
        (soon_list if first and first > today else now_list).append(film)
    now_list.sort(key=lambda f: ("watchlist" not in f["labels"], f["start"]))
    soon_list.sort(key=lambda f: f["start"])
    return {"now": now_list, "soon": soon_list}


def by_date(shows: list[dict[str, Any]]) -> OrderedDict[date, list[dict[str, Any]]]:
    """{day: [film...]} — each film lists that day's screenings per theater."""
    days: OrderedDict[date, OrderedDict[str, dict[str, Any]]] = OrderedDict()
    for s in shows:
        films = days.setdefault(s["start"].date(), OrderedDict())
        film = films.get(s["film_key"])
        if film is None:
            film = films[s["film_key"]] = {**s, "labels": set(), "theaters": OrderedDict(), "events": []}
        film["labels"].update(s["labels"])
        if s["event_name"] and s["event_name"] not in film["events"]:
            film["events"].append(s["event_name"])
        film["theaters"].setdefault(s["theater"], []).append(s)
    out: OrderedDict[date, list[dict[str, Any]]] = OrderedDict()
    for day, films in days.items():
        for film in films.values():
            film["labels"] = [k for k in KINDS if k in film["labels"]]
        out[day] = list(films.values())
    return out
