"""Match theater titles to TMDB films (and fall back to cleaned titles without a key)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from typing import Any

import httpx

from app import db
from app.classify import clean_title, normalize_key

log = logging.getLogger(__name__)

API = "https://api.themoviedb.org/3"
POSTER = "https://image.tmdb.org/t/p/w342{path}"


class Tmdb:
    def __init__(self, client: httpx.AsyncClient, key: str):
        self.client = client
        # v4 "read access tokens" are JWTs; v3 keys are 32 hex chars.
        if key.startswith("eyJ"):
            self.headers = {"Authorization": f"Bearer {key}"}
            self.params: dict[str, str] = {}
        else:
            self.headers = {}
            self.params = {"api_key": key}

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        resp = await self.client.get(f"{API}{path}", params={**self.params, **params}, headers=self.headers)
        resp.raise_for_status()
        return resp.json()

    async def movie(self, tmdb_id: int) -> dict[str, Any]:
        return await self._get(f"/movie/{tmdb_id}", append_to_response="external_ids")

    async def find_imdb(self, imdb_id: str) -> int | None:
        data = await self._get(f"/find/{imdb_id}", external_source="imdb_id")
        results = data.get("movie_results") or []
        return results[0]["id"] if results else None

    async def search(self, title: str, year: int | None) -> int | None:
        attempts = [{"query": title, "year": year}] if year else []
        attempts.append({"query": title})
        for params in attempts:
            data = await self._get("/search/movie", include_adult="false", **{k: v for k, v in params.items() if v})
            best = pick_best(title, year, data.get("results") or [])
            if best:
                return best
        return None


def pick_best(title: str, year: int | None, results: list[dict[str, Any]]) -> int | None:
    want = normalize_key(title)
    best_id, best_score = None, float("-inf")
    for r in results[:10]:
        names = {normalize_key(r.get("title") or ""), normalize_key(r.get("original_title") or "")}
        score = 0.0
        if want in names:
            score += 100
        elif any(want and n and (want in n or n in want) for n in names):
            score += 40
        else:
            continue
        r_year = int(r["release_date"][:4]) if (r.get("release_date") or "")[:4].isdigit() else None
        if year and r_year:
            # Cinemark lists re-releases with the re-release date, so a big gap is a
            # penalty, not a disqualifier.
            score -= min(abs(year - r_year), 30) * 2
        score += min(float(r.get("popularity") or 0), 50) / 10
        if score > best_score:
            best_id, best_score = r["id"], score
    return best_id


def film_from_tmdb(movie: dict[str, Any]) -> dict[str, Any]:
    return {
        "film_key": f"tmdb:{movie['id']}",
        "tmdb_id": movie["id"],
        "imdb_id": (movie.get("external_ids") or {}).get("imdb_id") or movie.get("imdb_id"),
        "title": movie.get("title") or movie.get("original_title"),
        "release_date": movie.get("release_date") or None,
        "poster_url": POSTER.format(path=movie["poster_path"]) if movie.get("poster_path") else None,
        "overview": movie.get("overview") or None,
        "runtime_min": movie.get("runtime") or None,
    }


def film_from_row(row: sqlite3.Row) -> dict[str, Any]:
    title = clean_title(row["match_title"] or row["raw_title"])
    return {
        "film_key": f"title:{normalize_key(title)}",
        "tmdb_id": None,
        "imdb_id": None,
        "title": title,
        "release_date": row["source_release"],
        "poster_url": row["poster_url"],
        "overview": row["synopsis"],
        "runtime_min": row["runtime_min"],
    }


async def resolve(conn: sqlite3.Connection, client: httpx.AsyncClient, key: str) -> int:
    """Match every active, unmatched title. Returns how many were matched to TMDB."""
    tmdb = Tmdb(client, key) if key else None
    matched = 0
    for row in db.unmatched(conn):
        match_key: str = row["match_key"]
        film = None
        if tmdb:
            try:
                tmdb_id = await _tmdb_id_for(tmdb, row)
                if tmdb_id:
                    film = film_from_tmdb(await tmdb.movie(tmdb_id))
                    matched += 1
            except httpx.HTTPError as exc:
                log.warning("TMDB lookup failed for %s: %s", row["raw_title"], exc)
                continue  # try again next refresh rather than caching a title-only match
        if film is None:
            film = film_from_row(row)
        if not film.get("poster_url") and row["poster_url"]:
            film["poster_url"] = row["poster_url"]
        db.save_match(conn, match_key, film)
    return matched


async def _tmdb_id_for(tmdb: Tmdb, row: sqlite3.Row) -> int | None:
    kind, _, rest = row["match_key"].partition(":")
    if kind == "tmdb":
        return int(rest)
    if kind == "imdb":
        found = await tmdb.find_imdb(rest)
        if found:
            return found
    year = date.fromisoformat(row["source_release"]).year if row["source_release"] else None
    return await tmdb.search(clean_title(row["match_title"] or row["raw_title"]), year)
