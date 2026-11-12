"""Discord bot: slash commands and alert delivery (channel posts + DMs).

Connects out to Discord's gateway, so it works behind NAT with no public URL.
The command logic lives in app.commands; this module only adapts it to discord.py.
(No `from __future__ import annotations` here: discord.py reads parameter annotations
to build the slash-command options.)
"""

import asyncio
import contextlib
import logging
from typing import Any, Optional

import discord
import httpx
from discord import app_commands

from app import commands, db, prefs as prefs_mod, refresh
from app.config import Settings
from app.integrations.discord import chunks
from app.sources import THEATERS, fetchers

log = logging.getLogger(__name__)

# Send Messages (2048) + Embed Links (16384)
INVITE_PERMISSIONS = 18432


class ShowtimesBot(discord.Client):
    def __init__(self, settings: Settings):
        super().__init__(intents=discord.Intents.default())
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.web: Optional[httpx.AsyncClient] = None
        self.owner_id_: Optional[int] = None
        self.last_error: Optional[str] = None
        _register(self)

    # -- lifecycle ---------------------------------------------------------------

    async def setup_hook(self) -> None:
        self.web = refresh.client()
        info = await self.application_info()
        self.owner_id_ = info.team.owner_id if info.team else info.owner.id

    async def on_ready(self) -> None:
        log.info("Discord bot connected as %s in %d server(s)", self.user, len(self.guilds))
        for guild in self.guilds:
            await self._sync(guild)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._sync(guild)

    async def _sync(self, guild: discord.Guild) -> None:
        # Per-server sync makes commands show up immediately (global sync can take an hour).
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        except discord.HTTPException:
            log.exception("Syncing slash commands to %s failed", guild.name)

    async def close(self) -> None:
        if self.web:
            await self.web.aclose()
        await super().close()

    # -- helpers used by the web app and alerts -------------------------------------

    def conn(self):
        return contextlib.closing(db.connect(self.settings.db_path))

    def prefs(self) -> prefs_mod.Prefs:
        with self.conn() as c:
            return prefs_mod.load(c, self.settings)

    def is_admin(self, user_id: int) -> bool:
        admin = self.prefs().admin_user_id
        return str(user_id) == admin if admin else user_id == self.owner_id_

    def invite_url(self) -> Optional[str]:
        if not self.application_id:
            return None
        return (f"https://discord.com/oauth2/authorize?client_id={self.application_id}"
                f"&scope=bot+applications.commands&permissions={INVITE_PERMISSIONS}")

    def text_channels(self) -> list[tuple[str, str]]:
        out = []
        for guild in self.guilds:
            me = guild.me
            for ch in guild.text_channels:
                if ch.permissions_for(me).send_messages:
                    out.append((str(ch.id), f"#{ch.name} ({guild.name})"))
        return out

    def notifier(self) -> "BotNotifier":
        return BotNotifier(self)


class BotNotifier:
    can_dm = True

    def __init__(self, bot: ShowtimesBot):
        self.bot = bot

    async def channel(self, content: str, embeds: list[dict[str, Any]], mentions: list[str]) -> None:
        channel_id = self.bot.prefs().channel_id
        if not channel_id:
            raise RuntimeError("No alerts channel is set on the Settings page")
        channel = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))
        allowed = discord.AllowedMentions(everyone=False, roles=False,
                                          users=[discord.Object(int(u)) for u in mentions])
        for i, batch in enumerate(chunks(embeds)):
            await channel.send(content[:2000] if i == 0 else None,
                               embeds=[discord.Embed.from_dict(e) for e in batch], allowed_mentions=allowed)

    async def dm(self, user_id: str, content: str, embeds: list[dict[str, Any]]) -> None:
        user = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
        for i, batch in enumerate(chunks(embeds)):
            await user.send(content if i == 0 else None, embeds=[discord.Embed.from_dict(e) for e in batch])


async def _reply(interaction: discord.Interaction, reply: commands.Reply) -> None:
    embeds = [discord.Embed.from_dict(e) for e in reply.embeds]
    kwargs = {"embeds": embeds, "ephemeral": reply.ephemeral, "allowed_mentions": discord.AllowedMentions.none()}
    if interaction.response.is_done():
        await interaction.followup.send(reply.content or None, **kwargs)
    else:
        await interaction.response.send_message(reply.content or None, **kwargs)


def _choices(pairs: list[tuple[str, str]]) -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=name[:100], value=value[:100]) for name, value in pairs]


