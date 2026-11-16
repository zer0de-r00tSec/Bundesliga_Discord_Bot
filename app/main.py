"""Einstiegspunkt: python -m app.main"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import discord

from app.config import settings


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    for noisy in ("discord.gateway", "discord.http", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def main() -> int:
    setup_logging()
    log = logging.getLogger("app")

    from app.db import engine, init_db
    from app.discord_bot import create_bot
    from app.openligadb_client import OpenLigaDBClient

    await init_db()
    client = OpenLigaDBClient()
    bot = create_bot(client)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))
        except NotImplementedError:  # Windows
            pass

    exit_code = 0
    try:
        async with bot:
            await bot.start(settings.discord_bot_token)
    except discord.PrivilegedIntentsRequired:
        log.error(
            "Discord verweigert das Members-Intent. Entweder im Developer-Portal "
            "'Server Members Intent' aktivieren oder DISCORD_WELCOME_CHANNEL_ID=0 setzen."
        )
        exit_code = 2
    except discord.LoginFailure:
        log.error("Login fehlgeschlagen – DISCORD_BOT_TOKEN ist ungültig.")
        exit_code = 2
    finally:
        await client.close()
        await engine.dispose()
        log.info("Bot beendet.")
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
