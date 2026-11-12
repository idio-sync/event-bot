"""Discord alerts — who hears about what, once per run.

A "run" is a film's showings at one theater with no gap of `return_days` or more.
Showings added while a run continues extend it silently; showings starting a gap or
more after the run's last show begin a new run, which can alert again.

What can be due for a run (tracked in alert_runs.sent):
- 'follow:<user>': a Discord user follows the film (/follow). Once per run, also
  when they follow mid-run.
- 'plex': the film is on the admin's Plex watchlist. Once per run.
- 'event' / 'rerelease': the general channel feed. Only when a run starts with newly
  listed showings (or a film returns), so a theater's first fetch doesn't announce
  its whole existing schedule.

Routing: channel cards carry the feed items and @mention followers who get alerts
in the channel; followers who chose DMs get a DM card; Plex watchlist alerts go to
the admin by DM or to the channel, per the Settings page.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from app import db, listing, prefs as prefs_mod
from app.config import Settings
from app.integrations import discord
from app.sources import THEATERS, fetchers
from app.timefmt import stamp

log = logging.getLogger(__name__)

MAX_TIMES = 8


@dataclass
class Run:
    covered_until: datetime
    sent: list[str] = field(default_factory=list)


@dataclass
class Audience:
    channel_kinds: tuple[str, ...] = ("event", "rerelease")
    plex: bool = True  # Plex watchlist alerts on at all
    followers: dict[str, list[str]] = field(default_factory=dict)  # film_key -> user ids


@dataclass
class Alert:
    film_key: str
    theater: str
    kinds: list[str]  # subset of event, rerelease, plex
    followers: list[str]  # users due a "you follow this" alert
    returning: bool
    shows: list[dict[str, Any]]


def plan(
    shows: list[dict[str, Any]], runs: dict[tuple[str, str], Run], audience: Audience, gap: timedelta
) -> tuple[list[Alert], dict[tuple[str, str], Run]]:
    """Decide which alerts are due. Returns them and the updated runs (save only after sending)."""
    groups: OrderedDict[tuple[str, str], list[dict[str, Any]]] = OrderedDict()
    for s in sorted(shows, key=lambda s: s["start"]):
        groups.setdefault((s["film_key"], s["theater"]), []).append(s)

    alerts: list[Alert] = []
    updated: dict[tuple[str, str], Run] = {}
    for pair, group in groups.items():
        first = group[0]["start"].astimezone(UTC)
        last = group[-1]["start"].astimezone(UTC)
        run = runs.get(pair)
        starts_run = run is None or first > run.covered_until + gap
        returning = run is not None and starts_run
        run = Run(covered_until=last) if starts_run else Run(max(run.covered_until, last), list(run.sent))
        labels = {label for s in group for label in s["labels"]}

        kinds, due_followers = [], []
        if audience.plex and "watchlist" in labels and "plex" not in run.sent:
            kinds.append("plex")
        fresh = any("new" in s["labels"] for s in group) or returning
        for kind in ("event", "rerelease"):
            if kind in audience.channel_kinds and kind in labels and starts_run and fresh and kind not in run.sent:
                kinds.append(kind)
        for user in audience.followers.get(pair[0], []):
            if f"follow:{user}" not in run.sent:
                due_followers.append(user)

        if kinds or due_followers:
            run.sent.extend(kinds + [f"follow:{u}" for u in due_followers])
            alerts.append(Alert(pair[0], pair[1], kinds, due_followers, returning, group))
        updated[pair] = run
    alerts.sort(key=lambda a: (not (a.followers or "plex" in a.kinds), a.shows[0]["start"]))
    return alerts, updated


@dataclass
class Card:
    alert: Alert
    why: list[str]
    mentions: list[str] = field(default_factory=list)


@dataclass
class Routing:
    channel: list[Card] = field(default_factory=list)
    dms: dict[str, list[Card]] = field(default_factory=dict)


def route(alerts: list[Alert], p: prefs_mod.Prefs, user_delivery: dict[str, str], can_dm: bool) -> Routing:
    out = Routing()
    for a in alerts:
        base = _base_why(a)
        channel_why = [w for w in base]
        mentions = []
        if "event" in a.kinds:
            channel_why.append(_event_why(a))
        if "rerelease" in a.kinds:
            channel_why.append(_rerelease_why(a))
        for user in a.followers:
            wants_dm = user_delivery.get(user, p.default_delivery) == "dm"
            if wants_dm and can_dm:
                out.dms.setdefault(user, []).append(Card(a, base + ["You follow this"] + _extra_why(a)))
            else:
                mentions.append(user)
        if "plex" in a.kinds:
            if p.plex_delivery == "dm" and can_dm and p.admin_user_id:
                out.dms.setdefault(p.admin_user_id, []).append(Card(a, base + ["On your Plex watchlist"] + _extra_why(a)))
            elif p.plex_delivery != "off":
                channel_why.append("On the Plex watchlist")
        if mentions:
            channel_why.append("Followed")
        if len(channel_why) > len(base):
            out.channel.append(Card(a, channel_why, mentions))
    return out


def _base_why(a: Alert) -> list[str]:
    return ["Back after a break"] if a.returning else []


def _event_why(a: Alert) -> str:
    events = list(dict.fromkeys(s["event_name"] for s in a.shows if s["event_name"]))
    return "Special event" + (f": {events[0]}" if events else "")


def _rerelease_why(a: Alert) -> str:
    year = a.shows[0].get("year")
    return f"Re-release ({year})" if year else "Re-release"


def _extra_why(a: Alert) -> list[str]:
    """Context for personal cards, whether or not the feed announced it."""
    labels = {label for s in a.shows for label in s["labels"]}
    extra = []
    if "event" in labels:
        extra.append(_event_why(a))
    if "rerelease" in labels:
        extra.append(_rerelease_why(a))
    return extra


def embed(card: Card) -> dict[str, Any]:
    a = card.alert
    first = a.shows[0]
    times = [stamp(s["start"]) + (" (sold out)" if s["sold_out"] else "") for s in a.shows[:MAX_TIMES]]
    if len(a.shows) > MAX_TIMES:
        times.append(f"…and {len(a.shows) - MAX_TIMES} more")
    year = f" ({first['year']})" if first.get("year") else ""
    lines = []
    if card.why:
        lines.append("**" + " · ".join(card.why) + "**")
    if card.mentions:
        lines.append(" ".join(f"<@{u}>" for u in card.mentions))
    lines += times
    return discord.embed(
        f"{first['title']}{year} — {THEATERS[a.theater].name}", first["ticket_url"], "\n".join(lines), first["poster"]
    )


class Notifier(Protocol):
    can_dm: bool

    async def channel(self, content: str, embeds: list[dict[str, Any]], mentions: list[str]) -> None: ...

    async def dm(self, user_id: str, content: str, embeds: list[dict[str, Any]]) -> None: ...


class WebhookNotifier:
    """Channel posts through a webhook. Can't DM; followers are @mentioned instead."""

    can_dm = False

    def __init__(self, http: httpx.AsyncClient, url: str):
        self.http, self.url = http, url

    async def channel(self, content: str, embeds: list[dict[str, Any]], mentions: list[str]) -> None:
        await discord.send(self.http, self.url, content, embeds, mentions)

    async def dm(self, user_id: str, content: str, embeds: list[dict[str, Any]]) -> None:
        raise RuntimeError("Webhooks can't send DMs")


