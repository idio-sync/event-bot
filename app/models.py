from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class Showing:
    """One screening as reported by a theater source."""

    theater: str
    source_id: str  # unique within the theater
    title: str  # raw title as the theater lists it
    start: datetime  # timezone-aware
    match_title: str | None = None  # the film's own title when `title` is an event name
    tmdb_id: int | None = None
    imdb_id: str | None = None
    release_date: date | None = None  # the source's idea of the release date
    tags: list[str] = field(default_factory=list)  # formats, attributes, event tags
    is_event: bool = False  # the source itself marks this as an event
    ticket_url: str | None = None
    runtime_min: int | None = None
    synopsis: str | None = None
    poster_url: str | None = None
    sold_out: bool = False

    @property
    def key(self) -> str:
        return f"{self.theater}:{self.source_id}"


@dataclass(frozen=True)
class FetchResult:
    """A source snapshot; only complete snapshots can cancel missing showings.

    Partial results upsert their observed screenings while retaining all other
    cached screenings. Warnings describe coverage limits on the status page.
    """

    showings: list[Showing]
    complete: bool = False
    warning: str | None = None


@dataclass(frozen=True)
class Theater:
    id: str
    name: str
    short: str
    address: str
    url: str
