from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import alerts, db, prefs as prefs_mod
from app.alerts import Audience, Run, WebhookNotifier, plan, route
from app.config import Settings
from app.models import Showing

GAP = timedelta(days=30)
T0 = datetime(2026, 12, 1, 19, tzinfo=UTC)
FEED = Audience(channel_kinds=("event", "rerelease"), plex=True)


def show(days: float, *labels: str, film: str = "tmdb:426", theater: str = "charles", event_name=None):
    return {
        "film_key": film, "theater": theater, "start": T0 + timedelta(days=days), "labels": list(labels),
        "event_name": event_name, "sold_out": False, "title": "Vertigo", "year": 1958,
        "ticket_url": "https://tickets.example/", "poster": None,
    }


def summary(found):
    return {(a.film_key, a.theater): (a.kinds, a.followers) for a in found}


# ---- plan: runs and what's due ------------------------------------------------------

def test_first_fetch_records_runs_without_announcing_old_listings():
    found, runs = plan([show(0, "rerelease"), show(3, "rerelease")], {}, FEED, GAP)
    assert found == []
    assert runs[("tmdb:426", "charles")].covered_until == T0 + timedelta(days=3)


def test_newly_listed_rerelease_and_event_alert_once():
    shows = [show(0, "rerelease", "event", "new", event_name="VERTIGO with Q&A"), show(2, "rerelease", "new")]
    found, runs = plan(shows, {}, FEED, GAP)
    assert summary(found) == {("tmdb:426", "charles"): (["event", "rerelease"], [])}
    found, _ = plan(shows, runs, FEED, GAP)
    assert found == []


def test_shows_added_to_a_running_run_stay_quiet():
    runs = {("tmdb:426", "charles"): Run(T0 + timedelta(days=3), ["rerelease"])}
    found, updated = plan([show(10, "rerelease", "new")], runs, FEED, GAP)
    assert found == []
    assert updated[("tmdb:426", "charles")].covered_until == T0 + timedelta(days=10)


def test_return_after_a_gap_alerts_again():
    runs = {("tmdb:426", "charles"): Run(T0, ["rerelease", "follow:1"])}
    aud = Audience(channel_kinds=("rerelease",), followers={"tmdb:426": ["1"]})
    found, updated = plan([show(45, "rerelease")], runs, aud, GAP)
    [alert] = found
    assert alert.kinds == ["rerelease"] and alert.followers == ["1"] and alert.returning
    assert updated[("tmdb:426", "charles")].sent == ["rerelease", "follow:1"]


def test_gap_just_under_the_limit_is_the_same_run():
    runs = {("tmdb:426", "charles"): Run(T0, ["rerelease"])}
    found, _ = plan([show(29, "rerelease", "new")], runs, FEED, GAP)
    assert found == []


def test_followers_alert_once_per_run_including_late_follows():
    aud = Audience(channel_kinds=(), followers={"tmdb:426": ["1"]})
    found, runs = plan([show(0)], {}, aud, GAP)
    assert summary(found) == {("tmdb:426", "charles"): ([], ["1"])}
    found, runs = plan([show(0)], runs, aud, GAP)
    assert found == []
    # A second person follows mid-run: only they hear about it.
    aud.followers["tmdb:426"].append("2")
    found, _ = plan([show(0)], runs, aud, GAP)
    assert summary(found) == {("tmdb:426", "charles"): ([], ["2"])}


def test_plex_watchlist_once_per_run_and_can_be_off():
    found, runs = plan([show(0, "watchlist")], {}, FEED, GAP)
    assert summary(found) == {("tmdb:426", "charles"): (["plex"], [])}
    assert plan([show(0, "watchlist")], runs, FEED, GAP)[0] == []
    assert plan([show(0, "watchlist")], {}, Audience(plex=False), GAP)[0] == []


def test_each_theater_is_its_own_run():
    aud = Audience(followers={"tmdb:426": ["1"]})
    found, _ = plan([show(0), show(1, theater="senator")], {}, aud, GAP)
    assert set(summary(found)) == {("tmdb:426", "charles"), ("tmdb:426", "senator")}


def test_disabled_feed_kinds_are_ignored():
    found, _ = plan([show(0, "event", "rerelease", "new")], {}, Audience(channel_kinds=("rerelease",)), GAP)
    assert summary(found) == {("tmdb:426", "charles"): (["rerelease"], [])}


# ---- route: who hears where ---------------------------------------------------------

def _alert(kinds=(), followers=(), *labels):
    return alerts.Alert("tmdb:426", "charles", list(kinds), list(followers), False, [show(0, *labels)])


def test_route_default_channel_mentions_followers():
    p = prefs_mod.Prefs(default_delivery="channel", admin_user_id="9")
    r = route([_alert([], ["1", "2"])], p, {"2": "dm"}, can_dm=True)
    [card] = r.channel
    assert card.mentions == ["1"]
    assert list(r.dms) == ["2"] and "You follow this" in r.dms["2"][0].why


def test_route_dm_default_and_channel_override():
    p = prefs_mod.Prefs(default_delivery="dm")
    r = route([_alert([], ["1", "2"])], p, {"2": "channel"}, can_dm=True)
    assert [c.mentions for c in r.channel] == [["2"]]
    assert list(r.dms) == ["1"]


