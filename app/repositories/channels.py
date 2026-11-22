"""Zuordnung Spiel → vom Bot angelegter Discord-Channel."""
from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.db import AsyncSessionLocal
from app.models import MatchChannel
from app.timeutil import utcnow


async def all_channels() -> list[MatchChannel]:
    async with AsyncSessionLocal() as session:
        return list((await session.execute(select(MatchChannel))).scalars().all())


async def save(match_id: int, league: str, season: int, matchday: int, channel_id: int) -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            stmt = mysql_insert(MatchChannel).values(
                match_id=match_id, league_id=league, season=season, matchday=matchday,
                channel_id=channel_id, status=None, created_at=utcnow(),
            )
            await session.execute(stmt.on_duplicate_key_update(
                channel_id=stmt.inserted.channel_id, matchday=stmt.inserted.matchday,
                status=stmt.inserted.status, created_at=stmt.inserted.created_at,
            ))


async def set_status(match_id: int, status: str | None) -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(update(MatchChannel).where(MatchChannel.match_id == match_id).values(status=status))


async def remove(match_ids: list[int]) -> None:
    if not match_ids:
        return
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(delete(MatchChannel).where(MatchChannel.match_id.in_(match_ids)))
