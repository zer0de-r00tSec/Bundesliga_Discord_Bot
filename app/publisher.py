"""Senden/Editieren in Discord mit klaren Fehlermeldungen und Rechte-Check."""
from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)

REQUIRED_PERMS = ("view_channel", "send_messages", "embed_links", "attach_files")


class Publisher:
    def __init__(self, bot: discord.Client):
        self.bot = bot
        self._cache: dict[int, discord.abc.Messageable] = {}
        self.sent = 0
        self.failed = 0

    async def channel(self, channel_id: int):
        if channel_id in self._cache:
            return self._cache[channel_id]
        ch = self.bot.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(channel_id)
            except discord.NotFound:
                logger.error("Channel %s existiert nicht (falsche ID in .env?).", channel_id)
                return None
            except discord.Forbidden:
                logger.error("Kein Zugriff auf Channel %s (Bot nicht im Server oder Channel privat).", channel_id)
                return None
        self._cache[channel_id] = ch
        return ch

    async def check_permissions(self, channel_id: int) -> list[str]:
        """Gibt fehlende Rechte zurück (leere Liste = alles ok)."""
        ch = await self.channel(channel_id)
        if ch is None:
            return ["channel_not_found"]
        guild = getattr(ch, "guild", None)
        if guild is None:
            return []
        me = guild.me or await guild.fetch_member(self.bot.user.id)
        perms = ch.permissions_for(me)
        return [p for p in REQUIRED_PERMS if not getattr(perms, p, False)]

    async def send(
        self, channel_id: int, embed: discord.Embed, file: discord.File | None = None, content: str | None = None
    ) -> discord.Message | None:
        ch = await self.channel(channel_id)
        if ch is None:
            self.failed += 1
            return None
        try:
            kwargs = {"embed": embed}
            if file:
                kwargs["file"] = file
            if content:
                kwargs["content"] = content
            msg = await ch.send(**kwargs)
            self.sent += 1
            label = embed.title or (embed.description or "")[:60]
            logger.info("→ Discord %s: '%s' (msg %s, gesamt %d)", channel_id, label, msg.id, self.sent)
            return msg
        except discord.Forbidden:
            logger.error("Discord verweigert Senden in %s – Rechte prüfen: %s", channel_id, ", ".join(REQUIRED_PERMS))
        except discord.HTTPException as e:
            logger.error("Discord-Sendefehler in %s: %s", channel_id, e)
        self.failed += 1
        return None

    async def edit(self, channel_id: int, message_id: int, embed: discord.Embed) -> bool:
        ch = await self.channel(channel_id)
        if ch is None:
            return False
        try:
            msg = ch.get_partial_message(message_id)
            await msg.edit(embed=embed)
            logger.debug("✎ Discord %s: msg %s editiert ('%s')", channel_id, message_id, embed.title)
            return True
        except discord.NotFound:
            logger.info("Nachricht %s nicht mehr vorhanden (gelöscht?).", message_id)
        except discord.HTTPException as e:
            logger.warning("Editieren von %s fehlgeschlagen: %s", message_id, e)
        return False
