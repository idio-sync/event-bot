"""SQLite storage. One short-lived connection per unit of work."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from app.models import Showing

SCHEMA = """
CREATE TABLE IF NOT EXISTS films (
    film_key     TEXT PRIMARY KEY,          -- 'tmdb:<id>' or 'title:<normalized>'
    tmdb_id      INTEGER,
    imdb_id      TEXT,
    title        TEXT NOT NULL,
    release_date TEXT,                      -- original release (ISO date)
    poster_url   TEXT,
    overview     TEXT,
    runtime_min  INTEGER,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS showings (
    key          TEXT PRIMARY KEY,          -- '<theater>:<source id>'
    theater      TEXT NOT NULL,
    raw_title    TEXT NOT NULL,
    match_title  TEXT,
    match_key    TEXT NOT NULL,
    film_key     TEXT,
    start_utc    TEXT NOT NULL,
    source_release TEXT,
    tags         TEXT NOT NULL DEFAULT '[]',
    is_event     INTEGER NOT NULL DEFAULT 0,
    ticket_url   TEXT,
    runtime_min  INTEGER,
    synopsis     TEXT,
    poster_url   TEXT,
    sold_out     INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS showings_start ON showings(start_utc);
CREATE INDEX IF NOT EXISTS showings_film ON showings(film_key);
CREATE TABLE IF NOT EXISTS matches (
    match_key    TEXT PRIMARY KEY,          -- what a source gave us to identify the film
    film_key     TEXT NOT NULL,
    checked_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    theater      TEXT PRIMARY KEY,
    last_attempt TEXT,
    last_success TEXT,
    last_count   INTEGER,
    last_error   TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    tmdb_id      INTEGER NOT NULL,
    theater      TEXT NOT NULL,
    sent_at      TEXT NOT NULL,
    PRIMARY KEY (tmdb_id, theater)
);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def match_key_for(s: Showing) -> str:
    from app.classify import normalize_key

    if s.tmdb_id:
        return f"tmdb:{s.tmdb_id}"
    if s.imdb_id:
        return f"imdb:{s.imdb_id}"
    year = s.release_date.year if s.release_date else ""
    return f"title:{normalize_key(s.match_title or s.title)}:{year}"


def save_showings(conn: sqlite3.Connection, theater: str, showings: list[Showing]) -> int:
    """Upsert a successful fetch. Returns the number of showings never seen before.

    Future showings of this theater missing from the fetch are marked inactive
    (cancelled or moved); past ones are left alone.
    """
    now = now_iso()
    new = 0
    seen: list[str] = []
    for s in showings:
        start_utc = s.start.astimezone(UTC).isoformat(timespec="seconds")
        exists = conn.execute("SELECT 1 FROM showings WHERE key = ?", (s.key,)).fetchone()
        if not exists:
            new += 1
        conn.execute(
            """
            INSERT INTO showings (key, theater, raw_title, match_title, match_key, start_utc, source_release,
                                  tags, is_event, ticket_url, runtime_min, synopsis, poster_url, sold_out,
                                  first_seen, last_seen, active)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
            ON CONFLICT(key) DO UPDATE SET
                raw_title=excluded.raw_title, match_title=excluded.match_title, match_key=excluded.match_key,
                start_utc=excluded.start_utc, source_release=excluded.source_release, tags=excluded.tags,
                is_event=excluded.is_event, ticket_url=excluded.ticket_url, runtime_min=excluded.runtime_min,
                synopsis=excluded.synopsis, poster_url=excluded.poster_url, sold_out=excluded.sold_out,
                last_seen=excluded.last_seen, active=1
            """,
            (
                s.key, theater, s.title, s.match_title, match_key_for(s), start_utc,
                s.release_date.isoformat() if s.release_date else None, json.dumps(s.tags), int(s.is_event),
                s.ticket_url, s.runtime_min, s.synopsis, s.poster_url, int(s.sold_out), now, now,
            ),
        )
        seen.append(s.key)
    placeholders = ",".join("?" * len(seen)) or "''"
    conn.execute(
        f"UPDATE showings SET active = 0 WHERE theater = ? AND start_utc > ? AND key NOT IN ({placeholders})",
        (theater, now, *seen),
    )
    # Attach already-known matches so new showings of known films get their film at once.
    conn.execute(
        """UPDATE showings SET film_key = (SELECT film_key FROM matches m WHERE m.match_key = showings.match_key)
           WHERE theater = ? AND match_key IN (SELECT match_key FROM matches)""",
        (theater,),
    )
    conn.commit()
    return new


def record_source(conn: sqlite3.Connection, theater: str, ok: bool, count: int = 0, error: str | None = None) -> None:
    now = now_iso()
    if ok:
        conn.execute(
            """INSERT INTO sources (theater, last_attempt, last_success, last_count, last_error)
               VALUES (?,?,?,?,NULL)
               ON CONFLICT(theater) DO UPDATE SET last_attempt=excluded.last_attempt,
                   last_success=excluded.last_success, last_count=excluded.last_count, last_error=NULL""",
            (theater, now, now, count),
        )
    else:
        conn.execute(
            """INSERT INTO sources (theater, last_attempt, last_error) VALUES (?,?,?)
               ON CONFLICT(theater) DO UPDATE SET last_attempt=excluded.last_attempt, last_error=excluded.last_error""",
            (theater, now, error),
        )
    conn.commit()


def unmatched(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """One representative active showing per match key that has no film yet."""
    return conn.execute(
        """SELECT match_key, MIN(raw_title) AS raw_title, MIN(match_title) AS match_title,
                  MIN(source_release) AS source_release, MIN(runtime_min) AS runtime_min,
                  MIN(synopsis) AS synopsis, MIN(poster_url) AS poster_url
           FROM showings WHERE active = 1 AND film_key IS NULL GROUP BY match_key"""
    ).fetchall()


def save_match(conn: sqlite3.Connection, match_key: str, film: dict[str, Any]) -> None:
    now = now_iso()
    conn.execute(
        """INSERT INTO films (film_key, tmdb_id, imdb_id, title, release_date, poster_url, overview, runtime_min, updated_at)
           VALUES (:film_key, :tmdb_id, :imdb_id, :title, :release_date, :poster_url, :overview, :runtime_min, :now)
           ON CONFLICT(film_key) DO UPDATE SET tmdb_id=excluded.tmdb_id, imdb_id=COALESCE(excluded.imdb_id, films.imdb_id),
               title=excluded.title, release_date=COALESCE(excluded.release_date, films.release_date),
               poster_url=COALESCE(excluded.poster_url, films.poster_url),
               overview=COALESCE(excluded.overview, films.overview),
               runtime_min=COALESCE(excluded.runtime_min, films.runtime_min), updated_at=excluded.updated_at""",
        {**{k: film.get(k) for k in ("film_key", "tmdb_id", "imdb_id", "title", "release_date",
                                     "poster_url", "overview", "runtime_min")}, "now": now},
    )
    conn.execute(
        "INSERT OR REPLACE INTO matches (match_key, film_key, checked_at) VALUES (?,?,?)",
        (match_key, film["film_key"], now),
    )
    conn.execute("UPDATE showings SET film_key = ? WHERE match_key = ?", (film["film_key"], match_key))
    conn.commit()


def forget_title_matches(conn: sqlite3.Connection) -> None:
    """Retry title-only matches (e.g. after a TMDB key is added)."""
    conn.execute("DELETE FROM matches WHERE film_key LIKE 'title:%'")
    conn.execute("UPDATE showings SET film_key = NULL WHERE film_key LIKE 'title:%'")
    conn.commit()


def upcoming(conn: sqlite3.Connection, start_utc: str, end_utc: str, theaters: Iterable[str]) -> list[sqlite3.Row]:
    theaters = list(theaters)
    placeholders = ",".join("?" * len(theaters)) or "''"
    return conn.execute(
        f"""SELECT s.*, f.tmdb_id, f.imdb_id, f.title AS film_title, f.release_date, f.overview,
                   f.poster_url AS film_poster, f.runtime_min AS film_runtime
            FROM showings s LEFT JOIN films f ON f.film_key = s.film_key
            WHERE s.active = 1 AND s.start_utc >= ? AND s.start_utc < ? AND s.theater IN ({placeholders})
            ORDER BY s.start_utc""",
        (start_utc, end_utc, *theaters),
    ).fetchall()


def sources(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["theater"]: r for r in conn.execute("SELECT * FROM sources")}


def kv_get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?,?,?)", (key, json.dumps(value), now_iso())
    )
    conn.commit()


def alerted(conn: sqlite3.Connection) -> set[tuple[int, str]]:
    return {(r["tmdb_id"], r["theater"]) for r in conn.execute("SELECT tmdb_id, theater FROM alerts")}


def mark_alerted(conn: sqlite3.Connection, pairs: Iterable[tuple[int, str]]) -> None:
    now = now_iso()
    conn.executemany("INSERT OR IGNORE INTO alerts (tmdb_id, theater, sent_at) VALUES (?,?,?)",
                     [(t, th, now) for t, th in pairs])
    conn.commit()
