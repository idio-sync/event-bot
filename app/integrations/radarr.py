"""Radarr v3 API: library status and adding films."""

from __future__ import annotations

from typing import Any

import httpx


def _h(api_key: str) -> dict[str, str]:
    return {"X-Api-Key": api_key}


async def movies(client: httpx.AsyncClient, url: str, api_key: str) -> dict[str, Any]:
    """{tmdb_id: {'monitored', 'has_file', 'digital', 'physical', 'link'}}"""
    resp = await client.get(f"{url}/api/v3/movie", headers=_h(api_key))
    resp.raise_for_status()
    out: dict[str, Any] = {}
    for m in resp.json():
        if not m.get("tmdbId"):
            continue
        out[str(m["tmdbId"])] = {
            "monitored": bool(m.get("monitored")),
            "has_file": bool(m.get("hasFile")),
            "digital": (m.get("digitalRelease") or "")[:10] or None,
            "physical": (m.get("physicalRelease") or "")[:10] or None,
            "link": f"{url}/movie/{m.get('titleSlug') or m['tmdbId']}",
        }
    return out


async def add(
    client: httpx.AsyncClient, url: str, api_key: str, tmdb_id: int, quality_profile: str = "", root_folder: str = ""
) -> dict[str, Any]:
    h = _h(api_key)
    lookup = await client.get(f"{url}/api/v3/movie/lookup/tmdb", params={"tmdbId": tmdb_id}, headers=h)
    lookup.raise_for_status()
    movie = lookup.json()

    profiles = (await client.get(f"{url}/api/v3/qualityprofile", headers=h)).raise_for_status().json()
    profile = next((p for p in profiles if p["name"].lower() == quality_profile.lower()), None) if quality_profile else None
    profile = profile or (profiles[0] if profiles else None)
    folders = (await client.get(f"{url}/api/v3/rootfolder", headers=h)).raise_for_status().json()
    folder = root_folder or (folders[0]["path"] if folders else "")
    if not profile or not folder:
        raise RuntimeError("Radarr has no quality profile or root folder to add the film with")

    body = {
        **movie,
        "qualityProfileId": profile["id"],
        "rootFolderPath": folder,
        "monitored": True,
        "minimumAvailability": "released",
        "addOptions": {"searchForMovie": True},
    }
    resp = await client.post(f"{url}/api/v3/movie", json=body, headers=h)
    resp.raise_for_status()
    return resp.json()
