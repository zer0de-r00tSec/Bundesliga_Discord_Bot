"""Wappen prüfen und fehlende nachladen.

  python -m tools.check_crests              zeigt Teams der DB ohne lokales Wappen
  python -m tools.check_crests --fetch      lädt fehlende Wappen der konfigurierten Ligen (LEAGUES)
  python -m tools.check_crests --fetch bl3 ffb2 --season 2026
                                            lädt zusätzlich/gezielt andere Ligen bzw. Saisons
  … --force                                 ignoriert die 6-h-Sperre nach Fehlversuchen

Gespeichert wird app/assets/crests/<teamId>.png – vorhandene Dateien bleiben unangetastet.
"""
import argparse
import asyncio
import logging

from sqlalchemy import select

from app import crests
from app.config import settings
from app.db import AsyncSessionLocal
from app.image_composer import get_crest_path
from app.models import Team
from app.openligadb_client import OpenLigaDBClient


async def show_missing() -> None:
    async with AsyncSessionLocal() as session:
        teams = (await session.execute(select(Team).order_by(Team.name))).scalars().all()
    missing = [t for t in teams if not get_crest_path(t.id)]
    if not missing:
        print(f"✅ Alle {len(teams)} Wappen vorhanden.")
    for t in missing:
        print(f"❌ {t.id:>5}  {t.name}")


async def fetch(leagues: list[str], season: int, force: bool) -> None:
    client = OpenLigaDBClient()
    try:
        for lg in leagues:
            raws = await client.get_season_matches(lg, season)
            if not raws:
                print(f"⚠️  {lg}/{season}: keine Spiele bei OpenLigaDB")
                continue
            teams: dict[int, tuple[str, str | None]] = {}
            for raw in raws:
                for key in ("team1", "team2"):
                    t = raw.get(key) or {}
                    if isinstance(t.get("teamId"), int):
                        teams.setdefault(t["teamId"], (t.get("teamName") or "?", t.get("teamIconUrl")))
            new = await crests.ensure_crests(teams, force=force)
            still = crests.missing_crests(teams)
            print(f"{lg}/{season}: {len(teams)} Teams, {len(new)} neu geladen, {len(still)} fehlen")
            for tid in still:
                print(f"   ❌ {tid:>5}  {teams[tid][0]}  ← {teams[tid][1] or 'keine URL'}")
    finally:
        await client.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", nargs="*", metavar="LIGA", help="fehlende Wappen laden (ohne Liga: LEAGUES)")
    ap.add_argument("--season", type=int, default=0, help="Saison (Standard: aktuelle)")
    ap.add_argument("--force", action="store_true", help="auch kürzlich fehlgeschlagene erneut versuchen")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.fetch is None:
        asyncio.run(show_missing())
        return
    leagues = args.fetch or settings.league_list
    asyncio.run(fetch(leagues, args.season or settings.current_season, args.force))


if __name__ == "__main__":
    main()