def test_route_webhook_mentions_everyone_in_channel():
    p = prefs_mod.Prefs(default_delivery="dm", plex_delivery="dm", admin_user_id="9")
    r = route([_alert(["plex"], ["1"])], p, {}, can_dm=False)
    [card] = r.channel
    assert card.mentions == ["1"] and "On the Plex watchlist" in card.why
    assert r.dms == {}


def test_route_plex_watchlist_dm_to_admin_only():
    p = prefs_mod.Prefs(plex_delivery="dm", admin_user_id="9")
    r = route([_alert(["plex"], [], "rerelease")], p, {}, can_dm=True)
    assert r.channel == []
    [card] = r.dms["9"]
    assert card.why == ["On your Plex watchlist", "Re-release (1958)"]


def test_route_feed_items_go_to_channel():
    p = prefs_mod.Prefs()
    r = route([_alert(["event"], [], "event")], p, {}, can_dm=True)
    assert [c.why for c in r.channel] == [["Special event"]]


# ---- send: end to end -----------------------------------------------------------------

class FakeNotifier:
    def __init__(self, can_dm=True, fail_channel=False, fail_dm=()):
        self.can_dm, self.fail_channel, self.fail_dm = can_dm, fail_channel, set(fail_dm)
        self.channel_posts: list[tuple[str, list, list]] = []
        self.dms: list[tuple[str, list]] = []

    async def channel(self, content, embeds, mentions):
        if self.fail_channel:
            raise RuntimeError("no channel")
        self.channel_posts.append((content, embeds, mentions))

    async def dm(self, user_id, content, embeds):
        if user_id in self.fail_dm:
            raise RuntimeError("DMs closed")
        self.dms.append((user_id, embeds))


@pytest.fixture
def setup(tmp_path):
    settings = Settings(data_dir=str(tmp_path), theaters=("charles",), plex_token="t", public_url="http://tower:8080")
    conn = db.connect(settings.db_path)
    db.init(conn)
    soon = datetime.now(UTC) + timedelta(days=2)
    db.save_showings(conn, "charles", [
        Showing("charles", "1", "VERTIGO", soon, tmdb_id=426),
        Showing("charles", "2", "VERTIGO", soon + timedelta(days=1), tmdb_id=426, sold_out=True),
    ])
    db.save_match(conn, "tmdb:426", {"film_key": "tmdb:426", "tmdb_id": 426, "title": "Vertigo",
                                     "release_date": "1958-05-09"})
    db.kv_set(conn, "plex_watchlist", {"426": {"title": "Vertigo"}})
    prefs_mod.save(conn, prefs_mod.Prefs(admin_user_id="9", channel_id="100"))
    db.follow(conn, "1", "tmdb:426", "Vertigo", 1958)
    db.follow(conn, "2", "tmdb:426", "Vertigo", 1958)
    db.set_delivery(conn, "2", "Sam", "dm")
    yield settings, conn
    conn.close()


def test_send_routes_channel_mentions_dms_and_plex(setup):
    settings, conn = setup
    n = FakeNotifier()
    assert asyncio.run(alerts.send(settings, conn, n)) == 3
    [(content, embeds, mentions)] = n.channel_posts
    assert mentions == ["1"] and "<@1>" in content
    assert embeds[0]["title"] == "Vertigo (1958) — The Charles Theatre"
    assert "(sold out)" in embeds[0]["description"]
    assert sorted(u for u, _ in n.dms) == ["2", "9"]
    # Nothing more next time.
    n2 = FakeNotifier()
    assert asyncio.run(alerts.send(settings, conn, n2)) == 0
    assert n2.channel_posts == [] and n2.dms == []


def test_failed_channel_post_retries_everything(setup):
    settings, conn = setup
    assert asyncio.run(alerts.send(settings, conn, FakeNotifier(fail_channel=True))) == 0
    assert db.alert_runs(conn) == {}
    assert db.kv_get(conn, "status:discord")["ok"] is False
    assert asyncio.run(alerts.send(settings, conn, FakeNotifier())) == 3


def test_closed_dms_dont_repeat_the_batch(setup):
    settings, conn = setup
    assert asyncio.run(alerts.send(settings, conn, FakeNotifier(fail_dm={"2"}))) == 2
    assert asyncio.run(alerts.send(settings, conn, FakeNotifier())) == 0


def test_webhook_notifier_posts_with_user_mentions_only(setup, monkeypatch):
    settings, conn = setup
    posted = []

    async def instant(*_):
        return None

    monkeypatch.setattr("app.integrations.discord.asyncio.sleep", instant)

    def handler(req: httpx.Request) -> httpx.Response:
        posted.append(json.loads(req.content))
        return httpx.Response(204)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await alerts.send(settings, conn, WebhookNotifier(http, "https://discord.test/hook"))

    assert asyncio.run(go()) == 1  # one channel card: followers 1 and 2 mentioned, Plex in channel
    [msg] = posted
    assert msg["allowed_mentions"] == {"parse": [], "users": ["1", "2"]}
    assert "On the Plex watchlist" in msg["embeds"][0]["description"]


def test_no_notifier_sends_nothing_and_records_nothing(setup):
    settings, conn = setup
    assert asyncio.run(alerts.send(settings, conn, None)) == 0
    assert db.alert_runs(conn) == {}
