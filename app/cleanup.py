"""Wöchentliches Leeren der Ticker-Channels + /aufraeumen für Admins."""
from __future__ import annotations

import datetime
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from app.commands import admin_only
from app.config import settings
from app.db import AsyncSessionLocal
from app.repositories.matches import get_live_matches

logger = logging.getLogger(__name__)

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
CLEANUP_TIME = datetime.time(hour=settings.cleanup_hour, minute=0, tzinfo=settings.tz)


async def purge_channel(channel: discord.TextChannel) -> int:
    """Löscht alle Nachrichten außer angepinnten. Gibt die Anzahl zurück."""
    deleted = await channel.purge(limit=None, check=lambda m: not m.pinned)
    return len(deleted)


class CleanupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if settings.cleanup_enabled:
            self.weekly_cleanup.start()
            logger.info(
                "Auto-Aufräumen aktiv: jeden %s um %02d:00 (%d Channel).",
                WEEKDAYS[settings.cleanup_weekday], settings.cleanup_hour, len(settings.all_ticker_channels),
            )

    def cog_unload(self) -> None:
        self.weekly_cleanup.cancel()

    @tasks.loop(time=CLEANUP_TIME)
    async def weekly_cleanup(self) -> None:
        if datetime.datetime.now(settings.tz).weekday() != settings.cleanup_weekday:
            return
        try:
            async with AsyncSessionLocal() as session:
                if await get_live_matches(session):
                    logger.info("Auto-Aufräumen übersprungen: es läuft gerade ein Spiel.")
                    return
        except Exception:
            logger.exception("Auto-Aufräumen: Live-Check fehlgeschlagen, räume trotzdem auf.")

        for cid in settings.all_ticker_channels:
            channel = self.bot.get_channel(cid)
            if not isinstance(channel, discord.TextChannel):
                logger.warning("Auto-Aufräumen: Channel %s nicht gefunden.", cid)
                continue
            try:
                n = await purge_channel(channel)
                logger.info("Auto-Aufräumen: %d Nachrichten in #%s gelöscht.", n, channel.name)
            except discord.Forbidden:
                logger.error(
                    "Auto-Aufräumen: in #%s fehlen die Rechte 'Nachrichten verwalten' / 'Nachrichtenverlauf lesen'.",
                    channel.name,
                )
            except discord.HTTPException as e:
                logger.error("Auto-Aufräumen in #%s fehlgeschlagen: %s", channel.name, e)

    @weekly_cleanup.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="aufraeumen", description="Leert diesen Channel (angepinnte Nachrichten bleiben)")
    @admin_only()
    async def aufraeumen(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("Geht nur in Text-Channels.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            n = await purge_channel(interaction.channel)
        except discord.Forbidden:
            await interaction.followup.send(
                "Mir fehlen hier die Rechte *Nachrichten verwalten* / *Nachrichtenverlauf lesen*.", ephemeral=True
            )
            return
        logger.info("/aufraeumen von %s: %d Nachrichten in #%s gelöscht.", interaction.user, n, interaction.channel.name)
        await interaction.followup.send(f"🧹 {n} Nachrichten gelöscht.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CleanupCog(bot))
