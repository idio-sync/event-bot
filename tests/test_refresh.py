import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import db, models, refresh
from app.config import Settings
from app.models import Showing


@pytest.fixture
def conn(tmp_path):
    with contextlib.closing(db.connect(str(tmp_path / 'showtimes.db'))) as connection:
        db.init(connection)
        yield connection


def showing(source_id, days=1):
    return Showing('parkway', source_id, 'Film', datetime.now(UTC) + timedelta(days=days))


def active(conn):
    return {row['key']: row['active'] for row in conn.execute('SELECT key, active FROM showings')}


def result(shows, **kwargs):
    cls = getattr(models, 'FetchResult', None)
    assert cls is not None, 'FetchResult must express schedule completeness'
    return cls(shows, **kwargs)


def run_fetch(monkeypatch, conn, fetched):
    async def fetch(http):
        if isinstance(fetched, Exception):
            raise fetched
        return fetched
    monkeypatch.setattr(refresh, 'fetchers', lambda settings: {'parkway': fetch})
    async def run():
        async with httpx.AsyncClient() as client:
            await refresh._theaters(Settings(theaters=('parkway',)), conn, client)
    asyncio.run(run())


def test_partial_database_snapshot_preserves_missing_future_shows(conn):
    db.save_showings(conn, 'parkway', [showing('a'), showing('b', 60)])
    db.save_showings(conn, 'parkway', [showing('a')], complete=False)
    assert active(conn) == {'parkway:a': 1, 'parkway:b': 1}


def test_partial_refresh_preserves_missing_and_records_warning(monkeypatch, conn):
    db.save_showings(conn, 'parkway', [showing('a'), showing('b', 60)])
    run_fetch(monkeypatch, conn, result([showing('a')], warning='Partial schedule'))
    assert active(conn) == {'parkway:a': 1, 'parkway:b': 1}
    status = db.sources(conn)['parkway']
    assert status['last_success']
    assert status['last_count'] == 1
    assert status['last_error'] == 'Partial schedule'


def test_complete_empty_refresh_removes_cancelled_future_shows(monkeypatch, conn):
    db.save_showings(conn, 'parkway', [showing('future'), showing('past', -1)])
    run_fetch(monkeypatch, conn, result([], complete=True))
    assert active(conn) == {'parkway:future': 0, 'parkway:past': 1}
    assert db.sources(conn)['parkway']['last_count'] == 0


def test_partial_empty_refresh_does_not_claim_success(monkeypatch, conn):
    db.save_showings(conn, 'parkway', [showing('a')])
    run_fetch(monkeypatch, conn, result([]))
    assert active(conn)['parkway:a'] == 1
    assert db.sources(conn)['parkway']['last_success'] is None


def test_legacy_complete_refresh_still_removes_missing(monkeypatch, conn):
    db.save_showings(conn, 'parkway', [showing('a'), showing('b')])
    run_fetch(monkeypatch, conn, [showing('a')])
    assert active(conn) == {'parkway:a': 1, 'parkway:b': 0}


def test_failed_fetch_preserves_data_and_redacts_key(monkeypatch, conn, caplog):
    db.save_showings(conn, 'parkway', [showing('a')])
    request = httpx.Request('GET', 'https://api.eventive.org/events?api_key=super-secret')
    response = httpx.Response(403, request=request)
    error = httpx.HTTPStatusError('Failed at https://api.eventive.org/events?api_key=super-secret', request=request, response=response)
    run_fetch(monkeypatch, conn, error)
    assert active(conn)['parkway:a'] == 1
    status = db.sources(conn)['parkway']
    assert '403' in status['last_error']
    assert 'super-secret' not in status['last_error']
    assert 'super-secret' not in caplog.text


def test_unknown_exception_does_not_expose_credentials(monkeypatch, conn, caplog):
    run_fetch(monkeypatch, conn, ValueError('bad input api_key=secret-text'))
    assert 'secret-text' not in db.sources(conn)['parkway']['last_error']
    assert 'secret-text' not in caplog.text


def test_transport_http_status_remains_visible_without_urls(monkeypatch, conn):
    from app.sources import transport
    cls = getattr(transport, 'SourceHTTPError', None)
    assert cls is not None, 'Transport must preserve sanitized HTTP status'
    run_fetch(monkeypatch, conn, cls(403))
    assert '403' in db.sources(conn)['parkway']['last_error']


def test_warehouse_showings_render_in_web_and_discord(tmp_path):
    from fastapi.testclient import TestClient
    from app import commands
    from app.main import create_app
    from app.tmdb import film_from_row
    settings = Settings(data_dir=str(tmp_path), theaters=('warehouse-rotunda',))
    with contextlib.closing(db.connect(settings.db_path)) as connection:
        db.init(connection)
        db.save_showings(connection, 'warehouse-rotunda', [
            Showing('warehouse-rotunda', '42', 'Wicker', datetime.now(UTC) + timedelta(days=1))
        ], complete=False)
        for row in db.unmatched(connection):
            db.save_match(connection, row['match_key'], film_from_row(row))
        reply = commands.showtimes(connection, settings, 'warehouse-rotunda', 'week')
        assert 'Wicker' in reply.embeds[0]['description']
        assert 'Warehouse' in str(reply.embeds)
    with TestClient(create_app(settings, run_loop=False)) as client:
        page = client.get('/')
        assert page.status_code == 200
        assert 'Wicker' in page.text
        assert 'warehouse-rotunda' in page.text
        assert 'Warehouse Cinemas Rotunda' in client.get('/status').text
