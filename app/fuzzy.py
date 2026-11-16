"""Vereinssuche: Alias → Teilstring → rapidfuzz."""
from __future__ import annotations

import logging

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Team, TeamAlias

logger = logging.getLogger(__name__)
SCORE_THRESHOLD = 72


def rank_teams(query: str, teams: list[Team], limit: int = 25) -> list[Team]:
    """Für Autocomplete: Teams nach Relevanz."""
    q = query.lower().strip()
    if not q:
        return teams[:limit]
    starts = [t for t in teams if t.name.lower().startswith(q) or (t.short_name or "").lower().startswith(q)]
    contains = [t for t in teams if t not in starts and (q in t.name.lower() or q in (t.short_name or "").lower())]
    rest = [t for t in teams if t not in starts and t not in contains]
    scored = process.extract(q, {t.id: t.name.lower() for t in rest}, scorer=fuzz.WRatio, limit=limit, score_cutoff=50)
    by_id = {t.id: t for t in rest}
    return (starts + contains + [by_id[s[2]] for s in scored])[:limit]


async def find_team(session: AsyncSession, query: str) -> Team | None:
    q = query.lower().strip()
    if not q:
        return None

    # Autocomplete liefert die Team-ID als Wert
    if q.isdigit():
        team = await session.get(Team, int(q))
        if team:
            return team

    alias = (await session.execute(select(TeamAlias).where(TeamAlias.alias == q))).scalar_one_or_none()
    if alias:
        return await session.get(Team, alias.team_id)

    teams = list((await session.execute(select(Team))).scalars().all())
    for t in teams:
        if t.name.lower() == q or (t.short_name or "").lower() == q:
            return t
    contains = [t for t in teams if q in t.name.lower() or q in (t.short_name or "").lower()]
    if len(contains) == 1:
        return contains[0]

    candidates: dict[int, str] = {}
    for t in contains or teams:
        candidates[t.id] = t.name.lower()
        if t.short_name:
            candidates[-t.id] = t.short_name.lower()
    match = process.extractOne(q, candidates, scorer=fuzz.WRatio, score_cutoff=SCORE_THRESHOLD)
    if not match:
        return None
    return await session.get(Team, abs(match[2]))
