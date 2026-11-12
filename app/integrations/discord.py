"""Discord webhook alerts."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

MAX_EMBEDS = 10
GOLD = 0xE8B04B


def embed(title: str, url: str | None, description: str, thumbnail: str | None) -> dict[str, Any]:
    e: dict[str, Any] = {"title": title[:256], "description": description[:4000], "color": GOLD}
    if url:
        e["url"] = url
    if thumbnail:
        e["thumbnail"] = {"url": thumbnail}
    return e


async def send(client: httpx.AsyncClient, webhook: str, content: str, embeds: list[dict[str, Any]]) -> None:
    for i in range(0, max(len(embeds), 1), MAX_EMBEDS):
        resp = await client.post(
            webhook,
            json={"username": "Showtimes", "content": content if i == 0 else "", "embeds": embeds[i : i + MAX_EMBEDS]},
        )
        resp.raise_for_status()
        await asyncio.sleep(1)
