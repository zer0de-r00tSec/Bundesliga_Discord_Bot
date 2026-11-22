"""Synchronisation OpenLigaDB → MariaDB."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.config import CUP_LEAGUES, settings
from app.db import AsyncSessionLocal
from app.matchdata import MatchSnapshot, parse_match
from app.models import ApiCache, Match, Team, TeamAlias
from app.openligadb_client import OpenLigaDBClient
from app.timeutil import to_local, utcnow

logger = logging.getLogger(__name__)


async def upsert_team(session, team_id: int, name: str, short: str | None) -> None:
    stmt = mysql_insert(Team).values(id=team_id, name=name, short_name=short)
    stmt = stmt.on_duplicate_key_update(name=stmt.inserted.name, short_name=stmt.inserted.short_name)
    await session.execute(stmt)


async def upsert_match(session, snap: MatchSnapshot) -> None:
    await upsert_team(session, snap.home.id, snap.home.name, snap.home.short)
    await upsert_team(session, snap.away.id, snap.away.name, snap.away.short)
    local = to_local(snap.kickoff_utc)
    values = dict(
        match_id=snap.match_id,
        league_id=snap.league,
        season=snap.season,
        matchday=snap.matchday,
        group_name=snap.group_name,
        kickoff_utc=snap.kickoff_utc,
        kickoff_local=local.replace(tzinfo=None) if local else None,
        home_team_id=snap.home.id,
        away_team_id=snap.away.id,
        home_score=snap.score[0] if snap.has_score_data else None,
        away_score=snap.score[1] if snap.has_score_data else None,
        is_finished=snap.finished,
        status="finished" if snap.finished else None,
        last_update=snap.last_update,
        last_synced_at=utcnow(),
        raw_json=snap.raw,
    )
    stmt = mysql_insert(Match).values(**values)
    update_cols = {k: stmt.inserted[k] for k in values if k != "match_id"}
    await session.execute(stmt.on_duplicate_key_update(**update_cols))


async def store_snapshots(snaps: list[MatchSnapshot]) -> None:
    if not snaps:
        return
    async with AsyncSessionLocal() as session:
        async with session.begin():
            for s in snaps:
                await upsert_match(session, s)


def parse_all(raws: list[dict], league: str, season: int) -> list[MatchSnapshot]:
    out = []
    for raw in raws or []:
        snap = parse_match(raw, league, season)
        if snap and snap.kickoff_utc:
            snap.league = league  # Shortcut aus der Anfrage ist verlässlicher
            snap.season = season
            out.append(snap)
    return out


async def sync_season(client: OpenLigaDBClient, league: str, season: int) -> int:
    logger.info("Season-Sync %s/%s …", league, season)
    snaps = parse_all(await client.get_season_matches(league, season), league, season)
    if not snaps:
        logger.warning("Keine Spiele für %s/%s erhalten – stimmt SEASON?", league, season)
        return 0
    await store_snapshots(snaps)
    logger.info("Season-Sync %s/%s fertig: %d Spiele", league, season, len(snaps))
    return len(snaps)


async def sync_matchday(client: OpenLigaDBClient, league: str, season: int, day: int) -> list[MatchSnapshot]:
    snaps = parse_all(await client.get_matchday(league, season, day), league, season)
    await store_snapshots(snaps)
    return snaps


async def sync_table_cache(client: OpenLigaDBClient, league: str, season: int) -> list[dict]:
    rows = await client.get_table(league, season)
    if not rows:
        return []
    async with AsyncSessionLocal() as session:
        async with session.begin():
            stmt = mysql_insert(ApiCache).values(
                cache_key=f"table_{league}_{season}", payload=rows, updated_at=utcnow()
            )
            await session.execute(
                stmt.on_duplicate_key_update(payload=stmt.inserted.payload, updated_at=stmt.inserted.updated_at)
            )
    return rows


async def get_cached_table(league: str, season: int) -> list[dict]:
    async with AsyncSessionLocal() as session:
        row = await session.get(ApiCache, f"table_{league}_{season}")
        return list(row.payload or []) if row else []


async def sync_all_leagues(client: OpenLigaDBClient) -> dict[str, int]:
    """Season + Tabelle für alle Ligen. Fehler einer Liga stoppen die anderen nicht."""
    season = settings.current_season
    result: dict[str, int] = {}
    for league in settings.league_list:
        try:
            result[league] = await sync_season(client, league, season)
        except Exception as e:
            logger.error("Season-Sync %s fehlgeschlagen: %s", league, e)
            result[league] = -1
        if league in CUP_LEAGUES:
            continue  # Pokal: keine Tabelle
        try:
            await sync_table_cache(client, league, season)
        except Exception as e:
            logger.warning("Tabellen-Cache %s fehlgeschlagen: %s", league, e)
    try:
        await seed_aliases()
    except Exception as e:
        logger.warning("Alias-Seed fehlgeschlagen: %s", e)
    return result


# ── Aliase ───────────────────────────────────────────────────────────────────
# alias → Suchbegriff im offiziellen Teamnamen
MANUAL_ALIASES: dict[str, str] = {
    "fcb": "bayern", "bayern": "bayern", "fc bayern": "bayern",
    "bvb": "dortmund", "bvb09": "dortmund", "dortmund": "dortmund",
    "b04": "leverkusen", "leverkusen": "leverkusen", "werkself": "leverkusen",
    "rbl": "leipzig", "leipzig": "leipzig",
    "vfb": "stuttgart", "stuttgart": "stuttgart",
    "sge": "frankfurt", "eintracht": "frankfurt", "frankfurt": "frankfurt",
    "scf": "freiburg", "freiburg": "freiburg",
    "effzeh": "köln", "koeln": "köln", "köln": "köln", "fck": "köln",
    "bmg": "gladbach", "gladbach": "gladbach", "fohlen": "gladbach",
    "svw": "werder", "werder": "werder", "bremen": "werder",
    "hsv": "hamburger", "hamburg": "hamburger",
    "s04": "schalke", "schalke": "schalke",
    "bsc": "hertha", "hertha": "hertha",
    "m05": "mainz", "mainz": "mainz",
    "wob": "wolfsburg", "wolfsburg": "wolfsburg",
    "tsg": "hoffenheim", "hoffenheim": "hoffenheim",
    "fca": "augsburg", "augsburg": "augsburg",
    "boch": "bochum", "bochum": "bochum",
    "f95": "düsseldorf", "fortuna": "düsseldorf", "düsseldorf": "düsseldorf",
    "h96": "hannover", "hannover": "hannover",
    "fcu": "union", "union": "union", "eisern": "union",
    "fcsp": "pauli", "st pauli": "pauli", "pauli": "pauli",
    "fch": "heidenheim", "heidenheim": "heidenheim",
    "ksv": "kiel", "kiel": "kiel", "holstein": "kiel",
    "fcn": "nürnberg", "club": "nürnberg", "nürnberg": "nürnberg",
    "ksc": "karlsruher", "karlsruhe": "karlsruher",
    "svd": "darmstadt", "darmstadt": "darmstadt", "lilien": "darmstadt",
    "scp": "paderborn", "paderborn": "paderborn",
    "fcm": "magdeburg", "magdeburg": "magdeburg",
    "dsc": "bielefeld", "arminia": "bielefeld", "bielefeld": "bielefeld",
    "fck lautern": "kaiserslautern", "lautern": "kaiserslautern", "betze": "kaiserslautern",
    "sge fürth": "fürth", "kleeblatt": "fürth", "fürth": "fürth",
    "elv": "elversberg", "elversberg": "elversberg",
    "ssv": "ulm", "ulm": "ulm",
    "pmü": "preußen", "preussen": "preußen", "münster": "preußen",
    "ebs": "braunschweig", "braunschweig": "braunschweig",
    "jahn": "regensburg", "regensburg": "regensburg",
    "sgd": "dresden", "dynamo": "dresden", "dresden": "dresden",
}


async def seed_aliases() -> int:
    """Aliase auf Teams der aktuellen DB auflösen. Nicht auflösbare werden übersprungen."""
    async with AsyncSessionLocal() as session:
        teams = (await session.execute(select(Team))).scalars().all()
    if not teams:
        return 0
    names = {t.id: t.name.lower() for t in teams}
    # Herren-Teams bevorzugen: "fcb" soll FC Bayern sein, nicht die Frauenmannschaft
    async with AsyncSessionLocal() as session:
        mens = set((await session.execute(
            select(Match.home_team_id).where(Match.league_id.in_(("bl1", "bl2", "bl3", "dfb")))
        )).scalars().all())
    count = 0
    async with AsyncSessionLocal() as session:
        async with session.begin():
            for alias, term in MANUAL_ALIASES.items():
                # nur eindeutige Teilstring-Treffer (Teams anderer Ligen fehlen in der DB)
                hits = [tid for tid, n in names.items() if term in n]
                if len(hits) > 1:
                    hits = [tid for tid in hits if tid in mens] or hits
                if len(hits) != 1:
                    continue
                best = hits[0]
                stmt = mysql_insert(TeamAlias).values(alias=alias, team_id=best)
                await session.execute(stmt.on_duplicate_key_update(team_id=stmt.inserted.team_id))
                count += 1
    logger.info("%d Aliase gesetzt.", count)
    return count
