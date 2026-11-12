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
