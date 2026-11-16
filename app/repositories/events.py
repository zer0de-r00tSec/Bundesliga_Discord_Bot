"""Idempotenz: posted_events (was wurde gepostet) und match_events (Tor-Details)."""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.db import AsyncSessionLocal
from app.models import MatchEvent, PostedEvent
from app.timeutil import utcnow


async def get_posted(event_key: str) -> PostedEvent | None:
    async with AsyncSessionLocal() as session:
        return await session.get(PostedEvent, event_key)


async def get_posted_many(event_keys: list[str]) -> list[PostedEvent]:
    if not event_keys:
        return []
    async with AsyncSessionLocal() as session:
        rows = await session.execute(select(PostedEvent).where(PostedEvent.event_key.in_(event_keys)))
        return list(rows.scalars().all())


async def posted_keys_for(match_ids: list[int]) -> set[str]:
    if not match_ids:
        return set()
    async with AsyncSessionLocal() as session:
        rows = await session.execute(select(PostedEvent.event_key).where(PostedEvent.match_id.in_(match_ids)))
        return set(rows.scalars().all())


async def is_posted(event_key: str) -> bool:
    return await get_posted(event_key) is not None


async def mark_posted(event_key: str, match_id: int, channel_id: int, message_id: int | None) -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            stmt = mysql_insert(PostedEvent).values(
                event_key=event_key, match_id=match_id, channel_id=channel_id,
                message_id=message_id, posted_at=utcnow(),
            )
            await session.execute(stmt.on_duplicate_key_update(
                telegram_message_id=stmt.inserted.telegram_message_id,
                telegram_chat_id=stmt.inserted.telegram_chat_id,
            ))


async def unmark_posted(event_keys: list[str]) -> None:
    if not event_keys:
        return
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(delete(PostedEvent).where(PostedEvent.event_key.in_(event_keys)))


async def get_event(event_key: str) -> MatchEvent | None:
    async with AsyncSessionLocal() as session:
        return await session.get(MatchEvent, event_key)


async def upsert_event(
    event_key: str, match_id: int, event_type: str, *, minute=None, home_score=None,
    away_score=None, player_name=None, team_id=None, raw_json=None,
) -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            stmt = mysql_insert(MatchEvent).values(
                event_key=event_key, match_id=match_id, event_type=event_type, minute=minute,
                home_score=home_score, away_score=away_score, player_name=player_name,
                team_id=team_id, raw_json=raw_json, created_at=utcnow(),
            )
            await session.execute(stmt.on_duplicate_key_update(
                minute=stmt.inserted.minute, player_name=stmt.inserted.player_name,
                team_id=stmt.inserted.team_id, raw_json=stmt.inserted.raw_json,
            ))


async def get_state(key: str) -> str | None:
    from app.models import BotState

    async with AsyncSessionLocal() as session:
        row = await session.get(BotState, key)
        return row.state_value if row else None


async def set_state(key: str, value: str) -> None:
    from app.models import BotState

    async with AsyncSessionLocal() as session:
        async with session.begin():
            stmt = mysql_insert(BotState).values(state_key=key, state_value=value, updated_at=utcnow())
            await session.execute(stmt.on_duplicate_key_update(
                state_value=stmt.inserted.state_value, updated_at=stmt.inserted.updated_at))
