"""Automatische Discord-Channels.

1. Konferenz-Channels für weitere Wettbewerbe (z.B. #dfb-pokal, #frauen-bundesliga), falls in der
   .env keine ID gesetzt ist. Sie landen in derselben Kategorie wie DISCORD_TICKER_CHANNEL_ID.
2. Spiel-Channels: je Liga eine Kategorie ("Bundesliga 1", "Bundesliga 2"), darin ein Channel pro
   Begegnung (#dortmund-vs-köln). MATCH_CHANNEL_PREPARE_HOURS vor dem ersten Anstoß eines Spieltags
   werden dessen Channels angelegt und die Channels des alten, beendeten Spieltags gelöscht.

Der Bot braucht dafür das Server-Recht "Kanäle verwalten". Fehlt es, läuft der Ticker normal weiter –
dann eben nur in den Konferenz-Channels.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import discord
from sqlalchemy import select

from app import formatters as fmt
from app.config import LEAGUE_NAMES, RUNTIME_CHANNELS, settings
from app.db import AsyncSessionLocal
from app.matchdata import parse_match
from app.models import Match
from app.publisher import Publisher
from app.repositories import channels as ch_repo
from app.repositories.matches import get_matches_between, get_matchday_matches
from app.timeutil import fmt_local, utcnow

logger = logging.getLogger(__name__)

STATUS_PREFIX = {None: "", "live": "🔴", "ft": "✅"}
PREPARE_EVERY = timedelta(minutes=5)
# Spiele eines Spieltags, die so weit nach dem Fenster liegen (verlegte Spiele), bekommen
# erst später einen Channel
LATE_MATCH_SLACK = timedelta(days=7)


def slug(text: str) -> str:
    """Discord-Textchannel: Kleinbuchstaben, keine Leerzeichen."""
    t = (text or "").lower().strip()
    t = re.sub(r"[\s./\\]+", "-", t)
    t = re.sub(r"[^\w\-äöüß]", "", t)
    return re.sub(r"-{2,}", "-", t).strip("-") or "team"


def _short(team) -> str:
    return getattr(team, "short_name", None) or getattr(team, "short", None) or getattr(team, "name", "?")


@dataclass
class Entry:
    match_id: int
    league: str
    season: int
    matchday: int
    channel_id: int
    status: str | None = None
    base_name: str = ""


class ChannelManager:
    def __init__(self, bot: discord.Client, publisher: Publisher):
        self.bot = bot
        self.pub = publisher
        self._map: dict[int, Entry] = {}
        self._last_prepare: datetime | None = None
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._last_err_log = 0.0
        self.problems: list[str] = []
        self.test_channels: list[int] = []
        self.created = 0
        self.deleted = 0

    # ── Basics ───────────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return bool(settings.match_channel_league_list)

    def _guild(self) -> discord.Guild | None:
        return self.bot.get_guild(settings.discord_guild_id)

    def can_manage(self) -> bool:
        g = self._guild()
        return bool(g and g.me and g.me.guild_permissions.manage_channels)

    def _log_error(self, msg: str, *args) -> None:
        """Rechte-Fehler höchstens alle 30 Min. loggen (sonst alle 5 Min. dieselbe Meldung)."""
        if time.monotonic() - self._last_err_log > 1800:
            self._last_err_log = time.monotonic()
            logger.error(msg, *args)

    def channel_for(self, match_id: int) -> int | None:
        e = self._map.get(match_id)
        if not e:
            return None
        g = self._guild()
        if g is not None and g.get_channel(e.channel_id) is None:
            return None  # von Hand gelöscht → beim nächsten prepare() neu anlegen
        return e.channel_id

    def count(self) -> int:
        return len(self._map)

    # ── Start ────────────────────────────────────────────────────────────────
    async def start(self) -> None:
        self.problems = []
        g = self._guild()
        if g is None:
            self.problems.append("Bot ist nicht im Server DISCORD_GUILD_ID")
            return
        if not self.can_manage():
            self.problems.append("Recht 'Kanäle verwalten' fehlt")
            logger.error("❌ Bot hat kein Recht 'Kanäle verwalten' – Spiel-/Pokal-Channels werden nicht angelegt.")
        try:
            for row in await ch_repo.all_channels():
                ch = g.get_channel(row.channel_id)
                base = re.sub(r"^[🔴✅]+", "", ch.name) if ch else ""
                self._map[row.match_id] = Entry(row.match_id, row.league_id, row.season, row.matchday,
                                                row.channel_id, row.status, base)
            logger.info("%d Spiel-Channels aus der DB geladen.", len(self._map))
        except Exception:
            logger.exception("Spiel-Channels konnten nicht aus der DB geladen werden")
        await self.resolve_conference_channels()

    async def resolve_conference_channels(self) -> None:
        """Konferenz-Channel für Ligen ohne feste ID suchen bzw. anlegen."""
        if not settings.auto_create_conference_channels:
            return
        g = self._guild()
        if g is None:
            return
        explicit = {"bl1": settings.discord_ticker_channel_bl1, "bl2": settings.discord_ticker_channel_bl2}
        explicit.update(settings.explicit_ticker_channels)
        main = g.get_channel(settings.discord_ticker_channel_id)
        category = getattr(main, "category", None)
        by_name: dict[str, int] = {}
        for lg, name in settings.conference_names.items():
            if lg not in settings.league_list or explicit.get(lg):
                continue
            name = slug(name)
            cid = by_name.get(name)
            if not cid:
                existing = discord.utils.get(g.text_channels, name=name)
                if existing:
                    cid = existing.id
                elif self.can_manage():
                    try:
                        ch = await g.create_text_channel(
                            name, category=category, topic=f"Live-Konferenz {LEAGUE_NAMES.get(lg, lg)} – Stadion-Echo",
                            reason="Stadion-Echo: Konferenz-Channel",
                        )
                        cid = ch.id
                        self.created += 1
                        logger.info("Konferenz-Channel #%s für %s angelegt.", name, lg)
                    except discord.HTTPException as e:
                        logger.error("Konferenz-Channel #%s konnte nicht angelegt werden: %s", name, e)
            if cid:
                by_name[name] = cid
                RUNTIME_CHANNELS[lg] = cid
                logger.info("Konferenz %s → #%s (%s)", lg, name, cid)
            else:
                logger.warning("Kein Konferenz-Channel für %s – Posts gehen in den Haupt-Ticker.", lg)

    # ── Namen ────────────────────────────────────────────────────────────────
    @staticmethod
    def base_name(m: Match) -> str:
        return f"{slug(_short(m.home_team))}{settings.match_channel_separator}{slug(_short(m.away_team))}"[:95]

    @staticmethod
    def _full_name(base: str, status: str | None) -> str:
        if not settings.match_channel_status_emoji:
            return base
        return f"{STATUS_PREFIX.get(status, '')}{base}"

    @staticmethod
    def topic(m: Match) -> str:
        label = fmt.league_label(m.league_id, m.matchday, group_name=m.group_name)
        return (f"{m.home_team.name} – {m.away_team.name} · {label} · "
                f"Anstoß {fmt_local(m.kickoff_utc, '%a %d.%m. %H:%M')}")[:1024]

    # ── Kategorien ───────────────────────────────────────────────────────────
    async def _category(self, league: str) -> discord.CategoryChannel | None:
        g = self._guild()
        name = settings.category_names.get(league) or LEAGUE_NAMES.get(league, league)
        for cat in g.categories:
            if cat.name.lower() == name.lower():
                return cat
        cat = await g.create_category(name, reason="Stadion-Echo: Spiel-Channels")
        self.created += 1
        logger.info("Kategorie '%s' angelegt.", name)
        return cat

    # ── Spieltag vorbereiten ─────────────────────────────────────────────────
    async def prepare(self, now: datetime | None = None, force: bool = False) -> str:
        """Channels für anstehende Spieltage anlegen, alte löschen. Gibt eine Zusammenfassung zurück."""
        now = now or utcnow()
        if not self.enabled:
            return "Spiel-Channels sind aus (MATCH_CHANNEL_LEAGUES leer)."
        if not force and self._last_prepare and now - self._last_prepare < PREPARE_EVERY:
            return ""
        if self._guild() is None:
            return "Bot ist nicht im Server."
        if not self.can_manage():
            self._log_error("❌ Recht 'Kanäle verwalten' fehlt – Spiel-Channels werden nicht angelegt.")
            return "❌ Dem Bot fehlt das Recht **Kanäle verwalten**."
        async with self._lock:
            self._last_prepare = now
            return await self._prepare(now)

    async def _prepare(self, now: datetime) -> str:
        leagues = settings.match_channel_league_list
        horizon = now + timedelta(hours=settings.match_channel_prepare_hours)
        max_dur = timedelta(minutes=settings.match_max_duration_minutes)
        async with AsyncSessionLocal() as session:
            soon = [m for m in await get_matches_between(session, now - max_dur, horizon) if m.league_id in leagues]
        groups = sorted({(m.league_id, m.season, m.matchday) for m in soon})
        created, removed = 0, 0
        for league, season, md in groups:
            async with AsyncSessionLocal() as session:
                md_matches = await get_matchday_matches(session, md, league, season)
            targets = sorted(
                (m for m in md_matches
                 if now - timedelta(days=3) <= m.kickoff_utc <= horizon + LATE_MATCH_SLACK),
                key=lambda m: (m.kickoff_utc, m.match_id),
            )
            new_here = 0
            for m in targets:
                if self.channel_for(m.match_id):
                    continue
                try:
                    if await self._create(m, now):
                        new_here += 1
                except discord.Forbidden:
                    self._log_error("❌ Discord verweigert das Anlegen von Channels (Recht 'Kanäle verwalten'?)")
                    return "❌ Discord verweigert das Anlegen (Rechte prüfen)."
                except discord.HTTPException as e:
                    logger.error("Channel für Spiel %s nicht angelegt: %s", m.match_id, e)
            created += new_here
            if new_here and settings.match_channel_cleanup:
                removed += await self._cleanup(league, season, md, now)
        if created or removed:
            logger.info("Spiel-Channels: %d angelegt, %d gelöscht.", created, removed)
        return f"✅ {created} Channel angelegt, {removed} alte gelöscht, {len(self._map)} aktiv."

    async def _create(self, m: Match, now: datetime) -> bool:
        g = self._guild()
        cat = await self._category(m.league_id)
        if cat is not None and len(cat.channels) >= 50:
            logger.error("Kategorie '%s' ist voll (50 Channels).", cat.name)
            return False
        status = "ft" if m.is_finished else ("live" if m.kickoff_utc <= now else None)
        base = self.base_name(m)
        old = self._map.get(m.match_id)
        ch = await g.create_text_channel(
            self._full_name(base, status), category=cat, topic=self.topic(m),
            reason=f"Stadion-Echo: {LEAGUE_NAMES.get(m.league_id, m.league_id)} {m.matchday}. Spieltag",
        )
        if old:
            self.pub.forget(old.channel_id)
        self._map[m.match_id] = Entry(m.match_id, m.league_id, m.season, m.matchday, ch.id, status, base)
        self.created += 1
        logger.info("Spiel-Channel #%s angelegt (Spiel %s).", ch.name, m.match_id)
        try:
            await ch_repo.save(m.match_id, m.league_id, m.season, m.matchday, ch.id)
            if status:
                await ch_repo.set_status(m.match_id, status)
        except Exception:
            logger.exception("Spiel-Channel %s konnte nicht in der DB gespeichert werden", ch.id)
        snap = parse_match(m.raw_json, m.league_id, m.season) if m.raw_json else None
        if snap and not status:
            await self.pub.send(ch.id, fmt.embed_match_preview(snap))
        return True

    async def _cleanup(self, league: str, season: int, matchday: int, now: datetime) -> int:
        """Channels älterer, beendeter Spiele derselben Liga löschen."""
        old = [e for e in self._map.values()
               if e.league == league and (e.season, e.matchday) < (season, matchday)]
        if not old:
            return 0
        max_dur = timedelta(minutes=settings.match_max_duration_minutes)
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(Match).where(Match.match_id.in_([e.match_id for e in old])))).scalars().all()
        done = {r.match_id for r in rows if r.is_finished or r.kickoff_utc < now - max_dur}
        done |= {e.match_id for e in old if e.match_id not in {r.match_id for r in rows}}
        g = self._guild()
        removed: list[int] = []
        for e in old:
            if e.match_id not in done:
                continue  # verlegtes Spiel, das noch aussteht → Channel bleibt
            ch = g.get_channel(e.channel_id)
            try:
                if ch is not None:
                    await ch.delete(reason="Stadion-Echo: neuer Spieltag")
                    self.deleted += 1
            except discord.NotFound:
                pass
            except discord.HTTPException as ex:
                logger.warning("Channel %s nicht gelöscht: %s", e.channel_id, ex)
                continue
            self.pub.forget(e.channel_id)
            removed.append(e.match_id)
        for mid in removed:
            self._map.pop(mid, None)
        try:
            await ch_repo.remove(removed)
        except Exception:
            logger.exception("Gelöschte Spiel-Channels konnten nicht aus der DB entfernt werden")
        if removed:
            logger.info("%d alte Spiel-Channels (%s) gelöscht.", len(removed), league)
        return len(removed)

    # ── Testlauf ─────────────────────────────────────────────────────────────
    async def create_test_channel(self, snap) -> int:
        """Temporärer Spiel-Channel für /test_spiel (nicht in der DB, wird per /test_aufraeumen gelöscht)."""
        g = self._guild()
        cat = await self._category(snap.league)
        base = f"{slug(snap.home.short or snap.home.name)}{settings.match_channel_separator}" \
               f"{slug(snap.away.short or snap.away.name)}"
        ch = await g.create_text_channel(f"🧪test-{base}"[:95], category=cat, topic="Testlauf – wird wieder gelöscht",
                                         reason="Stadion-Echo: Testlauf")
        self.test_channels.append(ch.id)
        return ch.id

    async def delete_test_channels(self) -> int:
        g = self._guild()
        n = 0
        for cid in list(self.test_channels):
            ch = g.get_channel(cid) if g else None
            try:
                if ch is not None:
                    await ch.delete(reason="Stadion-Echo: Testlauf aufgeräumt")
                n += 1
                self.test_channels.remove(cid)
                self.pub.forget(cid)
            except discord.HTTPException as e:
                logger.warning("Test-Channel %s nicht gelöscht: %s", cid, e)
        return n

    # ── Status (🔴 / ✅ im Namen) ─────────────────────────────────────────────
    def mark_status(self, match_id: int, status: str) -> None:
        e = self._map.get(match_id)
        if not e or e.status == status:
            return
        e.status = status
        t = asyncio.create_task(self._apply_status(e, status), name=f"rename-{match_id}")
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _apply_status(self, e: Entry, status: str) -> None:
        try:
            await ch_repo.set_status(e.match_id, status)
        except Exception:
            logger.exception("Status von Spiel-Channel %s nicht gespeichert", e.channel_id)
        if not settings.match_channel_status_emoji or not e.base_name:
            return
        g = self._guild()
        ch = g.get_channel(e.channel_id) if g else None
        if ch is None:
            return
        try:
            # Discord erlaubt 2 Umbenennungen / 10 Min. pro Channel – discord.py wartet notfalls,
            # deshalb läuft das als eigener Task und blockiert den Ticker nicht.
            await ch.edit(name=self._full_name(e.base_name, status), reason="Stadion-Echo: Spielstatus")
        except discord.HTTPException as ex:
            logger.warning("Channel %s nicht umbenannt: %s", e.channel_id, ex)
