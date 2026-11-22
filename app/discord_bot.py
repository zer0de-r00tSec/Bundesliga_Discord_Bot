"""Discord-Bot: Intents, Command-Sync, Willkommensnachricht, Ticker-Start."""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from app.channels import ChannelManager
from app import sprueche
from app.config import settings
from app.openligadb_client import OpenLigaDBClient
from app.publisher import Publisher
from app.ticker import Ticker

logger = logging.getLogger(__name__)


class StadionBot(commands.Bot):
    def __init__(self, client: OpenLigaDBClient):
        intents = discord.Intents.default()
        # Members-Intent nur, wenn Willkommensnachrichten aktiv sind. Er muss im
        # Developer-Portal (Bot → Privileged Gateway Intents → Server Members) an sein,
        # sonst verweigert Discord den Login.
        intents.members = bool(settings.discord_welcome_channel_id or settings.discord_auto_role_id)
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.ol_client = client
        pub = Publisher(self)
        self.channels = ChannelManager(self, pub)
        self.ticker = Ticker(client, self, pub, channels=self.channels)
        self._ticker_task: asyncio.Task | None = None
        self.test_posts: list[tuple[int, int]] = []  # (channel_id, message_id) aller Test-Nachrichten

    async def setup_hook(self) -> None:
        from app.commands import setup as setup_commands

        await setup_commands(self)
        guild = discord.Object(id=settings.discord_guild_id)
        self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
            logger.info("%d Slash-Commands im Server %s synchronisiert.", len(synced), settings.discord_guild_id)
        except discord.Forbidden:
            logger.error(
                "Slash-Commands konnten nicht registriert werden: Bot fehlt der Scope 'applications.commands' "
                "oder DISCORD_GUILD_ID ist falsch. Bot mit Scopes 'bot applications.commands' neu einladen."
            )
        self._ticker_task = asyncio.create_task(self._run_ticker(), name="ticker")

    async def _run_ticker(self) -> None:
        try:
            await self.ticker.run()
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - run() fängt eigentlich alles ab
            logger.exception("Ticker beendet sich unerwartet")

    async def on_ready(self) -> None:
        logger.info("Eingeloggt als %s (ID %s) in %d Server(n).", self.user, self.user.id, len(self.guilds))
        if not self.get_guild(settings.discord_guild_id):
            logger.error("Bot ist NICHT im Server DISCORD_GUILD_ID=%s!", settings.discord_guild_id)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="Bundesliga ⚽"))

    async def _give_auto_role(self, member: discord.Member) -> None:
        if not settings.discord_auto_role_id or member.bot:
            return
        role = member.guild.get_role(settings.discord_auto_role_id)
        if role is None:
            logger.error("DISCORD_AUTO_ROLE_ID=%s: Rolle gibt es nicht.", settings.discord_auto_role_id)
            return
        if role in member.roles:
            return
        try:
            await member.add_roles(role, reason="Stadion-Echo: Auto-Rolle für neue Mitglieder")
            logger.info("Rolle '%s' an %s vergeben.", role.name, member)
        except discord.Forbidden:
            logger.error("Rolle '%s' nicht vergeben: Bot braucht 'Rollen verwalten' und seine Rolle muss "
                         "ÜBER '%s' stehen.", role.name, role.name)
        except discord.HTTPException as e:
            logger.warning("Rolle '%s' an %s fehlgeschlagen: %s", role.name, member, e)

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        # Server mit Regel-Bestätigung: Rolle erst, wenn die Regeln akzeptiert sind
        if before.pending and not after.pending:
            await self._give_auto_role(after)

    async def on_member_join(self, member: discord.Member) -> None:
        if not member.pending:
            await self._give_auto_role(member)
        if not settings.discord_welcome_channel_id:
            return
        channel = self.get_channel(settings.discord_welcome_channel_id)
        if not channel:
            return
        embed = discord.Embed(
            title="🏟 Neuer Fan im Stadion!",
            description=f"{sprueche.pick('willkommen', name=member.display_name)}\n"
                        f"Tipp: `/hilfe` zeigt alle Commands.",
            color=0x1A1A2E,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            logger.warning("Willkommensnachricht fehlgeschlagen: %s", e)

    async def close(self) -> None:
        if self._ticker_task:
            self._ticker_task.cancel()
        await super().close()


def create_bot(client: OpenLigaDBClient) -> StadionBot:
    return StadionBot(client)
