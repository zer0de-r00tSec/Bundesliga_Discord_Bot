"""DB-Engine, Session-Factory und Initialisierung."""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import LEAGUE_NAMES, settings

logger = logging.getLogger(__name__)

engine = create_async_engine(
    settings.db_url,
    # pre_ping war früher wegen aiomysql deaktiviert. Stattdessen Verbindungen
    # früh recyceln, damit "MySQL server has gone away" nicht auftritt.
    pool_recycle=900,
    pool_pre_ping=False,
    pool_size=5,
    max_overflow=5,
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# Altlasten-Spalten aus früheren Versionen (Telegram / erste Discord-Version) → Zielspalte
_POSTED_EVENTS_LEGACY = {
    "channel_id": ("telegram_chat_id", "discord_channel_id", "chat_id"),
    "message_id": ("telegram_message_id", "discord_message_id"),
}
_POSTED_EVENTS_DDL = {"channel_id": "BIGINT NOT NULL", "message_id": "BIGINT NULL"}


async def _columns(conn, table: str) -> dict[str, dict]:
    rows = await conn.execute(text(
        "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"
    ), {"t": table})
    return {r[0]: {"type": r[1], "nullable": r[2] == "YES", "default": r[3], "extra": r[4] or ""} for r in rows}


async def _migrate_posted_events(conn) -> None:
    """Bringt posted_events auf das aktuelle Schema (idempotent, Daten bleiben erhalten)."""
    cols = await _columns(conn, "posted_events")
    if not cols:
        return  # Tabelle gibt es noch nicht → create_all legt sie neu an
    logger.debug("posted_events vorher: %s", ", ".join(cols))
    for target, legacy_names in _POSTED_EVENTS_LEGACY.items():
        for old in legacy_names:
            if old not in cols:
                continue
            if target not in cols:
                await conn.execute(text(
                    f"ALTER TABLE posted_events CHANGE COLUMN `{old}` `{target}` {_POSTED_EVENTS_DDL[target]}"))
                logger.warning("Migration posted_events: %s → %s umbenannt", old, target)
            else:
                await conn.execute(text(
                    f"UPDATE posted_events SET `{target}` = COALESCE(`{target}`, `{old}`)"))
                await conn.execute(text(f"ALTER TABLE posted_events DROP COLUMN `{old}`"))
                logger.warning("Migration posted_events: Altspalte %s in %s übernommen und entfernt", old, target)
            cols = await _columns(conn, "posted_events")


async def _relax_unknown_columns(conn) -> None:
    """Spalten, die das Modell nicht kennt und die NOT NULL ohne Default sind, blockieren
    jedes INSERT ("Field 'x' doesn't have a default value") → auf NULL-fähig setzen.
    Es wird nichts gelöscht."""
    for table in Base.metadata.sorted_tables:
        cols = await _columns(conn, table.name)
        known = {c.name for c in table.columns}
        for name, info in cols.items():
            if name in known or info["nullable"] or info["default"] is not None:
                continue
            if "auto_increment" in info["extra"].lower():
                continue
            await conn.execute(text(f"ALTER TABLE `{table.name}` MODIFY COLUMN `{name}` {info['type']} NULL"))
            logger.warning("Migration %s: unbekannte Pflichtspalte '%s' auf NULL-fähig gesetzt", table.name, name)


async def init_db() -> None:
    """Tabellen anlegen/migrieren und Ligen seeden (sonst schlägt der FK matches.league_id fehl)."""
    from app import models  # noqa: F401  (registriert Tabellen)

    async with engine.begin() as conn:
        await _migrate_posted_events(conn)
        await conn.run_sync(Base.metadata.create_all)
        await _relax_unknown_columns(conn)
        for lid, name in LEAGUE_NAMES.items():
            await conn.execute(
                text("INSERT IGNORE INTO leagues (id, name) VALUES (:id, :name)"),
                {"id": lid, "name": name},
            )
    logger.info("DB initialisiert.")


async def db_ok() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as e:  # pragma: no cover
        logger.error("DB-Check fehlgeschlagen: %s", e)
        return False
