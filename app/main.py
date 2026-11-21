"""Einstiegspunkt: python -m app.main"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import discord

from app.config import settings


LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging() -> None:
    """Konsole (journalctl) mit LOG_LEVEL, Dateien in <projekt>/log/ mit LOG_FILE_LEVEL.

    log/bot.log    – alles ab LOG_FILE_LEVEL (Standard DEBUG), rotiert 10 MB × 10
    log/error.log  – nur Warnungen/Fehler, rotiert 5 MB × 5
    """
    from logging.handlers import RotatingFileHandler

    log_dir = settings.log_path
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    console.setFormatter(fmt)

    main_file = RotatingFileHandler(log_dir / "bot.log", maxBytes=10_000_000, backupCount=10, encoding="utf-8")
    main_file.setLevel(getattr(logging, settings.log_file_level.upper(), logging.DEBUG))
    main_file.setFormatter(fmt)

    err_file = RotatingFileHandler(log_dir / "error.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    err_file.setLevel(logging.WARNING)
    err_file.setFormatter(fmt)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    for h in (console, main_file, err_file):
        root.addHandler(h)

    # Fremdbibliotheken leiser – Rate-Limits (WARNING) von discord.py bleiben sichtbar
    for noisy in ("discord", "discord.gateway", "discord.http", "discord.client", "httpx", "httpcore",
                  "aiomysql", "sqlalchemy", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.INFO)  # zeigt 429/Rate-Limit-Hinweise

    def _excepthook(exc_type, exc, tb):
        logging.getLogger("app").critical("Unbehandelte Ausnahme", exc_info=(exc_type, exc, tb))
    sys.excepthook = _excepthook

    from pathlib import Path
    try:
        version = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    except OSError:
        version = "unbekannt"
    logging.getLogger("app").info("Stadion-Echo Version %s – Code in %s", version, Path(__file__).resolve().parent)
    logging.getLogger("app").info("Logging nach %s (Datei-Level %s)", log_dir, settings.log_file_level.upper())


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
