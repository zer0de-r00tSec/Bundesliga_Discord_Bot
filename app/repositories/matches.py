"""Lese-Abfragen auf matches."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Match
from app.timeutil import local_day_bounds_utc, utcnow


def _base(league_id: str | None = None):
    q = select(Match)
    if league_id:
        q = q.where(Match.league_id == league_id)
    else:
        q = q.where(Match.league_id.in_(settings.league_list))
    return q


async def get_matches_between(
    session: AsyncSession, start_utc: datetime, end_utc: datetime, league_id: str | None = None
) -> list[Match]:
    q = _base(league_id).where(Match.kickoff_utc >= start_utc, Match.kickoff_utc < end_utc)
    return list((await session.execute(q.order_by(Match.kickoff_utc, Match.match_id))).scalars().all())


async def get_day_matches(session: AsyncSession, day: date | None = None, league_id: str | None = None) -> list[Match]:
    """Spiele eines *lokalen* Kalendertags (Europe/Berlin)."""
    start, end = local_day_bounds_utc(day)
    return await get_matches_between(session, start, end, league_id)


get_today_matches = get_day_matches


async def get_live_matches(session: AsyncSession, league_id: str | None = None) -> list[Match]:
    """Angepfiffen und laut letztem Stand noch nicht beendet."""
    now = utcnow()
    q = _base(league_id).where(
        and_(
            Match.kickoff_utc <= now,
            Match.kickoff_utc >= now - timedelta(minutes=settings.match_max_duration_minutes),
            Match.is_finished.is_(False),
        )
    )
    return list((await session.execute(q.order_by(Match.kickoff_utc))).scalars().all())


async def get_upcoming_matches(
    session: AsyncSession, league_id: str | None = None, days: int = 7, limit: int = 25
) -> list[Match]:
    now = utcnow()
    q = _base(league_id).where(Match.kickoff_utc > now, Match.kickoff_utc <= now + timedelta(days=days))
    return list((await session.execute(q.order_by(Match.kickoff_utc).limit(limit))).scalars().all())


async def get_next_kickoff(session: AsyncSession, after: datetime | None = None) -> datetime | None:
    after = after or utcnow()
    q = _base().where(Match.kickoff_utc > after).order_by(Match.kickoff_utc).limit(1)
    m = (await session.execute(q)).scalars().first()
    return m.kickoff_utc if m else None


async def get_matchday_matches(
    session: AsyncSession, matchday: int, league_id: str | None = None, season: int | None = None
) -> list[Match]:
    q = _base(league_id).where(Match.matchday == matchday, Match.season == (season or settings.current_season))
    return list((await session.execute(q.order_by(Match.kickoff_utc))).scalars().all())


async def get_current_matchday(session: AsyncSession, league_id: str, season: int | None = None) -> int:
    """Spieltag des zuletzt angepfiffenen Spiels; vor Saisonstart 1.
    Liegt der nächste Spieltag näher als der letzte zurück (>3 Tage vorbei), wird der nächste genommen."""
    season = season or settings.current_season
    now = utcnow()
    last = (await session.execute(
        select(Match).where(Match.league_id == league_id, Match.season == season, Match.kickoff_utc <= now)
        .order_by(Match.kickoff_utc.desc()).limit(1)
    )).scalars().first()
    nxt = (await session.execute(
        select(Match).where(Match.league_id == league_id, Match.season == season, Match.kickoff_utc > now)
        .order_by(Match.kickoff_utc).limit(1)
    )).scalars().first()
    if last and (not nxt or now - last.kickoff_utc < timedelta(days=3)):
        return last.matchday
    if nxt:
        return nxt.matchday
    return last.matchday if last else 1


def _team_filter(team_id: int):
    return or_(Match.home_team_id == team_id, Match.away_team_id == team_id)


async def get_team_form(session: AsyncSession, team_id: int, limit: int = 5) -> list[Match]:
    q = (
        select(Match)
        .where(_team_filter(team_id), Match.is_finished.is_(True), Match.home_score.is_not(None))
        .order_by(Match.kickoff_utc.desc())
        .limit(limit)
    )
    return list((await session.execute(q)).scalars().all())


async def get_team_next_match(session: AsyncSession, team_id: int) -> Match | None:
    q = (
        select(Match)
        .where(_team_filter(team_id), Match.is_finished.is_(False), Match.kickoff_utc > utcnow() - timedelta(hours=3))
        .order_by(Match.kickoff_utc)
        .limit(1)
    )
    return (await session.execute(q)).scalars().first()


async def get_team_current_or_last(session: AsyncSession, team_id: int) -> Match | None:
    """Laufendes Spiel, sonst das letzte beendete."""
    now = utcnow()
    live = (await session.execute(
        select(Match).where(
            _team_filter(team_id), Match.kickoff_utc <= now,
            Match.kickoff_utc >= now - timedelta(minutes=settings.match_max_duration_minutes),
            Match.is_finished.is_(False),
        ).limit(1)
    )).scalars().first()
    if live:
        return live
    forms = await get_team_form(session, team_id, limit=1)
    return forms[0] if forms else None


async def get_team_league(session: AsyncSession, team_id: int, season: int) -> str | None:
    q = select(Match.league_id).where(_team_filter(team_id), Match.season == season).limit(1)
    return (await session.execute(q)).scalar_one_or_none()


async def get_team_season_stats(session: AsyncSession, team_id: int, season: int) -> dict:
    q = select(Match).where(
        _team_filter(team_id), Match.is_finished.is_(True), Match.season == season, Match.home_score.is_not(None)
    )
    matches = (await session.execute(q)).scalars().all()
    w = d = l = gf = ga = 0
    for m in matches:
        home = m.home_team_id == team_id
        ts, os_ = (m.home_score, m.away_score) if home else (m.away_score, m.home_score)
        gf += ts
        ga += os_
        if ts > os_:
            w += 1
        elif ts == os_:
            d += 1
        else:
            l += 1
    return {"w": w, "d": d, "l": l, "gf": gf, "ga": ga, "played": len(matches)}
