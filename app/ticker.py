"""Ticker-Engine: ersetzt scheduler.py + live_poller.py.

Ablauf pro Tick
  1. ggf. voller Saison-Sync (Anstoßzeiten ändern sich!)
  2. relevante Spiele aus der DB: Anstoß im Fenster [jetzt - max_dauer, jetzt + Vorlauf],
     Abpfiff noch nicht gepostet
  3. pro Liga+Spieltag: billiger lastchangedate-Check → nur bei Änderung den Spieltag laden
  4. Events idempotent posten (Schlüssel in posted_events):
       opener:{datum}:{channel}   Tagesübersicht (Vorlauf vor erstem Anstoß)
       ko:{id}                    Anpfiff (zeitbasiert – OpenLigaDB kennt keinen Live-Status)
       goal:{id}:{h}-{a}          jedes Tor, später automatisch mit Schütze ergänzt (Edit)
       corr:{id}:{alt}>{neu}      Tor zurückgenommen
       ht:{id}                    Halbzeit (sobald Halbzeitergebnis eingetragen)
       ft:{id}                    Abpfiff inkl. Torschützenliste
       table:{liga}:{datum}       Tabelle nach dem letzten Spiel des Tages
       conf:{datum}:{channel}     Live-Konferenz (eine Nachricht, wird laufend editiert)
  5. schlafen: im Spiel `poll_interval`, sonst bis kurz vor dem nächsten Anstoß.

Jeder Fehler wird geloggt und der Loop läuft weiter – der Bot stürzt nicht mehr ab.
Ein Event gilt nur dann als gepostet, wenn Discord die Nachricht wirklich angenommen hat.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import discord

from app import formatters as fmt
from app.config import settings
from app.db import AsyncSessionLocal
from app.matchdata import MatchSnapshot, parse_match
from app.models import Match
from app.openligadb_client import OpenLigaDBClient
from app.publisher import Publisher
from app.repositories import events as ev_repo
from app.repositories.matches import (
    get_day_matches, get_matches_between, get_matchday_matches, get_next_kickoff,
)
from app.sync import sync_all_leagues, sync_matchday, sync_table_cache
from app.timeutil import local_date_of, utcnow

logger = logging.getLogger(__name__)

FORCE_REFETCH_SECONDS = 120  # auch ohne Änderungsmarke spätestens alle 2 Min. neu laden
TABLE_DELAY = timedelta(minutes=3)  # OpenLigaDB rechnet die Tabelle kurz nach Abpfiff neu


def goal_key(mid: int, score_key: str) -> str:
    return f"goal:{mid}:{score_key}"


def _total_of_goal_key(key: str) -> int:
    try:
        h, a = key.rsplit(":", 1)[1].split("-")
        return int(h) + int(a)
    except Exception:
        return 0


@dataclass
class TickerStatus:
    started_at: datetime = field(default_factory=utcnow)
    last_tick: datetime | None = None
    last_full_sync: datetime | None = None
    next_wake: datetime | None = None
    active_matches: int = 0
    last_error: str | None = None
    permission_problems: dict[int, list[str]] = field(default_factory=dict)


class Ticker:
    def __init__(self, client: OpenLigaDBClient, bot: discord.Client, publisher: Publisher | None = None):
        self.client = client
        self.bot = bot
        self.pub = publisher or Publisher(bot)
        self.status = TickerStatus()
        self._wake = asyncio.Event()
        self._force_full_sync = False
        self._marks: dict[tuple, str | None] = {}
        self._last_fetch: dict[tuple, float] = {}
        self._snaps: dict[int, MatchSnapshot] = {}
        self._conf_sig: dict[str, str] = {}
        self._scorer_known: set[str] = set()

    # ── Steuerung ────────────────────────────────────────────────────────────
    def wake(self, full_sync: bool = False) -> None:
        self._force_full_sync = self._force_full_sync or full_sync
        self._wake.set()

    async def run(self) -> None:
        await self.bot.wait_until_ready()
        await self.startup_checks()
        logger.info("Ticker läuft (Saison %s, Ligen %s).", settings.current_season, ",".join(settings.league_list))
        while True:
            try:
                delay = await self.tick()
                self.status.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Fehler im Ticker-Loop – weiter in 60 s")
                self.status.last_error = f"{type(e).__name__}: {e}"
                delay = 60
            self.status.next_wake = utcnow() + timedelta(seconds=delay)
            await self._sleep(delay)

    async def _sleep(self, seconds: float) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=max(1.0, seconds))
        except asyncio.TimeoutError:
            pass

    async def startup_checks(self) -> None:
        for ch_id in settings.all_ticker_channels:
            missing = await self.pub.check_permissions(ch_id)
            self.status.permission_problems[ch_id] = missing
            if missing:
                logger.error("❌ Ticker-Channel %s: fehlende Rechte/Problem: %s", ch_id, ", ".join(missing))
            else:
                logger.info("✅ Ticker-Channel %s: Rechte ok.", ch_id)

    # ── Haupt-Tick ───────────────────────────────────────────────────────────
    async def tick(self, now: datetime | None = None) -> float:
        now = now or utcnow()
        self.status.last_tick = now

        if (
            self._force_full_sync
            or not self.status.last_full_sync
            or now - self.status.last_full_sync >= timedelta(hours=settings.full_sync_interval_hours)
        ):
            self._force_full_sync = False
            await sync_all_leagues(self.client)
            self.status.last_full_sync = now
            self._marks.clear()

        max_dur = timedelta(minutes=settings.match_max_duration_minutes)
        lead = timedelta(minutes=settings.pre_kickoff_wake_minutes)
        async with AsyncSessionLocal() as session:
            window = await get_matches_between(session, now - max_dur, now + lead + timedelta(minutes=1))
        posted = await ev_repo.posted_keys_for([m.match_id for m in window])
        active = [m for m in window if f"ft:{m.match_id}" not in posted]
        self.status.active_matches = len(active)

        # Spieltag laden (nur wenn sich etwas geändert hat)
        groups = sorted({(m.league_id, m.season, m.matchday) for m in active})
        for g in groups:
            await self._refresh_group(*g, now=now)

        for m in active:
            snap = self._snaps.get(m.match_id)
            if snap is None:
                continue
            try:
                await self._process_match(snap, posted, now)
            except Exception:
                logger.exception("Verarbeitung von Match %s fehlgeschlagen", m.match_id)

        today = local_date_of(now)
        await self._maybe_opener(today, now)
        if settings.conference_enabled:
            await self._update_conference(today, now)
        if settings.post_table_after_matchday:
            await self._maybe_tables(today, now)

        return await self._next_delay(active, now)

    async def _next_delay(self, active: list[Match], now: datetime) -> float:
        started = [m for m in active if self._kickoff(m) <= now]
        if started:
            return settings.poll_interval_live_seconds
        lead = timedelta(minutes=settings.pre_kickoff_wake_minutes)
        if active:  # Spiele im Vorlauf: bis zum Anstoß schlafen (max. 5 Min. am Stück)
            nxt = min(self._kickoff(m) for m in active)
            return max(5.0, min((nxt - now).total_seconds(), 300.0))
        async with AsyncSessionLocal() as session:
            nxt = await get_next_kickoff(session, now)
        if not nxt:
            return 6 * 3600
        secs = (nxt - lead - now).total_seconds()
        return max(30.0, min(secs, 6 * 3600))

    def _kickoff(self, m: Match) -> datetime:
        snap = self._snaps.get(m.match_id)
        return (snap.kickoff_utc if snap and snap.kickoff_utc else m.kickoff_utc)

    async def _refresh_group(self, league: str, season: int, day: int, now: datetime) -> None:
        key = (league, season, day)
        mark = await self.client.get_last_change_date(league, season, day)
        stale = time.monotonic() - self._last_fetch.get(key, 0) > FORCE_REFETCH_SECONDS
        if key in self._marks and mark is not None and mark == self._marks[key] and not stale:
            return
        try:
            snaps = await sync_matchday(self.client, league, season, day)
        except Exception as e:
            logger.warning("Spieltag %s/%s/%s nicht ladbar: %s", league, season, day, e)
            return
        self._marks[key] = mark
        self._last_fetch[key] = time.monotonic()
        for s in snaps:
            self._snaps[s.match_id] = s
        logger.debug("Spieltag %s/%s geladen (%d Spiele, Marke %s)", league, day, len(snaps), mark)

    # ── Events eines Spiels ──────────────────────────────────────────────────
    async def _mark(self, posted: set[str], key: str, mid: int, ch: int, msg: discord.Message | None) -> None:
        await ev_repo.mark_posted(key, mid, ch, msg.id if msg else None)
        posted.add(key)

    async def _process_match(self, snap: MatchSnapshot, posted: set[str], now: datetime) -> None:
        mid = snap.match_id
        ch = settings.ticker_channel_for(snap.league)
        if not snap.kickoff_utc or now < snap.kickoff_utc:
            return

        ko_key, ht_key, ft_key = f"ko:{mid}", f"ht:{mid}", f"ft:{mid}"
        # Bot hat ein Spiel erst nach Abpfiff gesehen → nicht nachträglich alles spammen
        catch_up = snap.finished and ko_key not in posted

        # Anpfiff
        if ko_key not in posted:
            if catch_up:
                await self._mark(posted, ko_key, mid, ch, None)
            else:
                embed, file = await fmt.embed_kickoff(snap)
                msg = await self.pub.send(ch, embed, file)
                if msg:
                    await self._mark(posted, ko_key, mid, ch, msg)
                else:
                    return  # Discord-Problem → nächster Tick versucht es erneut

        await self._process_goals(snap, posted, ch, catch_up)

        # Halbzeit
        if ht_key not in posted and snap.halftime is not None:
            if snap.finished:
                await self._mark(posted, ht_key, mid, ch, None)
            else:
                msg = await self.pub.send(ch, fmt.embed_halftime(snap))
                if msg:
                    await self._mark(posted, ht_key, mid, ch, msg)

        # Abpfiff
        if snap.finished and ft_key not in posted:
            msg = await self.pub.send(ch, fmt.embed_fulltime(snap))
            if msg:
                await self._mark(posted, ft_key, mid, ch, msg)
                logger.info("Abpfiff %s: %s %d:%d %s", mid, snap.home.name, *snap.score, snap.away.name)

    async def _process_goals(self, snap: MatchSnapshot, posted: set[str], ch: int, catch_up: bool) -> None:
        mid = snap.match_id
        prefix = f"goal:{mid}:"
        announced = [k for k in posted if k.startswith(prefix)]
        announced_max = max((_total_of_goal_key(k) for k in announced), default=0)
        current_total = sum(snap.score)

        # Korrektur: Spielstand ist niedriger als bereits gemeldet (VAR / Datenfehler)
        if snap.has_score_data and current_total < announced_max:
            too_high = [k for k in announced if _total_of_goal_key(k) > current_total]
            old = max(too_high, key=_total_of_goal_key).rsplit(":", 1)[1]
            new = f"{snap.score[0]}-{snap.score[1]}"
            corr_key = f"corr:{mid}:{old}>{new}"
            if corr_key not in posted:
                oh, oa = (int(x) for x in old.split("-"))
                msg = await self.pub.send(ch, fmt.embed_goal_correction(snap, (oh, oa), snap.score))
                if msg:
                    await self._mark(posted, corr_key, mid, ch, msg)
            await ev_repo.unmark_posted(too_high)  # gleicher Stand später wieder → neu melden
            posted.difference_update(too_high)

        for ev in snap.goals:
            key = goal_key(mid, ev.score_key)
            team_id = snap.home.id if ev.side == "home" else snap.away.id if ev.side == "away" else None
            if key in posted:
                if ev.scorer and key not in self._scorer_known:
                    await self._maybe_update_goal(snap, ev, key, ch, team_id)
                    self._scorer_known.add(key)
                continue
            if catch_up:
                await self._mark(posted, key, mid, ch, None)
            else:
                msg = await self.pub.send(ch, fmt.embed_goal(snap, ev, key))
                if not msg:
                    return  # Reihenfolge wahren – nächster Tick
                await self._mark(posted, key, mid, ch, msg)
                logger.info("Tor %s: %s %s (%s)", mid, ev.score_key, ev.scorer or "?", ev.minute_label)
            await ev_repo.upsert_event(
                key, mid, "goal", minute=ev.minute, home_score=ev.home, away_score=ev.away,
                player_name=ev.scorer, team_id=team_id,
            )
            if ev.scorer:
                self._scorer_known.add(key)

    async def _maybe_update_goal(self, snap, ev, key: str, ch: int, team_id) -> None:
        """Schütze wurde nachgetragen → Tor-Nachricht editieren."""
        stored = await ev_repo.get_event(key)
        if stored and stored.player_name:
            return
        posted = await ev_repo.get_posted(key)
        if posted and posted.message_id:
            ok = await self.pub.edit(posted.channel_id or ch, posted.message_id, fmt.embed_goal(snap, ev, key))
            if ok:
                logger.info("Tor %s %s: Schütze nachgetragen (%s)", snap.match_id, ev.score_key, ev.scorer)
        await ev_repo.upsert_event(
            key, snap.match_id, "goal", minute=ev.minute, home_score=ev.home, away_score=ev.away,
            player_name=ev.scorer, team_id=team_id,
        )

    # ── Tagesbezogene Posts ──────────────────────────────────────────────────
    async def _day_matches_by_channel(self, day: date) -> dict[int, list[Match]]:
        async with AsyncSessionLocal() as session:
            matches = await get_day_matches(session, day)
        out: dict[int, list[Match]] = {}
        for m in matches:
            out.setdefault(settings.ticker_channel_for(m.league_id), []).append(m)
        return out

    async def _maybe_opener(self, day: date, now: datetime) -> None:
        if not settings.post_matchday_opener:
            return
        lead = timedelta(minutes=settings.pre_kickoff_wake_minutes)
        for ch, matches in (await self._day_matches_by_channel(day)).items():
            key = f"opener:{day.isoformat()}:{ch}"
            first = min(self._kickoff(m) for m in matches)
            if now < first - lead:
                continue
            if not any(self._kickoff(m) > now for m in matches):
                continue  # Tag schon gelaufen (z.B. Bot erst abends gestartet)
            if await ev_repo.is_posted(key):
                continue
            msg = await self.pub.send(ch, fmt.embed_matchday_opener(matches, day))
            if msg:
                await ev_repo.mark_posted(key, 0, ch, msg.id)

    def _snapshot_for(self, m: Match) -> MatchSnapshot | None:
        return self._snaps.get(m.match_id) or (parse_match(m.raw_json, m.league_id, m.season) if m.raw_json else None)

    async def _update_conference(self, day: date, now: datetime) -> None:
        for ch, matches in (await self._day_matches_by_channel(day)).items():
            if len(matches) < 2 or not any(self._kickoff(m) <= now for m in matches):
                continue
            snaps = [s for s in (self._snapshot_for(m) for m in matches) if s]
            key = f"conf:{day.isoformat()}:{ch}"
            embed = fmt.embed_conference(snaps, day, now)
            d = embed.to_dict()
            d.pop("timestamp", None)
            sig = repr(d)
            if self._conf_sig.get(key) == sig:
                continue
            existing = await ev_repo.get_posted(key)
            if existing and existing.message_id:
                if await self.pub.edit(ch, existing.message_id, embed):
                    self._conf_sig[key] = sig
                    continue
                if all(s.finished for s in snaps):
                    self._conf_sig[key] = sig  # gelöscht und Tag vorbei → nicht neu posten
                    continue
            msg = await self.pub.send(ch, embed)
            if msg:
                await ev_repo.mark_posted(key, 0, ch, msg.id)
                self._conf_sig[key] = sig

    async def _maybe_tables(self, day: date, now: datetime) -> None:
        async with AsyncSessionLocal() as session:
            matches = await get_day_matches(session, day)
        if not matches:
            return
        ft_rows = await ev_repo.get_posted_many([f"ft:{m.match_id}" for m in matches])
        ft_by_match = {r.match_id: r for r in ft_rows}
        by_lg: dict[str, list[Match]] = {}
        for m in matches:
            by_lg.setdefault(m.league_id, []).append(m)
        for lg, lm in by_lg.items():
            rows_lg = [ft_by_match.get(m.match_id) for m in lm]
            if not all(rows_lg):
                continue
            if now - max(r.posted_at for r in rows_lg) < TABLE_DELAY:
                continue
            # nur wenn der Ticker die Spiele live begleitet hat (nicht beim Nachholen)
            ko_rows = await ev_repo.get_posted_many([f"ko:{m.match_id}" for m in lm])
            if not any(r.message_id for r in ko_rows):
                continue
            ch = settings.ticker_channel_for(lg)
            key = f"table:{lg}:{day.isoformat()}"
            if await ev_repo.is_posted(key):
                continue
            try:
                rows = await sync_table_cache(self.client, lg, lm[0].season)
            except Exception as e:
                logger.warning("Tabelle %s nicht ladbar: %s", lg, e)
                continue
            if not rows:
                continue
            async with AsyncSessionLocal() as session:
                md = await get_matchday_matches(session, lm[0].matchday, lg, lm[0].season)
            embed = fmt.embed_table(rows, lg)
            if md and all(m.is_finished for m in md):
                embed.title = f"📊 Tabelle nach dem {lm[0].matchday}. Spieltag – {fmt.league_label(lg)}"
            else:
                embed.title = f"📊 Zwischenstand nach den Spielen vom {day.strftime('%d.%m.')} – {fmt.league_label(lg)}"
            msg = await self.pub.send(ch, embed)
            if msg:
                await ev_repo.mark_posted(key, 0, ch, msg.id)
