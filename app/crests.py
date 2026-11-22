"""Vereinswappen lokal vorhalten: fehlende laden, nach PNG wandeln, dauerhaft speichern.

Quelle ist `teamIconUrl` aus OpenLigaDB. Die zeigt oft auf Wikimedia (SVG) oder imgur (PNG).
  - SVG  → mit cairosvg nach PNG gerendert; ohne cairosvg über das Wikimedia-PNG-Thumbnail
  - PNG/WebP/JPG → mit Pillow gelesen und als PNG gespeichert
Gespeichert wird immer app/assets/crests/<teamId>.png (max. 512 px). Vorhandene Dateien werden
nie überschrieben – ein Wappen wird also genau einmal geladen und bleibt dann liegen.

Wann geladen wird
  - nach jedem Saison-Sync (Start + alle FULL_SYNC_INTERVAL_HOURS) für alle Teams der Ligen
  - zum Saisonende (siehe prefetch_next_season): Teams der Liga darunter (mögliche Aufsteiger,
    inkl. Relegation) und – sobald OpenLigaDB ihn hat – der Spielplan der neuen Saison
  - von Hand: python -m tools.check_crests --fetch
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from urllib.parse import urlparse

import httpx
from PIL import Image

from app import image_composer
from app.image_composer import CRESTS_DIR, get_crest_path

try:
    import cairosvg  # benötigt libcairo2 auf dem System
except Exception:  # pragma: no cover
    cairosvg = None

logger = logging.getLogger(__name__)

# Wikimedia sperrt Clients ohne aussagekräftigen User-Agent (HTTP 403)
USER_AGENT = "StadionEcho-DiscordTicker/2.0 (+https://github.com/zer0de-r00tSec/Bundesliga_Discord_Bot)"
CREST_SIZE = 512
MAX_BYTES = 8_000_000
RETRY_AFTER_SECONDS = 6 * 3600   # fehlgeschlagene Wappen frühestens nach 6 h erneut versuchen
REQUEST_PAUSE = 0.3              # freundlich zu Wikimedia/imgur

# Liga → Liga darunter (deren Teams können aufsteigen bzw. spielen Relegation)
LOWER_LEAGUE = {"bl1": "bl2", "bl2": "bl3", "ffb1": "ffb2"}

_failed: dict[int, float] = {}
_names: dict[str, int] = {}   # normalisierter Teamname → teamId (aus allen bisher gesehenen Teams)

_TEAM_SUFFIX = re.compile(r"\s+(f|frauen|women|ii|2|u\d\d)$", re.IGNORECASE)


def _norm(name: str) -> str:
    return " ".join((name or "").lower().split())


def club_name(name: str) -> str:
    """'Hertha BSC F' / 'Hannover 96 Frauen' / 'VfB Stuttgart II' → Vereinsname."""
    n = _norm(name)
    while True:
        stripped = _TEAM_SUFFIX.sub("", n)
        if stripped == n:
            return n
        n = stripped


def _copy_from_club(team_id: int, name: str) -> bool:
    """Kein eigenes Wappen ladbar → das des Vereins (Herrenteam) übernehmen, falls vorhanden."""
    club = club_name(name)
    if club == _norm(name):
        return False
    src_id = _names.get(club)
    src = get_crest_path(src_id) if src_id and src_id != team_id else None
    if not src:
        return False
    if src.suffix.lower() == ".png":
        png = src.read_bytes()
    else:
        try:
            png = to_png(src.read_bytes(), str(src))
        except Exception as e:
            logger.warning("Wappen %s: Vereinswappen %s nicht umwandelbar: %s", team_id, src.name, e)
            return False
    CRESTS_DIR.mkdir(parents=True, exist_ok=True)
    (CRESTS_DIR / f"{team_id}.png").write_bytes(png)
    logger.info("Wappen %s (%s): Vereinswappen von %s (%s) übernommen", team_id, name, src_id, club)
    return True
_prefetch_done: set[tuple[str, int]] = set()

_WIKIMEDIA = re.compile(
    r"^https?://upload\.wikimedia\.org/wikipedia/(?P<proj>[^/]+)/(?!thumb/)"
    r"(?P<a>[0-9a-f])/(?P<ab>[0-9a-f]{2})/(?P<file>[^/?#]+)$"
)


def wikimedia_png_url(url: str | None, width: int = 500) -> str | None:
    """Wikimedia-SVG → URL des fertig gerenderten PNG-Thumbnails (None, wenn nicht passend).
    500 px ist eine Standardbreite – Wikimedia liefert nur noch feste Thumbnail-Stufen zuverlässig."""
    m = _WIKIMEDIA.match(url or "")
    if not m or not m["file"].lower().endswith(".svg"):
        return None
    return (f"https://upload.wikimedia.org/wikipedia/{m['proj']}/thumb/"
            f"{m['a']}/{m['ab']}/{m['file']}/{width}px-{m['file']}.png")


_WIKI_PAGE = re.compile(
    r"^https?://(?P<host>[a-z\-]+\.(?:m\.)?wikipedia\.org|commons\.(?:m\.)?wikimedia\.org)"
    r"/wiki/(?:File|Datei|Image|Bild|Fichier):(?P<file>[^#?]+)"
)


def wiki_file_url(url: str | None, width: int = 500) -> str | None:
    """Wikipedia-/Commons-*Dateiseite* (HTML) → direkter Bild-Link über Special:FilePath.

    OpenLigaDB hat manchmal 'https://commons.wikimedia.org/wiki/File:X.svg#/media/…' statt der
    Bilddatei. Mit ?width liefert Wikimedia ein fertig gerendertes PNG (auch für SVG)."""
    m = _WIKI_PAGE.match(url or "")
    if not m:
        return None
    host = m["host"].replace(".m.", ".")
    return f"https://{host}/wiki/Special:FilePath/{m['file']}" + (f"?width={width}" if width else "")


_RASTER_MAGIC = (b"\x89PNG", b"GIF8", b"\xff\xd8\xff", b"RIFF", b"BM")


def _looks_like_svg(data: bytes, url: str, content_type: str) -> bool:
    if data.startswith(_RASTER_MAGIC):  # Inhalt schlägt Dateiendung (FilePath?width liefert PNG)
        return False
    if "svg" in content_type.lower() or urlparse(url).path.lower().endswith(".svg"):
        return True
    head = data[:2048].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head)


def to_png(data: bytes, url: str = "", content_type: str = "") -> bytes:
    """Beliebiges Wappen-Bild → PNG mit Transparenz, längste Seite max. CREST_SIZE."""
    head = data[:512].lstrip().lower()
    if "html" in content_type.lower() or head.startswith((b"<!doctype html", b"<html")):
        raise RuntimeError("HTML-Seite statt Bild")
    if _looks_like_svg(data, url, content_type):
        if cairosvg is None:
            raise RuntimeError("SVG, aber cairosvg ist nicht installiert")
        data = cairosvg.svg2png(bytestring=data, output_width=CREST_SIZE)
    img = Image.open(io.BytesIO(data))
    img.load()
    img = img.convert("RGBA")
    if max(img.size) > CREST_SIZE:
        img.thumbnail((CREST_SIZE, CREST_SIZE), Image.LANCZOS)
    if img.width < 16 or img.height < 16:
        raise RuntimeError(f"Bild zu klein ({img.width}x{img.height})")
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _candidates(url: str) -> list[str]:
    page = wiki_file_url(url)
    if page:
        return [page, wiki_file_url(url, width=0)]
    thumb = wikimedia_png_url(url)
    if thumb and cairosvg is None:
        return [thumb]
    return [url, thumb] if thumb else [url]


def new_http_client(**kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        **kw,
        timeout=20.0,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "image/png,image/svg+xml,image/*;q=0.8,*/*;q=0.5"},
    )


async def fetch_crest(http: httpx.AsyncClient, team_id: int, url: str) -> bool:
    """Ein Wappen laden und als <teamId>.png speichern. True bei Erfolg."""
    last_err = "keine URL"
    for u in _candidates(url):
        try:
            resp = await http.get(u)
            resp.raise_for_status()
            if len(resp.content) > MAX_BYTES:
                raise RuntimeError(f"zu groß ({len(resp.content)} Bytes)")
            png = await asyncio.to_thread(to_png, resp.content, str(resp.url), resp.headers.get("content-type", ""))
        except Exception as e:
            last_err = f"{u}: {type(e).__name__}: {e}"
            continue
        CRESTS_DIR.mkdir(parents=True, exist_ok=True)
        target = CRESTS_DIR / f"{team_id}.png"
        tmp = target.with_suffix(".png.tmp")
        tmp.write_bytes(png)
        tmp.replace(target)  # atomar – der Composer sieht nie eine halbe Datei
        return True
    logger.warning("Wappen %s nicht ladbar – %s", team_id, last_err)
    return False


def teams_from_snaps(snaps) -> dict[int, tuple[str, str | None]]:
    """MatchSnapshots → {teamId: (Name, IconUrl)}."""
    out: dict[int, tuple[str, str | None]] = {}
    for s in snaps or []:
        for t in (s.home, s.away):
            if t.id and (t.id not in out or not out[t.id][1]):
                out[t.id] = (t.name, t.icon_url)
    return out


def missing_crests(teams: dict[int, tuple[str, str | None]]) -> list[int]:
    return sorted(tid for tid in teams if not get_crest_path(tid))


async def ensure_crests(teams: dict[int, tuple[str, str | None]], http: httpx.AsyncClient | None = None,
                        force: bool = False) -> list[int]:
    """Fehlende Wappen laden. Gibt die IDs der neu gespeicherten Wappen zurück."""
    for tid, (name, _url) in teams.items():
        n = _norm(name)
        if n and (n == club_name(n)) and get_crest_path(tid):
            _names.setdefault(n, tid)
    now = time.monotonic()
    todo = [tid for tid in missing_crests(teams)
            if force or now - _failed.get(tid, -RETRY_AFTER_SECONDS) >= RETRY_AFTER_SECONDS]
    if not todo:
        return []
    own = http is None
    http = http or new_http_client()
    fetched: list[int] = []
    try:
        for tid in todo:
            name, url = teams[tid]
            ok = False
            if url:
                ok = await fetch_crest(http, tid, url)
                await asyncio.sleep(REQUEST_PAUSE)
                if ok:
                    logger.info("Wappen geladen: %s (%s)", tid, name)
                    if _norm(name) == club_name(name):
                        _names.setdefault(_norm(name), tid)
            if not ok:
                ok = _copy_from_club(tid, name)
            if ok:
                fetched.append(tid)
                _failed.pop(tid, None)
            else:
                if not url:
                    logger.warning("Wappen %s (%s): OpenLigaDB liefert keine teamIconUrl", tid, name)
                _failed[tid] = now
    finally:
        if own:
            await http.aclose()
    if fetched:
        image_composer.clear_cache()  # sonst bleibt "fehlt" im lru_cache des Composers hängen
    logger.info("Wappen-Sync: %d neu, %d fehlen weiterhin (%d Teams geprüft)",
                len(fetched), len(todo) - len(fetched), len(teams))
    return fetched


async def ensure_for_raws(raws: list[dict]) -> list[int]:
    """Roh-Matchdaten von OpenLigaDB → Wappen aller beteiligten Teams sicherstellen."""
    teams: dict[int, tuple[str, str | None]] = {}
    for raw in raws or []:
        for key in ("team1", "team2"):
            t = raw.get(key) or {}
            tid = t.get("teamId")
            if isinstance(tid, int) and tid > 0 and (tid not in teams or not teams[tid][1]):
                teams[tid] = (t.get("teamName") or str(tid), t.get("teamIconUrl"))
    return await ensure_crests(teams)


# ── Saisonwechsel ────────────────────────────────────────────────────────────

async def _season_finished(league: str, season: int) -> bool:
    from sqlalchemy import case, func, select

    from app.db import AsyncSessionLocal
    from app.models import Match

    async with AsyncSessionLocal() as session:
        total, open_ = (await session.execute(
            select(func.count(), func.sum(case((Match.is_finished.is_(True), 0), else_=1)))
            .where(Match.league_id == league, Match.season == season)
        )).one()
    return bool(total) and not open_


async def prefetch_next_season(client, leagues: list[str], season: int) -> None:
    """Zum Saisonende die Wappen der nächsten Saison bereitlegen.

    Ist eine Liga komplett durchgespielt, werden geladen:
      1. alle Teams der Liga darunter (Aufsteiger + Relegation, z.B. 3. Liga für die 2. BL)
      2. alle Teams der neuen Saison, sobald OpenLigaDB deren Spielplan veröffentlicht hat
    Läuft bei jedem Voll-Sync (alle 12 h), bis alles da ist – danach nur noch ein DB-Check.
    """
    for league in leagues:
        try:
            if not await _season_finished(league, season):
                continue
        except Exception as e:
            logger.warning("Saisonende-Check %s/%s fehlgeschlagen: %s", league, season, e)
            continue

        jobs = []
        lower = LOWER_LEAGUE.get(league)
        if lower and lower not in leagues:
            jobs.append((lower, season, "Aufsteiger-Kandidaten"))
        jobs.append((league, season + 1, "neue Saison"))

        for lg, ssn, what in jobs:
            if (lg, ssn) in _prefetch_done:
                continue
            try:
                raws = await client.get_season_matches(lg, ssn)
            except Exception as e:
                logger.info("Wappen-Vorladen %s/%s (%s): noch nicht verfügbar (%s)", lg, ssn, what, e)
                continue
            if not raws:
                logger.info("Wappen-Vorladen %s/%s (%s): Spielplan noch nicht veröffentlicht", lg, ssn, what)
                continue
            await ensure_for_raws(raws)
            teams = {t: ("", None) for r in raws for t in ((r.get("team1") or {}).get("teamId"),
                                                         (r.get("team2") or {}).get("teamId"))
                     if isinstance(t, int) and t > 0}
            if not missing_crests(teams):
                _prefetch_done.add((lg, ssn))
                logger.info("Wappen-Vorladen %s/%s (%s): alle %d Wappen vorhanden", lg, ssn, what, len(teams))
