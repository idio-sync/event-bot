import json
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.sources import cinemark, eventive, rts

FIX = Path(__file__).parent / "fixtures"
TZ = ZoneInfo("America/New_York")


def test_rts_parses_shows_release_dates_and_ids():
    data = json.loads((FIX / "rts_senator.json").read_text(encoding="utf-8"))
    shows = rts.parse(data, "senator", "997115", TZ)
    titles = {s.title for s in shows}
    assert {"BIRDS", "DUNE PART THREE", "WILD HORSE NINE"} <= titles
    birds = next(s for s in shows if s.title == "BIRDS")
    assert birds.release_date == date(1963, 3, 28)
    assert birds.imdb_id == "tt0056869"
    assert birds.start.tzinfo is TZ
    assert birds.ticket_url == "https://997115.formovietickets.com/"
    assert all(s.source_id for s in shows)
    assert len({s.key for s in shows}) == len(shows)


def test_rts_reads_tmdb_link():
    data = {"location": {"Titles": [{
        "title": "WICKER", "ReleaseUS": "2026-10-23T00:00:00", "lengthsecs": 6900,
        "webSite": "www.themoviedb.org/movie/1195803-wicker?language=en-US",
        "Shows": [{"time": "2026-11-13T18:45:00", "Id": 30116, "so": True}],
    }]}}
    [show] = rts.parse(data, "charles", "467271", TZ)
    assert show.tmdb_id == 1195803
    assert show.runtime_min == 115
    assert show.sold_out
    assert show.start == datetime(2026, 11, 13, 18, 45, tzinfo=TZ)


def test_cinemark_parses_print_types_and_utc_times():
    data = json.loads((FIX / "cinemark_day.json").read_text(encoding="utf-8"))
    shows = cinemark.parse_day(data, "cinemark-towson")
    assert shows
    godzilla = [s for s in shows if s.title.startswith("Godzilla")]
    assert godzilla
    assert any("Cinemark XD" in s.tags for s in godzilla)
    assert all(s.start.tzinfo is not None and s.start.utcoffset().total_seconds() == 0 for s in shows)
    assert all(s.ticket_url.startswith("https://www.cinemark.com/movies/") for s in shows)


def test_eventive_keeps_screenings_and_drops_non_film_events():
    data = {"events": [
        {"id": "a1", "name": "BEING HEUMANN followed by post-screening discussion (OC)",
         "start_time": "2026-11-14T22:00:00.000Z", "tags": [{"name": "Film"}, {"name": "Open Captions"}],
         "films": [{"name": "Being Heumann", "details": {"year": "2026"}}]},
        {"id": "a2", "name": "EMERGENCE//1 • A Creative Tech Summit", "start_time": "2026-11-14T15:00:00.000Z",
         "tags": [{"name": "Emerging Media"}], "films": []},
        {"id": "a3", "name": "CAUGHT OUT THERE with Q&A and Panel Discussion",
         "start_time": "2026-11-13T00:00:00.000Z", "tags": [{"name": "Film"}], "films": []},
    ]}
    shows = eventive.parse(data, "parkway")
    assert [s.source_id for s in shows] == ["a1", "a3"]
    heumann = shows[0]
    assert heumann.match_title == "Being Heumann"
    assert heumann.release_date == date(2026, 1, 1)
    assert heumann.is_event
    assert heumann.start == datetime(2026, 11, 14, 22, 0, tzinfo=UTC)
    assert heumann.ticket_url.endswith("/schedule/a1")
    assert shows[1].match_title is None


def test_cinemark_normalizes_relative_posters():
    data = json.loads((FIX / "cinemark_day.json").read_text(encoding="utf-8"))
    data[0]["moviePosterUrl"] = "/poster.jpg"
    assert cinemark.parse_day(data, "cinemark-towson")[0].poster_url == "https://www.cinemark.com/poster.jpg"


def test_cinemark_rejects_missing_identity_and_naive_time():
    import pytest
    for show in [{"startTime": "2026-11-14T20:00:00Z"}, {"id": "a", "startTime": "2026-11-14T20:00:00"}]:
        with pytest.raises(ValueError):
            cinemark.parse_day([{"movieTitle": "Film", "showtimesByPrintType": [{"showtimes": [show]}]}], "cinemark-towson")


