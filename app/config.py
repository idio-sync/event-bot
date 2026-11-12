"""Settings, all from environment variables (Docker/Unraid friendly)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


ALL_THEATERS = ("charles", "senator", "cinemark-towson", "parkway")
# Cinemark answers this app with a Cloudflare challenge (see docs/design.md), so it is opt-in.
DEFAULT_THEATERS = ("charles", "senator", "parkway")


@dataclass(frozen=True)
class Settings:
    data_dir: str = "/data"
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("America/New_York"))
    refresh_minutes: int = 240
    rerelease_months: int = 9
    theaters: tuple[str, ...] = DEFAULT_THEATERS
    tmdb_key: str = ""
    plex_url: str = ""
    plex_token: str = ""
    radarr_url: str = ""
    radarr_api_key: str = ""
    radarr_quality_profile: str = ""
    radarr_root_folder: str = ""
    discord_webhook: str = ""
    discord_bot_token: str = ""
    discord_alerts: tuple[str, ...] = ("watchlist", "event", "rerelease")
    alert_return_days: int = 30
    parkway_eventive_key: str = ""
    public_url: str = ""

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "showtimes.db")

    @property
    def plex_enabled(self) -> bool:
        return bool(self.plex_url and self.plex_token)

    @property
    def plex_watchlist_enabled(self) -> bool:
        return bool(self.plex_token)

    @property
    def radarr_enabled(self) -> bool:
        return bool(self.radarr_url and self.radarr_api_key)


def _alert_kinds(raw: str) -> tuple[str, ...]:
    """'watchlist,event,rerelease' (any subset; 'none' or empty turns alerts off)."""
    valid = ("watchlist", "event", "rerelease")
    wanted = {k.strip().lower() for k in raw.split(",")}
    return tuple(k for k in valid if k in wanted)


def load() -> Settings:
    theaters_raw = _env("THEATERS")
    theaters = tuple(t.strip() for t in theaters_raw.split(",") if t.strip()) if theaters_raw else DEFAULT_THEATERS
    return Settings(
        data_dir=_env("DATA_DIR", "/data"),
        tz=ZoneInfo(_env("TZ", "America/New_York") or "America/New_York"),
        refresh_minutes=max(30, _int("REFRESH_MINUTES", 240)),
        rerelease_months=max(1, _int("RERELEASE_MONTHS", 9)),
        theaters=theaters,
        tmdb_key=_env("TMDB_API_KEY"),
        plex_url=_env("PLEX_URL").rstrip("/"),
        plex_token=_env("PLEX_TOKEN"),
        radarr_url=_env("RADARR_URL").rstrip("/"),
        radarr_api_key=_env("RADARR_API_KEY"),
        radarr_quality_profile=_env("RADARR_QUALITY_PROFILE"),
        radarr_root_folder=_env("RADARR_ROOT_FOLDER"),
        discord_webhook=_env("DISCORD_WEBHOOK_URL"),
        discord_bot_token=_env("DISCORD_BOT_TOKEN"),
        discord_alerts=_alert_kinds(_env("DISCORD_ALERTS", "watchlist,event,rerelease")),
        alert_return_days=max(1, _int("ALERT_RETURN_DAYS", 30)),
        parkway_eventive_key=_env("PARKWAY_EVENTIVE_KEY"),
        public_url=_env("PUBLIC_URL").rstrip("/"),
    )
