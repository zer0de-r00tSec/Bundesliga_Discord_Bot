"""Zeigt Teams der DB ohne lokales Wappen (app/assets/crests/<teamId>.svg|png).
Aufruf: python -m tools.check_crests"""
import asyncio

from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.image_composer import get_crest_path
from app.models import Team


async def main():
    async with AsyncSessionLocal() as session:
        teams = (await session.execute(select(Team).order_by(Team.name))).scalars().all()
    missing = [t for t in teams if not get_crest_path(t.id)]
    if not missing:
        print("✅ Alle Wappen vorhanden.")
    for t in missing:
        print(f"❌ {t.id:>5}  {t.name}")


if __name__ == "__main__":
    asyncio.run(main())