def _register(bot: ShowtimesBot) -> None:
    settings = bot.settings
    tree = bot.tree
    enabled = [t for t, fetch in fetchers(settings).items() if fetch]
    theater_choices = [app_commands.Choice(name=THEATERS[t].name, value=t) for t in enabled]
    when_choices = [app_commands.Choice(name=label, value=key) for key, label in commands.WHEN.items()]

    def name_of(interaction: discord.Interaction) -> str:
        return interaction.user.display_name

    # Read-only commands — replies are public so friends can see what's on.

    @tree.command(name="showtimes", description="What's playing, by theater and day")
    @app_commands.describe(theater="Which theater (default: all)", when="When (default: next 7 days)")
    @app_commands.choices(theater=theater_choices, when=when_choices)
    async def showtimes_cmd(interaction: discord.Interaction,
                            theater: Optional[app_commands.Choice[str]] = None,
                            when: Optional[app_commands.Choice[str]] = None):
        with bot.conn() as c:
            reply = commands.showtimes(c, settings, theater.value if theater else None,
                                       when.value if when else "week")
        await _reply(interaction, reply)

    @tree.command(name="film", description="Every showtime for one film")
    @app_commands.describe(title="Start typing a title")
    async def film_cmd(interaction: discord.Interaction, title: str):
        with bot.conn() as c:
            reply = commands.film(c, settings, title)
        await _reply(interaction, reply)

    @film_cmd.autocomplete("title")
    async def film_ac(interaction: discord.Interaction, current: str):
        with bot.conn() as c:
            return _choices(commands.autocomplete_playing(c, settings, current))

    async def by_kind(interaction: discord.Interaction, kind: str,
                      theater: Optional[app_commands.Choice[str]]) -> None:
        with bot.conn() as c:
            reply = commands.by_kind(c, settings, kind, theater.value if theater else None)
        await _reply(interaction, reply)

    @tree.command(name="events", description="Upcoming special events (Q&As, Met Opera, festivals, …)")
    @app_commands.describe(theater="Which theater (default: all)")
    @app_commands.choices(theater=theater_choices)
    async def events_cmd(interaction: discord.Interaction, theater: Optional[app_commands.Choice[str]] = None):
        await by_kind(interaction, "event", theater)

    @tree.command(name="rereleases", description="Upcoming re-releases and revival screenings")
    @app_commands.describe(theater="Which theater (default: all)")
    @app_commands.choices(theater=theater_choices)
    async def rereleases_cmd(interaction: discord.Interaction, theater: Optional[app_commands.Choice[str]] = None):
        await by_kind(interaction, "rerelease", theater)

    @tree.command(name="coming", description="Films that haven't started showing yet")
    @app_commands.describe(theater="Which theater (default: all)")
    @app_commands.choices(theater=theater_choices)
    async def coming_cmd(interaction: discord.Interaction, theater: Optional[app_commands.Choice[str]] = None):
        await by_kind(interaction, "coming", theater)

    # Personal commands — replies only the user sees.

    @tree.command(name="follow", description="Get an alert when a film plays at one of the theaters")
    @app_commands.describe(title="Start typing a title — films not showing yet work too")
    async def follow_cmd(interaction: discord.Interaction, title: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        with bot.conn() as c:
            reply = await commands.follow(c, settings, bot.web, str(interaction.user.id), name_of(interaction), title)
        await _reply(interaction, reply)

    @follow_cmd.autocomplete("title")
    async def follow_ac(interaction: discord.Interaction, current: str):
        with bot.conn() as c:
            return _choices(await commands.autocomplete_follow(c, settings, bot.web, current))

    @tree.command(name="unfollow", description="Stop following a film")
    async def unfollow_cmd(interaction: discord.Interaction, title: str):
        with bot.conn() as c:
            reply = commands.unfollow(c, str(interaction.user.id), title)
        await _reply(interaction, reply)

    @unfollow_cmd.autocomplete("title")
    async def unfollow_ac(interaction: discord.Interaction, current: str):
        with bot.conn() as c:
            return _choices(commands.autocomplete_following(c, str(interaction.user.id), current))

    @tree.command(name="following", description="Films you follow and when they're next on")
    async def following_cmd(interaction: discord.Interaction):
        with bot.conn() as c:
            reply = commands.following(c, settings, str(interaction.user.id))
        await _reply(interaction, reply)

    @tree.command(name="alerts", description="How you get alerts for films you follow")
    @app_commands.describe(delivery="Channel @mention, DM, or the server default")
    @app_commands.choices(delivery=[
        app_commands.Choice(name="Mention me in the alerts channel", value="channel"),
        app_commands.Choice(name="Send me a DM", value="dm"),
        app_commands.Choice(name="Server default", value="default"),
    ])
    async def alerts_cmd(interaction: discord.Interaction, delivery: app_commands.Choice[str]):
        with bot.conn() as c:
            default = prefs_mod.load(c, settings).default_delivery
            reply = commands.set_alerts(c, settings, str(interaction.user.id), name_of(interaction),
                                        delivery.value, default)
        await _reply(interaction, reply)

    # Admin.

    @tree.command(name="refresh", description="Fetch showtimes now (admin only)")
    async def refresh_cmd(interaction: discord.Interaction):
        if not bot.is_admin(interaction.user.id):
            await _reply(interaction, commands.Reply("Only the server admin can do that.", ephemeral=True))
            return
        asyncio.create_task(refresh.run(settings, bot))
        await _reply(interaction, commands.Reply("Refreshing — alerts (if any) follow in a minute.", ephemeral=True))


async def run(bot: ShowtimesBot, token: str) -> None:
    """Keep the bot connected; record a login failure instead of crashing the web app."""
    try:
        await bot.start(token)
    except discord.LoginFailure as exc:
        bot.last_error = f"Login failed: {exc}"
        log.error("Discord bot login failed — check DISCORD_BOT_TOKEN")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        bot.last_error = f"{type(exc).__name__}: {exc}"
        log.exception("Discord bot stopped")
