"""Theater registry: which source feeds which theater."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

from app.config import Settings
from app.models import Showing, Theater
from app.sources import cinemark, eventive, rts

THEATERS: dict[str, Theater] = {
    t.id: t
    for t in [
        Theater("charles", "The Charles Theatre", "Charles", "1711 N. Charles St", "https://thecharles.com/"),
        Theater("senator", "The Senator Theatre", "Senator", "5904 York Rd", "https://thesenatortheatre.com/"),
        Theater(
            "cinemark-towson",
            "Cinemark Towson and XD",
            "Cinemark",
            "111 E. Joppa Rd, Towson",
            "https://www.cinemark.com/theatres/md-towson/cinemark-towson-and-xd",
        ),
        Theater("parkway", "SNF Parkway Theatre", "Parkway", "5 W. North Ave", "https://snfparkway.org/"),
    ]
}

Fetcher = Callable[[httpx.AsyncClient], Awaitable[list[Showing]]]


def fetchers(settings: Settings) -> dict[str, Fetcher | None]:
    """Fetcher per enabled theater; None means enabled but not configured."""
    tz = settings.tz
    table: dict[str, Fetcher | None] = {
        "charles": lambda c: rts.fetch(c, "charles", "467271", tz),
        "senator": lambda c: rts.fetch(c, "senator", "997115", tz),
        "cinemark-towson": lambda c: cinemark.fetch(c, "cinemark-towson", 1072, tz),
        "parkway": (lambda c: eventive.fetch(c, "parkway", settings.parkway_eventive_key))
        if settings.parkway_eventive_key
        else None,
    }
    return {tid: table[tid] for tid in settings.theaters if tid in table}
