"""End to end: store showings, match films, render the page and feeds."""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import db, listing
from app.config import Settings
from app.main import _fold, _safe_back, create_app
from app.models import Showing

TZ = ZoneInfo("America/New_York")


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=str(tmp_path), theaters=("charles", "senator", "cinemark-towson"))


def _at(days: int, hour: int) -> datetime:
    base = datetime.now(TZ).replace(hour=hour, minute=0, second=0, microsecond=0)
    return base + timedelta(days=days)


@pytest.fixture
def populated(settings):
    with contextlib.closing(db.connect(settings.db_path)) as conn:
        db.init(conn)
        charles = [
            Showing("charles", "1", "VERTIGO", _at(2, 11), tmdb_id=426, release_date=date(1958, 7, 21)),
            Showing("charles", "3", "MET OPERA: OTELLO", _at(5, 13)),
        ]
        db.save_showings(conn, "charles", charles)
        db.save_showings(conn, "senator", [
            Showing("senator", "10", "DUNE PART THREE", _at(1, 19), release_date=date.today() + timedelta(days=30)),
            Showing("senator", "11", "WILD HORSE NINE", _at(1, 21), release_date=date.today() - timedelta(days=6)),
        ])
        # Pretend that first fetch was two days ago, then a later fetch adds one showing.
        two_days_ago = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
        conn.execute("UPDATE showings SET first_seen = ?", (two_days_ago,))
        conn.commit()
        db.save_showings(conn, "charles", [
            *charles, Showing("charles", "2", "VERTIGO", _at(4, 19), tmdb_id=426, release_date=date(1958, 7, 21)),
        ])
        db.save_match(conn, "tmdb:426", {
            "film_key": "tmdb:426", "tmdb_id": 426, "imdb_id": "tt0052357", "title": "Vertigo",
            "release_date": "1958-05-09", "poster_url": None, "overview": "Obsession.", "runtime_min": 128,
        })
        for row in db.unmatched(conn):
            from app.tmdb import film_from_row
            db.save_match(conn, row["match_key"], film_from_row(row))
        db.kv_set(conn, "last_refresh", db.now_iso())
    return settings


def test_save_showings_marks_missing_future_shows_inactive(settings):
    with contextlib.closing(db.connect(settings.db_path)) as conn:
        db.init(conn)
        a = Showing("charles", "1", "A", _at(1, 19))
        b = Showing("charles", "2", "B", _at(2, 19))
        assert db.save_showings(conn, "charles", [a, b]) == 2
        assert db.save_showings(conn, "charles", [a]) == 0
        active = {r["key"]: r["active"] for r in conn.execute("SELECT key, active FROM showings")}
        assert active == {"charles:1": 1, "charles:2": 0}


def test_labels(populated):
    with contextlib.closing(db.connect(populated.db_path)) as conn:
        f = listing.Filters(theaters=["charles", "senator"], range="30")
        shows = {s["key"]: s for s in listing.load(conn, populated, f)}
    assert "rerelease" in shows["charles:1"]["labels"]
    assert shows["charles:1"]["title"] == "Vertigo"
    assert shows["charles:1"]["year"] == 1958
    assert "event" in shows["charles:3"]["labels"]
    assert "advance" in shows["senator:10"]["labels"]
    assert "rerelease" not in shows["senator:11"]["labels"]
    assert {k for k, s in shows.items() if "new" in s["labels"]} == {"charles:2"}


def test_kind_filter_and_search(populated):
    with contextlib.closing(db.connect(populated.db_path)) as conn:
        only_re = listing.load(conn, populated, listing.Filters(theaters=["charles", "senator"], kinds=["rerelease"]))
        assert {s["title"] for s in only_re} == {"Vertigo"}
        found = listing.load(conn, populated, listing.Filters(theaters=["charles", "senator"], q="dune"))
        assert {s["title"] for s in found} == {"Dune Part Three"}


