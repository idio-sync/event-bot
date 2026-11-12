from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import commands, db
from app.config import Settings
from app.models import Showing

TZ = ZoneInfo("America/New_York")


def at(days: int, hour: int) -> datetime:
    return datetime.now(TZ).replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=days)


@pytest.fixture
def env(tmp_path):
    settings = Settings(data_dir=str(tmp_path), theaters=("charles", "senator"))
    conn = db.connect(settings.db_path)
    db.init(conn)
    db.save_showings(conn, "charles", [
        Showing("charles", "1", "VERTIGO", at(1, 19), tmdb_id=426, release_date=date(1958, 7, 21)),
        Showing("charles", "2", "MET OPERA: OTELLO", at(3, 13)),
    ])
    db.save_showings(conn, "senator", [Showing("senator", "3", "WILD HORSE NINE", at(1, 21))])
    db.save_match(conn, "tmdb:426", {"film_key": "tmdb:426", "tmdb_id": 426, "title": "Vertigo",
                                     "release_date": "1958-05-09", "overview": "Obsession."})
    from app.tmdb import film_from_row
    for row in db.unmatched(conn):
        db.save_match(conn, row["match_key"], film_from_row(row))
    yield settings, conn
    conn.close()


def test_window():
    thu = date(2026, 11, 12)
    assert commands.window("today", thu) == (thu, date(2026, 11, 13))
    assert commands.window("weekend", thu) == (date(2026, 11, 13), date(2026, 11, 16))
    sat = date(2026, 11, 14)
    assert commands.window("weekend", sat) == (sat, date(2026, 11, 16))
    assert commands.window("week", thu)[1] == date(2026, 11, 19)


def test_showtimes_and_kinds(env):
    settings, conn = env
    r = commands.showtimes(conn, settings, None, "week")
    desc = r.embeds[0]["description"]
    assert "**Vertigo** (1958) · Re-release" in desc and "Wild Horse Nine" in desc
    r = commands.showtimes(conn, settings, "senator", "week")
    assert "Vertigo" not in r.embeds[0]["description"]
    r = commands.by_kind(conn, settings, "event")
    assert "Otello" in r.embeds[0]["description"] and "Vertigo" not in r.embeds[0]["description"]
    r = commands.by_kind(conn, settings, "rerelease")
    assert "Vertigo" in r.embeds[0]["description"]


def test_autocomplete_and_film(env):
    settings, conn = env
    assert commands.autocomplete_playing(conn, settings, "ver") == [("Vertigo (1958)", "tmdb:426")]
    r = commands.film(conn, settings, "tmdb:426")
    assert r.embeds[0]["title"] == "Vertigo (1958)"
    assert "Obsession." in r.embeds[0]["description"] and "Charles:" in r.embeds[0]["description"]
    assert commands.film(conn, settings, "vertigo").embeds  # typed title works too
    assert commands.film(conn, settings, "Jaws").ephemeral


def test_follow_unfollow_following_without_tmdb(env):
    settings, conn = env

    async def follow(value):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as http:
            return await commands.follow(conn, settings, http, "1", "Alex", value)

    r = asyncio.run(follow("tmdb:426"))
    assert "Following **Vertigo (1958)**" in r.content and "playing now" in r.content and r.ephemeral
    assert "already follow" in asyncio.run(follow("Vertigo")).content
    r = asyncio.run(follow("The Thing"))  # not playing, no TMDB: follows by title
    assert "when a theater lists it" in r.content
    assert db.followers(conn) == {"tmdb:426": ["1"], "title:thing": ["1"]}
    desc = commands.following(conn, settings, "1").embeds[0]["description"]
    assert "Vertigo (1958) — next: Charles" in desc and "The Thing — not playing yet" in desc
    assert commands.autocomplete_following(conn, "1", "thi") == [("The Thing", "title:thing")]
    assert "Stopped following" in commands.unfollow(conn, "1", "title:thing").content
    assert "don't follow" in commands.unfollow(conn, "1", "title:thing").content


def test_follow_unreleased_film_via_tmdb(env, tmp_path):
    settings, conn = env
    settings = Settings(data_dir=settings.data_dir, theaters=settings.theaters, tmdb_key="k")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/3/search/movie":
            return httpx.Response(200, json={"results": [{"id": 77, "title": "The Thing", "release_date": "1982-06-25"}]})
        return httpx.Response(200, json={"id": 77, "title": "The Thing", "release_date": "1982-06-25"})

    async def go(fn, *args):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await fn(conn, settings, http, *args)

    choices = asyncio.run(go(commands.autocomplete_follow, "thing"))
    assert ("The Thing (1982)", "tmdb:77") in choices
    r = asyncio.run(go(commands.follow, "1", "Alex", "the thing"))
    assert "Following **The Thing (1982)**" in r.content
    assert db.followers(conn)["tmdb:77"] == ["1"]


def test_set_alerts(env):
    settings, conn = env
    assert "by DM" in commands.set_alerts(conn, settings, "1", "Alex", "dm", "channel").content
    assert db.deliveries(conn) == {"1": "dm"}
    r = commands.set_alerts(conn, settings, "1", "Alex", "default", "channel")
    assert "@mention" in r.content and "(server default)" in r.content
    assert db.deliveries(conn) == {}