def test_eventive_rejects_unknown_envelope_and_invalid_showings():
    import pytest
    for data in [{"unexpected": []}, [{"id": None, "name": "Film", "start_time": "2026-11-14T20:00:00Z", "tags": [{"name": "Film"}]}], [{"id": "a", "name": "Film", "start_time": "2026-11-14T20:00:00", "tags": [{"name": "Film"}]}]]:
        with pytest.raises(ValueError):
            eventive.parse(data, "parkway")


def test_eventive_fetch_uses_header_auth_and_custom_bucket():
    import asyncio
    import base64
    import httpx
    def handle(request):
        assert request.url.path == "/event_buckets/custom/events"
        assert "api_key" not in request.url.params
        assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"secret:").decode()
        assert request.headers["Accept-Version"] == "~1"
        return httpx.Response(200, json={"events": []})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await eventive.fetch(client, "parkway", "secret", bucket="custom")
            assert result.showings == []
            assert not result.complete
    asyncio.run(run())


def test_cinemark_fetch_uses_transport_and_excludes_past_dates(monkeypatch):
    import asyncio
    from datetime import timedelta
    today = datetime.now(TZ).date()
    calls = []
    async def get_json(url, params=None):
        calls.append((url, params))
        if url.endswith("showtime-dates"):
            return [(today - timedelta(days=1)).isoformat(), today.isoformat(), (today + timedelta(days=366)).isoformat()]
        return []
    monkeypatch.setattr(cinemark, "get_json", get_json, raising=False)
    monkeypatch.setattr(cinemark, "REQUEST_GAP_SECONDS", 0)
    result = asyncio.run(cinemark.fetch(None, "cinemark-towson", 1072, TZ, days_ahead=365))
    assert len(calls) == 2
    assert result.showings == []
    assert not result.complete


def test_cinemark_failure_does_not_return_partial_day_batch(monkeypatch):
    import asyncio
    import pytest
    from datetime import timedelta
    today = datetime.now(TZ).date()
    tomorrow = (today + timedelta(days=1)).isoformat()
    async def get_json(url, params=None):
        if url.endswith("showtime-dates"):
            return [today.isoformat(), tomorrow]
        if params["date"] == tomorrow:
            raise RuntimeError("Source HTTP 403")
        return json.loads((FIX / "cinemark_day.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(cinemark, "get_json", get_json)
    monkeypatch.setattr(cinemark, "REQUEST_GAP_SECONDS", 0)
    with pytest.raises(RuntimeError, match="403"):
        asyncio.run(cinemark.fetch(None, "cinemark-towson", 1072, TZ))


def test_eventive_discovers_browser_key_and_rediscovers_after_rotation():
    import asyncio
    import base64
    import httpx
    keys = iter(["published-key-one", "published-key-two"])
    expected = []
    def handle(request):
        if request.url.host == "snfparkway.org":
            key = next(keys)
            expected.append(key)
            return httpx.Response(200, text=f"<script>const EventiveDataManager = {{apiKey: '{key}', bucketId: 'bucket'}};</script>")
        assert "api_key" not in request.url.params
        assert request.headers["Authorization"] == "Basic " + base64.b64encode((expected[-1] + ":").encode()).decode()
        return httpx.Response(200, json={"events": []})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            await eventive.fetch(client, "parkway", "")
            await eventive.fetch(client, "parkway", "")
    asyncio.run(run())
    assert expected == ["published-key-one", "published-key-two"]


def test_eventive_key_discovery_fails_safely_when_missing_or_ambiguous():
    import asyncio
    import httpx
    import pytest
    pages = ["<script>apiKey: dynamicallyComputed()</script>", "<script>const config = {apiKey: 'published-key-one', apiKey: 'published-key-two'};</script>"]
    for page in pages:
        def handle(request):
            assert request.url.host == "snfparkway.org"
            return httpx.Response(200, text=page)
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                with pytest.raises(ValueError) as error:
                    await eventive.fetch(client, "parkway", "")
                assert "published-key" not in str(error.value)
                assert "apiKey:" not in str(error.value)
        asyncio.run(run())


def test_parkway_registry_supports_discovery_without_configured_key():
    from app.config import Settings
    from app.sources import fetchers
    assert callable(fetchers(Settings(theaters=("parkway",)))["parkway"])