def test_page_renders_both_views(populated):
    app = create_app(populated, run_loop=False)
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "Vertigo" in r.text and "Re-release" in r.text
        assert "Otello" in r.text
        r = client.get("/?view=date&kinds=event")
        assert r.status_code == 200
        assert "Otello" in r.text and "Vertigo" not in r.text
        assert client.get("/status").status_code == 200


def test_feeds(populated):
    app = create_app(populated, run_loop=False)
    with TestClient(app) as client:
        rss = client.get("/feed.xml")
        assert rss.status_code == 200 and "<item>" in rss.text and "Vertigo at The Charles Theatre" in rss.text
        ics = client.get("/calendar.ics?kinds=rerelease")
        assert ics.text.count("BEGIN:VEVENT") == 2
        assert "SUMMARY:Vertigo (Charles)" in ics.text
        lst = client.get("/radarr/list.json").json()
        assert lst == [{"id": 426, "tmdbId": 426, "tmdb_id": 426, "title": "Vertigo", "year": 1958,
                        "imdb_id": "tt0052357", "poster_url": None}]


def test_plex_and_watchlist_badges(populated):
    s = Settings(data_dir=populated.data_dir, theaters=populated.theaters, plex_url="http://plex", plex_token="t")
    with contextlib.closing(db.connect(s.db_path)) as conn:
        db.kv_set(conn, "plex_library", {"machine": "abc", "movies": {"426": {"watched": True, "rating_key": "9"}}})
        db.kv_set(conn, "plex_watchlist", {"426": {"title": "Vertigo", "year": 1958}})
        shows = listing.load(conn, s, listing.Filters(theaters=["charles"], kinds=["watchlist"]))
        assert {x["title"] for x in shows} == {"Vertigo"}
        assert shows[0]["plex"]["watched"]
        assert "abc" in shows[0]["plex_link"]
        hidden = listing.load(conn, s, listing.Filters(theaters=["charles"], hide_watched=True))
        assert {x["title"] for x in hidden} == {"Otello"}


def test_safe_back_only_allows_local_paths():
    assert _safe_back("/?view=date") == "/?view=date"
    assert _safe_back("https://evil.example/") == "/"
    assert _safe_back("//evil.example/") == "/"
    assert _safe_back(None) == "/"


def test_fold_keeps_lines_within_75_octets():
    folded = _fold("DESCRIPTION:" + "é" * 100)
    assert all(len(part.encode()) <= 75 for part in folded.split("\r\n"))
    assert folded.replace("\r\n ", "") == "DESCRIPTION:" + "é" * 100


def test_settings_page_saves_prefs_and_removes_follows(populated):
    from app import prefs as prefs_mod

    s = Settings(data_dir=populated.data_dir, theaters=populated.theaters, discord_webhook="https://discord.test/h")
    with contextlib.closing(db.connect(s.db_path)) as conn:
        db.upsert_user(conn, "1", "Alex")
        db.follow(conn, "1", "tmdb:426", "Vertigo", 1958)
    app = create_app(s, run_loop=False)
    with TestClient(app) as client:
        page = client.get("/settings")
        assert page.status_code == 200
        assert "Webhook mode" in page.text and "Alex" in page.text and "Vertigo" in page.text
        r = client.post("/settings", data={
            "default_delivery": "dm", "channel_kinds": ["event"], "plex_delivery": "off",
            "admin_user_id": " 1234 ", "return_days": "45",
        }, follow_redirects=False)
        assert r.status_code == 303
        r = client.post("/settings/unfollow", data={"user_id": "1", "film_key": "tmdb:426"}, follow_redirects=False)
        assert r.status_code == 303
    with contextlib.closing(db.connect(s.db_path)) as conn:
        p = prefs_mod.load(conn, s)
        assert (p.default_delivery, p.channel_kinds, p.plex_delivery, p.admin_user_id, p.return_days) == \
            ("dm", ["event"], "off", "1234", 45)
        assert db.followers(conn) == {}