class DeliveryError(Exception):
    pass


async def send(settings: Settings, conn: sqlite3.Connection, notifier: Notifier | None) -> int:
    """Send due alerts. Returns how many cards went out."""
    if notifier is None:
        return 0
    p = prefs_mod.load(conn, settings)
    audience = Audience(
        channel_kinds=tuple(p.channel_kinds),
        plex=settings.plex_watchlist_enabled and p.plex_delivery != "off",
        followers=db.followers(conn),
    )
    enabled = [t for t, fetch in fetchers(settings).items() if fetch]
    shows = listing.load(conn, settings, listing.Filters(theaters=enabled, range="90"))
    alerts, runs = plan(shows, db.alert_runs(conn), audience, timedelta(days=p.return_days))
    routing = route(alerts, p, db.deliveries(conn), notifier.can_dm)

    if routing.channel:
        mentions = sorted({u for c in routing.channel for u in c.mentions})
        content = f"🎬 {len(routing.channel)} new showtime alert{'s' if len(routing.channel) != 1 else ''}"
        if mentions:
            content += " for " + " ".join(f"<@{u}>" for u in mentions)
        if settings.public_url:
            content += f" — {settings.public_url}/"
        try:
            await notifier.channel(content, [embed(c) for c in routing.channel], mentions)
        except Exception as exc:
            # Nothing is recorded, so everything (DMs included) is retried next refresh.
            log.exception("Posting alerts to the Discord channel failed; will retry next refresh")
            db.kv_set(conn, "status:discord", {"ok": False, "at": db.now_iso(), "error": f"{type(exc).__name__}: {exc}"[:500]})
            return 0
    sent = len(routing.channel)
    for user, cards in routing.dms.items():
        try:
            await notifier.dm(user, "🎬 Showtimes for you", [embed(c) for c in cards])
            sent += len(cards)
        except Exception:
            # Someone with DMs closed shouldn't make the whole batch repeat forever.
            log.exception("DM to Discord user %s failed; not retrying", user)
    db.save_alert_runs(conn, runs)
    if sent:
        db.kv_set(conn, "status:discord", {"ok": True, "at": db.now_iso(), "sent": sent})
    return sent
