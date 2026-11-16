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


async def init_db() -> None:
    """Tabellen anlegen und Ligen seeden (sonst schlägt der FK matches.league_id fehl)."""
    from app import models  # noqa: F401  (registriert Tabellen)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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
