"""Alert settings edited on the web Settings page (stored in the DB; env vars are the defaults)."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

from app import db
from app.config import Settings

DELIVERIES = ("channel", "dm")
PLEX_MODES = ("dm", "channel", "off")
CHANNEL_KINDS = ("event", "rerelease")


@dataclass
class Prefs:
    default_delivery: str = "channel"  # how followers hear about films: channel @mention or DM
    channel_id: str = ""  # bot posts here (unused in webhook mode)
    channel_kinds: list[str] = field(default_factory=lambda: list(CHANNEL_KINDS))  # general feed
    plex_delivery: str = "dm"  # the admin's Plex watchlist alerts
    admin_user_id: str = ""  # Discord user id of the server owner/admin
    return_days: int = 30

    @classmethod
    def defaults(cls, settings: Settings) -> Prefs:
        kinds = settings.discord_alerts
        return cls(
            channel_kinds=[k for k in CHANNEL_KINDS if k in kinds],
            plex_delivery="dm" if "watchlist" in kinds else "off",
            return_days=settings.alert_return_days,
        )

    def cleaned(self) -> Prefs:
        return Prefs(
            default_delivery=self.default_delivery if self.default_delivery in DELIVERIES else "channel",
            channel_id="".join(ch for ch in str(self.channel_id) if ch.isdigit()),
            channel_kinds=[k for k in CHANNEL_KINDS if k in self.channel_kinds],
            plex_delivery=self.plex_delivery if self.plex_delivery in PLEX_MODES else "dm",
            admin_user_id="".join(ch for ch in str(self.admin_user_id) if ch.isdigit()),
            return_days=min(max(int(self.return_days or 30), 1), 365),
        )


def load(conn: sqlite3.Connection, settings: Settings) -> Prefs:
    base = asdict(Prefs.defaults(settings))
    stored: dict[str, Any] = db.kv_get(conn, "prefs", {})
    base.update({k: v for k, v in stored.items() if k in base})
    return Prefs(**base).cleaned()


def save(conn: sqlite3.Connection, prefs: Prefs) -> None:
    db.kv_set(conn, "prefs", asdict(prefs.cleaned()))
