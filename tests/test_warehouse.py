"""Warehouse public structured screening data."""
import asyncio
import json
from datetime import datetime

import pytest

from app.sources import warehouse


def page(*objects, links=()):
    return "".join('<script type="application/ld+json">' + json.dumps(o) + "</script>" for o in objects) + "".join('<a href="' + u + '">film</a>' for u in links)


def movie():
    return {"@type": "Movie", "@id": warehouse.BASE + "/movie/wicker/#movie", "name": "Wicker", "description": "A story", "duration": "PT1H55M", "image": "https://images.example/poster.jpg"}


def event(id="123", when="2026-11-14T20:00:00-05:00"):
    url = warehouse.BASE + "/checkout/showing/wicker/" + id + "/"
    return {"@type": "ScreeningEvent", "@id": url + "#screening", "name": "Wicker", "startDate": when, "url": url, "workPresented": {"@id": movie()["@id"]}, "offers": {"availability": "https://schema.org/SoldOut"}}


def test_parse_resolves_graph_movies_dates_and_deduplicates():
    html = page({"@graph": [movie(), event(), event(), event("124", "2026-11-15T15:00:00-05:00")]})
    shows = warehouse.parse(html, "warehouse-rotunda")
    assert len(shows) == 2
    assert len({s.key for s in shows}) == 2
    assert shows[0].title == "Wicker"
    assert shows[0].start == datetime.fromisoformat("2026-11-14T20:00:00-05:00")
    assert shows[0].runtime_min == 115
    assert shows[0].synopsis == "A story"
    assert shows[0].sold_out
    assert shows[0].ticket_url.endswith("/123/")


@pytest.mark.parametrize("change", [{"startDate": "2026-11-14T20:00:00"}, {"@id": None, "url": None}, {"url": "https://evil.example/checkout/showing/wicker/123/"}])
def test_parse_rejects_invalid_screenings(change):
    obj = event()
    obj.update(change)
    with pytest.raises(ValueError):
        warehouse.parse(page(movie(), obj), "warehouse-rotunda")


def test_parse_rejects_challenge_and_malformed_schema():
    for html in ['<html>Just a moment...</html>', '<script type="application/ld+json">{</script>']:
        with pytest.raises(ValueError):
            warehouse.parse(html, "warehouse-rotunda")


def test_movie_without_published_screenings_is_valid_empty():
    assert warehouse.parse(page(movie()), "warehouse-rotunda") == []


def test_discovery_only_follows_same_origin_movie_pages():
    html = page(movie(), links=["/movie/wicker/", "/movie/wicker/#showtimes", "https://evil.example/movie/trap/", "//evil.example/movie/trap/", "/checkout/showing/wicker/123/", "/movie/x/?preferred"])
    assert warehouse.movie_urls(html) == [warehouse.BASE + "/movie/wicker/", warehouse.BASE + "/movie/x/"]


def test_fetch_combines_pages_and_reports_partial_coverage(monkeypatch):
    async def get_text(url):
        return {
            warehouse.BASE + "/home/": page(movie(), event(), links=["/movie/wicker/"]),
            warehouse.BASE + "/movie/wicker/": page(movie(), event(), event("124", "2026-11-15T15:00:00-05:00")),
        }[url]
    monkeypatch.setattr(warehouse.transport, "get_text", get_text)
    monkeypatch.setattr(warehouse, "REQUEST_GAP_SECONDS", 0)
    result = asyncio.run(warehouse.fetch(None))
    assert len(result.showings) == 2
    assert not result.complete
    assert result.warning


def test_fetch_retains_homepage_data_when_movie_page_fails(monkeypatch):
    async def get_text(url):
        if url.endswith("/home/"):
            return page(movie(), event(), links=["/movie/wicker/"])
        raise RuntimeError("unavailable sensitive-url")
    monkeypatch.setattr(warehouse.transport, "get_text", get_text)
    monkeypatch.setattr(warehouse, "REQUEST_GAP_SECONDS", 0)
    result = asyncio.run(warehouse.fetch(None))
    assert len(result.showings) == 1
    assert not result.complete
    assert "1 movie page" in result.warning
    assert "sensitive-url" not in result.warning
