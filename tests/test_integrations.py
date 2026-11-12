"""TMDB, Plex, Radarr and Discord code against fake HTTP servers (httpx.MockTransport)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app import db, refresh, tmdb
from app.config import Settings
from app.integrations import plex, radarr
from app.models import Showing

TZ = ZoneInfo("America/New_York")


def run(coro):
    return asyncio.run(coro)


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---- TMDB -------------------------------------------------------------------

def test_pick_best_prefers_exact_title_and_close_year():
    results = [
        {"id": 1, "title": "Birds of Prey", "release_date": "2020-02-07", "popularity": 40},
        {"id": 2, "title": "The Birds", "release_date": "1963-03-28", "popularity": 20},
        {"id": 3, "title": "Birds", "release_date": "2024-11-08", "popularity": 5},
    ]
    assert tmdb.pick_best("Birds", 1963, results) == 2
    assert tmdb.pick_best("Nothing Like It", None, results) is None


def test_resolve_uses_ids_imdb_and_search(tmp_path):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        assert req.url.params.get("api_key") == "k"
        if req.url.path == "/3/find/tt0056869":
            return httpx.Response(200, json={"movie_results": [{"id": 571}]})
        if req.url.path == "/3/search/movie":
            assert req.url.params["query"] == "Wild Horse Nine"
            return httpx.Response(200, json={"results": [{"id": 999, "title": "Wild Horse Nine", "release_date": "2026-11-06"}]})
        movie_id = int(req.url.path.rsplit("/", 1)[1])
        titles = {571: ("The Birds", "1963-03-28"), 426: ("Vertigo", "1958-05-09"), 999: ("Wild Horse Nine", "2026-11-06")}
        title, rel = titles[movie_id]
        return httpx.Response(200, json={"id": movie_id, "title": title, "release_date": rel, "poster_path": "/p.jpg",
                                         "external_ids": {"imdb_id": "tt1"}, "runtime": 100})

    path = str(tmp_path / "t.db")
    with contextlib.closing(db.connect(path)) as conn:
        db.init(conn)
        start = datetime(2026, 11, 20, 19, tzinfo=TZ)
        db.save_showings(conn, "senator", [
            Showing("senator", "1", "BIRDS", start, imdb_id="tt0056869", release_date=date(1963, 3, 28)),
            Showing("senator", "2", "WILD HORSE NINE", start, release_date=date(2026, 11, 6)),
            Showing("senator", "3", "VERTIGO", start, tmdb_id=426),
        ])

        async def go():
            async with client(handler) as http:
                return await tmdb.resolve(conn, http, "k")

        assert run(go()) == 3
        films = {r["film_key"]: r["title"] for r in conn.execute("SELECT film_key, title FROM films")}
        assert films == {"tmdb:571": "The Birds", "tmdb:999": "Wild Horse Nine", "tmdb:426": "Vertigo"}
        assert conn.execute("SELECT COUNT(*) FROM showings WHERE film_key IS NULL").fetchone()[0] == 0
        # Second run has nothing left to look up.
        calls.clear()
        assert run(go()) == 0 and calls == []


def test_resolve_without_key_falls_back_to_clean_title(tmp_path):
    path = str(tmp_path / "t.db")
    with contextlib.closing(db.connect(path)) as conn:
        db.init(conn)
        db.save_showings(conn, "charles", [Showing("charles", "1", "MET OPERA: OTELLO", datetime(2026, 12, 1, 13, tzinfo=TZ))])

        async def go():
            async with client(lambda r: httpx.Response(500)) as http:
                return await tmdb.resolve(conn, http, "")

        assert run(go()) == 0
        assert conn.execute("SELECT title FROM films").fetchone()[0] == "Otello"


# ---- Plex -------------------------------------------------------------------

def test_plex_library_maps_tmdb_guids_and_watched():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["X-Plex-Token"] == "tok"
        if req.url.path == "/identity":
            return httpx.Response(200, json={"MediaContainer": {"machineIdentifier": "m1"}})
        if req.url.path == "/library/sections":
            return httpx.Response(200, json={"MediaContainer": {"Directory": [
                {"key": "1", "type": "movie"}, {"key": "2", "type": "show"}]}})
        assert req.url.path == "/library/sections/1/all"
        return httpx.Response(200, json={"MediaContainer": {"Metadata": [
            {"ratingKey": 10, "viewCount": 2, "Guid": [{"id": "imdb://tt0052357"}, {"id": "tmdb://426"}]},
            {"ratingKey": 11, "Guid": [{"id": "tmdb://571"}]},
            {"ratingKey": 12, "Guid": []},
        ]}})

    async def go():
        async with client(handler) as http:
            return await plex.library(http, "http://plex:32400", "tok")

    lib = run(go())
    assert lib["machine"] == "m1"
    assert lib["movies"] == {"426": {"watched": True, "rating_key": "10"}, "571": {"watched": False, "rating_key": "11"}}


def test_plex_watchlist_pages_and_looks_up_missing_guids():
    lookups = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/library/sections/watchlist/all":
            start = int(req.url.params["X-Plex-Container-Start"])
            items = [
                {"ratingKey": "a", "type": "movie", "title": "Vertigo", "year": 1958, "Guid": [{"id": "tmdb://426"}]},
                {"ratingKey": "b", "type": "show", "title": "Twin Peaks"},
            ] if start == 0 else [{"ratingKey": "c", "type": "movie", "title": "The Birds", "year": 1963}]
            return httpx.Response(200, json={"MediaContainer": {"totalSize": 3, "Metadata": items}})
        lookups.append(req.url.path)
        assert req.url.path == "/library/metadata/c"
        return httpx.Response(200, json={"MediaContainer": {"Metadata": [{"Guid": [{"id": "tmdb://571"}]}]}})

    cache: dict[str, int] = {}

    async def go():
        async with client(handler) as http:
            return await plex.watchlist(http, "tok", cache)

    wl = run(go())
    assert set(wl) == {"426", "571"}
    assert cache == {"a": 426, "c": 571}
    run(go())
    assert lookups == ["/library/metadata/c"]  # cached the second time


# ---- Radarr -----------------------------------------------------------------

def test_radarr_movies_and_add():
    posted = {}

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["X-Api-Key"] == "rk"
        path = req.url.path
        if path == "/api/v3/movie" and req.method == "GET":
            return httpx.Response(200, json=[{"tmdbId": 426, "monitored": True, "hasFile": False,
                                              "digitalRelease": "2027-01-05T00:00:00Z", "titleSlug": "vertigo-426"}])
        if path == "/api/v3/movie/lookup/tmdb":
            return httpx.Response(200, json={"tmdbId": 571, "title": "The Birds", "images": []})
        if path == "/api/v3/qualityprofile":
            return httpx.Response(200, json=[{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}])
        if path == "/api/v3/rootfolder":
            return httpx.Response(200, json=[{"path": "/movies"}])
        if path == "/api/v3/movie" and req.method == "POST":
            posted.update(json.loads(req.content))
            return httpx.Response(201, json={**posted, "titleSlug": "the-birds-571"})
        return httpx.Response(404)

    async def go():
        async with client(handler) as http:
            lib = await radarr.movies(http, "http://radarr:7878", "rk")
            added = await radarr.add(http, "http://radarr:7878", "rk", 571, quality_profile="hd-1080p")
            return lib, added

    lib, added = run(go())
    assert lib["426"]["digital"] == "2027-01-05" and lib["426"]["link"] == "http://radarr:7878/movie/vertigo-426"
    assert posted["qualityProfileId"] == 4 and posted["rootFolderPath"] == "/movies"
    assert posted["monitored"] and posted["addOptions"] == {"searchForMovie": True}
    assert added["titleSlug"] == "the-birds-571"
