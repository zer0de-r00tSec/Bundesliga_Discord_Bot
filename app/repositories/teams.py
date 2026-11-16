"""Team-Zugriffe und Tabellenposition."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiCache, Match, Team


async def get_all_teams(session: AsyncSession, leagues: list[str] | None = None, season: int | None = None) -> list[Team]:
    q = select(Team)
    if leagues and season:
        ids = select(Match.home_team_id).where(Match.league_id.in_(leagues), Match.season == season)
        q = q.where(Team.id.in_(ids))
    return list((await session.execute(q.order_by(Team.name))).scalars().all())


def table_position(rows: list[dict], team_id: int) -> dict | None:
    for i, row in enumerate(rows or [], 1):
        if row.get("teamInfoId") == team_id:
            return {
                "position": i,
                "points": row.get("points", 0),
                "won": row.get("won", 0),
                "draw": row.get("draw", 0),
                "lost": row.get("lost", 0),
                "goals": row.get("goals", 0),
                "opponent_goals": row.get("opponentGoals", 0),
                "goal_diff": row.get("goalDiff", 0),
                "matches": row.get("matches", 0),
                "total_teams": len(rows),
            }
    return None


async def get_team_table_position(session: AsyncSession, team_id: int, league: str, season: int) -> dict | None:
    cached = await session.get(ApiCache, f"table_{league}_{season}")
    return table_position(list(cached.payload or []) if cached else [], team_id)
