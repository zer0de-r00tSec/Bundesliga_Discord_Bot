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
       ht:{id}                    Halbzeit (ab 50 Min. nach Anpfiff, siehe matchdata.HALFTIME_AFTER)
       ft:{id}                    Abpfiff inkl. Torschützenliste
       table:{liga}:{datum}       Tabelle nach dem letzten Spiel des Tages
       conf:{datum}:{channel}     Live-Konferenz (eine Nachricht, wird laufend editiert)
     Dieselben Spiel-Events gibt es mit Präfix "mc:" für den eigenen Spiel-Channel
     (mc:ko:{id}, mc:goal:{id}:{h}-{a}, mc:ht:{id}, mc:ft:{id} …), siehe app/channels.py.
  5. schlafen: im Spiel `poll_interval`, sonst bis kurz vor dem nächsten Anstoß.

Zwischenspeicher gegen API-Aussetzer (`_guard`): Liefert OpenLigaDB plötzlich weniger Tore, keine
Ergebnisse oder einen älteren Datenstand, bleibt der letzte gute Stand stehen (auch in der DB), bis die
Antwort sich DOWNGRADE_CONFIRMATIONS-mal bestätigt. Der Pausenstand wird in der Pause live mitgeschrieben
(match_events "htscore:{id}") – das "Halbzeit"-Ergebnis der API bleibt live oft auf 0:0 stehen.

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
from app import score_card, sprueche
from app.config import CUP_LEAGUES, settings
from app.db import AsyncSessionLocal
from app.matchdata import (
    HALFTIME_AFTER, HALFTIME_CAPTURE_UNTIL, MatchSnapshot, halftime_postable, halftime_reached,
    merge_halftime, parse_match,
)
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
TABLE_DELAY = timedelta(minutes=3)
# Bot sieht ein laufendes Spiel erst so spät nach Anpfiff → eine Zwischenstand-Karte statt Nachholen
LATE_JOIN = timedelta(minutes=10)  # OpenLigaDB rechnet die Tabelle kurz nach Abpfiff neu
MC = "mc:"  # Präfix für Events im Spiel-Channel
# Ein niedrigerer Spielstand / fehlende Daten müssen so oft hintereinander kommen, bevor wir sie glauben
# (sonst hält der letzte gute Stand – OpenLigaDB liefert zwischendurch gern mal Müll)
DOWNGRADE_CONFIRMATIONS = 2


def ht_score_key(mid: int) -> str:
    """match_events-Schlüssel für den live mitgeschriebenen Pausenstand."""
    return f"htscore:{mid}"


def _richness(s: MatchSnapshot) -> int:
    return len(s.goals) + len(s.raw.get("matchResults") or [])


def _is_downgrade(old: MatchSnapshot, new: MatchSnapshot) -> str | None:
    """Warum `new` schlechter aussieht als `old` (oder None, wenn alles plausibel ist)."""
    if old.finished and not new.finished:
        return "Spiel plötzlich nicht mehr beendet"
    if old.last_update and new.last_update and new.last_update < old.last_update:
        return f"älterer Datenstand ({new.last_update} < {old.last_update})"
    if old.has_score_data and not new.has_score_data:
        return "keine Ergebnisdaten mehr"
    if sum(new.score) < sum(old.score) and _richness(new) < _richness(old):
        return f"Stand {old.score} → {new.score} mit weniger Daten"
    if sum(new.score) < sum(old.score):
        return f"Stand {old.score} → {new.score}"
    return None


def goal_key(mid: int, score_key: str, prefix: str = "") -> str:
    return f"{prefix}goal:{mid}:{score_key}"


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
    def __init__(self, client: OpenLigaDBClient, bot: discord.Client, publisher: Publisher | None = None,
                 channels=None):
        self.client = client
        self.bot = bot
        self.pub = publisher or Publisher(bot)
        self.channels = channels  # ChannelManager oder None (nur Konferenz)
        self.status = TickerStatus()
        self._wake = asyncio.Event()
        self._force_full_sync = False
        self._marks: dict[tuple, str | None] = {}
        self._last_fetch: dict[tuple, float] = {}
        self._snaps: dict[int, MatchSnapshot] = {}
        self._conf_sig: dict[str, str] = {}
        self._scorer_known: set[str] = set()
        # Sicherheitsnetz gegen Spam: alles, was Discord angenommen hat, wird hier gemerkt –
        # auch wenn das Schreiben in die DB fehlschlägt. key -> (channel_id, message_id)
        self._mem_posted: dict[str, tuple[int, int | None]] = {}
        self._tick_no = 0
        self._suspect: dict[int, int] = {}                 # match_id -> Anzahl verdächtiger Antworten in Folge
        self._ht_live: dict[int, tuple[int, int]] = {}     # live mitgeschriebener Pausenstand
        self._ht_loaded: set[int] = set()
        self._ht_posted: dict[str, tuple[int, int]] = {}   # ht-Event-Key -> gezeigter Stand (für Korrektur-Edit)

    # ── Steuerung ────────────────────────────────────────────────────────────
    def wake(self, full_sync: bool = False) -> None:
        self._force_full_sync = self._force_full_sync or full_sync
        self._wake.set()

    async def run(self) -> None:
        await self.bot.wait_until_ready()
        if self.channels:
            try:
                await self.channels.start()
            except Exception:
                logger.exception("Channel-Verwaltung konnte nicht starten – Ticker läuft nur in der Konferenz")
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

        if self.channels:
            try:
                await self.channels.prepare(now)
            except Exception:
                logger.exception("Spiel-Channels vorbereiten fehlgeschlagen")

        max_dur = timedelta(minutes=settings.match_max_duration_minutes)
        lead = timedelta(minutes=settings.pre_kickoff_wake_minutes)
        async with AsyncSessionLocal() as session:
            window = await get_matches_between(session, now - max_dur, now + lead + timedelta(minutes=1))
        posted = await ev_repo.posted_keys_for([m.match_id for m in window]) | set(self._mem_posted)
        active = [m for m in window if f"ft:{m.match_id}" not in posted
                  or (self._mc(m.match_id) and f"{MC}ft:{m.match_id}" not in posted)]
        self.status.active_matches = len(active)
        self._tick_no += 1
        logger.debug(
            "Tick #%d: %d Spiele im Fenster, %d aktiv (%s), %d bekannte Events",
            self._tick_no, len(window), len(active),
            ", ".join(f"{m.league_id}:{m.match_id}@{m.kickoff_utc:%d.%m. %H:%M}Z" for m in active) or "-",
            len(posted),
        )

        # Spieltag laden (nur wenn sich etwas geändert hat)
        groups = sorted({(m.league_id, m.season, m.matchday) for m in active})
        for g in groups:
            await self._refresh_group(*g, now=now)

        for m in active:
            snap = self._snaps.get(m.match_id)
            if snap is None:
                continue
            try:
                await self._apply_halftime(snap, now)  # auch ohne neuen Abruf (Pausenfenster beginnt)
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
        # mit Spiel-Channels stündlich aufwachen, damit der nächste Spieltag rechtzeitig angelegt wird
        cap = 3600 if self.channels and self.channels.enabled else 6 * 3600
        if not nxt:
            return cap
        secs = (nxt - lead - now).total_seconds()
        return max(30.0, min(secs, cap))

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
            snaps = await sync_matchday(self.client, league, season, day, guard=self._guard)
        except Exception as e:
            logger.warning("Spieltag %s/%s/%s nicht ladbar: %s – letzter Stand bleibt", league, season, day, e)
            return
        self._marks[key] = mark
        self._last_fetch[key] = time.monotonic()
        for s in snaps:
            await self._apply_halftime(s, now)
            self._snaps[s.match_id] = s
        logger.debug("Spieltag %s/%s geladen (%d Spiele, Marke %s)", league, day, len(snaps), mark)

    def _guard(self, snaps: list[MatchSnapshot]) -> list[MatchSnapshot]:
        """Zwischenspeicher gegen API-Aussetzer: schlechtere Daten erst nach Bestätigung übernehmen.

        Wird vor dem Schreiben in die DB aufgerufen, damit auch /befehle den letzten guten Stand sehen.
        """
        out = []
        for new in snaps:
            old = self._snaps.get(new.match_id)
            why = _is_downgrade(old, new) if old else None
            if not why:
                self._suspect.pop(new.match_id, None)
                out.append(new)
                continue
            n = self._suspect.get(new.match_id, 0) + 1
            if n >= DOWNGRADE_CONFIRMATIONS:
                logger.warning("Match %s: %s – %d× bestätigt, übernehme neuen Stand", new.match_id, why, n)
                self._suspect.pop(new.match_id, None)
                out.append(new)
            else:
                self._suspect[new.match_id] = n
                logger.warning("Match %s: API-Antwort verdächtig (%s) – behalte letzten Stand", new.match_id, why)
                out.append(old)
        return out

    async def _apply_halftime(self, snap: MatchSnapshot, now: datetime) -> None:
        """Pausenstand absichern: in der Pause den Live-Stand mitschreiben (Speicher + DB).

        OpenLigaDB lässt "Halbzeit" gern auf 0:0 stehen. Zwischen 50' und 60' nach Anstoß ist Pause –
        was dann als Spielstand gemeldet wird, IST der Pausenstand. Nach einem Neustart kommt er aus der DB.
        """
        mid = snap.match_id
        if mid not in self._ht_live and mid not in self._ht_loaded:
            self._ht_loaded.add(mid)
            try:
                row = await ev_repo.get_event(ht_score_key(mid))
            except Exception:
                logger.exception("DB: Pausenstand %s nicht ladbar", mid)
                row = None
            if row and row.home_score is not None and row.away_score is not None:
                self._ht_live[mid] = (row.home_score, row.away_score)

        if snap.kickoff_utc and not snap.finished and snap.has_score_data:
            elapsed = now - snap.kickoff_utc
            if HALFTIME_AFTER <= elapsed <= HALFTIME_CAPTURE_UNTIL:
                seen = merge_halftime(self._ht_live.get(mid), snap.score, score=snap.score)
                if seen != self._ht_live.get(mid):
                    logger.info("Match %s: Pausenstand live gemerkt %s:%s", mid, *seen)
                    await self._store_ht(mid, seen)

        if mid in self._ht_live:
            live = self._ht_live[mid]
            merged = merge_halftime(live, snap.halftime, score=snap.score)
            if merged != snap.halftime:
                logger.debug("Match %s: Pausenstand API %s → %s", mid, snap.halftime, merged)
            snap.halftime = merged
            if snap.has_score_data and (live[0] > snap.score[0] or live[1] > snap.score[1]) and merged:
                # Tor vor der Pause wurde zurückgenommen (VAR) → gemerkten Stand nicht später wiederbeleben
                logger.info("Match %s: Pausenstand %s:%s zurückgenommen → %s:%s", mid, *live, *merged)
                await self._store_ht(mid, merged)

    async def _store_ht(self, mid: int, ht: tuple[int, int]) -> None:
        self._ht_live[mid] = ht
        try:
            await ev_repo.upsert_event(ht_score_key(mid), mid, "halftime", home_score=ht[0], away_score=ht[1])
        except Exception:
            logger.exception("DB: Pausenstand %s nicht gespeichert – nur im Speicher", mid)

    # ── Events eines Spiels ──────────────────────────────────────────────────
    async def _remember(self, key: str, mid: int, ch: int, msg_id: int | None) -> None:
        """Event merken: erst im Speicher (kann nicht fehlschlagen), dann in der DB."""
        self._mem_posted[key] = (ch, msg_id)
        try:
            await ev_repo.mark_posted(key, mid, ch, msg_id)
        except Exception:
            logger.exception("DB: posted_events '%s' konnte nicht gespeichert werden – nur im Speicher gemerkt", key)

    async def _already(self, key: str) -> bool:
        if key in self._mem_posted:
            return True
        return await ev_repo.is_posted(key)

    async def _posted_ref(self, key: str) -> tuple[int, int | None] | None:
        """(channel_id, message_id) eines geposteten Events – Speicher vor DB."""
        if key in self._mem_posted and self._mem_posted[key][1]:
            return self._mem_posted[key]
        row = await ev_repo.get_posted(key)
        if row:
            return (row.channel_id, row.message_id)
        return self._mem_posted.get(key)

    async def _mark(self, posted: set[str], key: str, mid: int, ch: int, msg: discord.Message | None) -> None:
        await self._remember(key, mid, ch, msg.id if msg else None)
        posted.add(key)
        logger.debug("Event %s erledigt (%s)", key, "gepostet" if msg else "still markiert")

    def _mc(self, match_id: int) -> int | None:
        return self.channels.channel_for(match_id) if self.channels else None

    def _status(self, mid: int, prefix: str, status: str) -> None:
        if prefix == MC and self.channels:
            self.channels.mark_status(mid, status)

    async def _process_match(self, snap: MatchSnapshot, posted: set[str], now: datetime) -> None:
        if not snap.kickoff_utc or now < snap.kickoff_utc:
            return
        logger.debug(
            "Match %s %s–%s: Stand %s, HZ %s, beendet=%s, Tore=%d",
            snap.match_id, snap.home.name, snap.away.name, snap.score, snap.halftime, snap.finished, len(snap.goals),
        )
        # 1) Konferenz-Channel der Liga, 2) eigener Spiel-Channel (falls angelegt)
        await self._process_dest(snap, posted, now, "", settings.ticker_channel_for(snap.league))
        mc = self._mc(snap.match_id)
        if mc:
            await self._process_dest(snap, posted, now, MC, mc)

    async def _process_dest(self, snap: MatchSnapshot, posted: set[str], now: datetime, p: str, ch: int) -> None:
        mid = snap.match_id
        ko_key, ht_key, ft_key = f"{p}ko:{mid}", f"{p}ht:{mid}", f"{p}ft:{mid}"
        # Bot hat ein Spiel erst nach Abpfiff gesehen → nicht nachträglich alles spammen
        catch_up = snap.finished and ko_key not in posted
        # Bot steigt mitten im Spiel ein → EINE Zwischenstand-Karte, Bisheriges still markieren
        late_join = (not snap.finished and ko_key not in posted and now - snap.kickoff_utc > LATE_JOIN)

        if late_join:
            embed = fmt.embed_match_detail(snap)
            embed.title = f"📡 Live dabei: {snap.home.name} – {snap.away.name}"
            embed.description = f"*{sprueche.pick('live_dabei')}*\n{embed.description or ''}"
            msg = await self.pub.send(ch, embed)
            if not msg:
                return
            await self._mark(posted, ko_key, mid, ch, msg)
            for ev in snap.goals:
                key = goal_key(mid, ev.score_key, p)
                if key not in posted:
                    await self._mark(posted, key, mid, ch, None)
                    if ev.scorer:
                        self._scorer_known.add(key)
            # Halbzeit nur still markieren, wenn sie schon vorbei ist – sonst kommt sie noch regulär
            if ht_key not in posted and halftime_reached(snap, now) and not halftime_postable(snap, now):
                await self._mark(posted, ht_key, mid, ch, None)
            self._status(mid, p, "live")
            logger.info("Später Einstieg %s%s (%s – %s, %d:%d): Zwischenstand gepostet, %d Tore still markiert",
                        p, mid, snap.home.name, snap.away.name, *snap.score, len(snap.goals))
            return

        # Anpfiff
        if ko_key not in posted:
            if catch_up:
                await self._mark(posted, ko_key, mid, ch, None)
            else:
                embed, file = await fmt.embed_kickoff(snap)
                msg = await self.pub.send(ch, embed, file)
                if settings.score_cards:
                    await score_card.warm_up(snap)  # Wappen in Kartengröße vorrendern
                if msg:
                    await self._mark(posted, ko_key, mid, ch, msg)
                    self._status(mid, p, "live")
                    logger.info("Anpfiff %s%s: %s – %s", p, mid, snap.home.name, snap.away.name)
                else:
                    return  # Discord-Problem → nächster Tick versucht es erneut

        await self._process_goals(snap, posted, ch, catch_up, p)

        # Halbzeit – OpenLigaDB trägt "Halbzeit 0:0" schon beim Anpfiff ein, daher zeitbasiert
        if ht_key not in posted and halftime_reached(snap, now):
            if not halftime_postable(snap, now):  # beendet oder 2. Hälfte läuft schon lange
                await self._mark(posted, ht_key, mid, ch, None)
            else:
                msg = await self.pub.send(ch, *await fmt.post_halftime(snap))
                if msg:
                    await self._mark(posted, ht_key, mid, ch, msg)
                    self._ht_posted[ht_key] = snap.halftime or snap.score
                    logger.info("Halbzeit %s%s: %s", p, mid, snap.halftime)
        elif ht_key in self._ht_posted and snap.halftime and snap.halftime != self._ht_posted[ht_key]:
            # Tor kurz vor der Pause kam erst nach dem Halbzeit-Post in der API an → Nachricht korrigieren
            ref = await self._posted_ref(ht_key)
            if ref and ref[1] and await self.pub.edit(ref[0] or ch, ref[1], *await fmt.post_halftime(snap)):
                logger.info("Halbzeit %s%s korrigiert: %s → %s", p, mid, self._ht_posted[ht_key], snap.halftime)
            self._ht_posted[ht_key] = snap.halftime

        # Abpfiff
        if snap.finished and ft_key not in posted:
            msg = await self.pub.send(ch, *await fmt.post_fulltime(snap))
            if msg:
                await self._mark(posted, ft_key, mid, ch, msg)
                self._status(mid, p, "ft")
                logger.info("Abpfiff %s%s: %s %d:%d %s", p, mid, snap.home.name, *snap.score, snap.away.name)

    async def _process_goals(self, snap: MatchSnapshot, posted: set[str], ch: int, catch_up: bool,
                             p: str = "") -> None:
        mid = snap.match_id
        prefix = f"{p}goal:{mid}:"
        announced = [k for k in posted if k.startswith(prefix)]
        announced_max = max((_total_of_goal_key(k) for k in announced), default=0)
        current_total = sum(snap.score)

        # Korrektur: Spielstand ist niedriger als bereits gemeldet (VAR / Datenfehler)
        if snap.has_score_data and current_total < announced_max:
            too_high = [k for k in announced if _total_of_goal_key(k) > current_total]
            old = max(too_high, key=_total_of_goal_key).rsplit(":", 1)[1]
            new = f"{snap.score[0]}-{snap.score[1]}"
            corr_key = f"{p}corr:{mid}:{old}>{new}"
            if corr_key not in posted:
                oh, oa = (int(x) for x in old.split("-"))
                msg = await self.pub.send(ch, fmt.embed_goal_correction(snap, (oh, oa), snap.score))
                if msg:
                    await self._mark(posted, corr_key, mid, ch, msg)
            await ev_repo.unmark_posted(too_high)  # gleicher Stand später wieder → neu melden
            posted.difference_update(too_high)
            for k in too_high:  # auch aus dem Speicher-Netz, sonst wird das Tor nie wieder gemeldet
                self._mem_posted.pop(k, None)
                self._scorer_known.discard(k)

        for ev in snap.goals:
            key = goal_key(mid, ev.score_key, p)
            team_id = snap.home.id if ev.side == "home" else snap.away.id if ev.side == "away" else None
            if key in posted:
                if ev.scorer and key not in self._scorer_known:
                    await self._maybe_update_goal(snap, ev, key, ch, team_id)
                    self._scorer_known.add(key)
                continue
            if catch_up:
                await self._mark(posted, key, mid, ch, None)
            else:
                msg = await self.pub.send(ch, *await fmt.post_goal(snap, ev, key))
                if not msg:
                    return  # Reihenfolge wahren – nächster Tick
                await self._mark(posted, key, mid, ch, msg)
                logger.info("Tor %s%s: %s %s (%s)", p, mid, ev.score_key, ev.scorer or "?", ev.minute_label)
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
        ref = await self._posted_ref(key)
        if ref and ref[1]:
            ok = await self.pub.edit(ref[0] or ch, ref[1], *await fmt.post_goal(snap, ev, key))
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
            if await self._already(key):
                continue
            msg = await self.pub.send(ch, fmt.embed_matchday_opener(matches, day))
            if msg:
                await self._remember(key, 0, ch, msg.id)
                logger.info("Tagesübersicht %s in Channel %s gepostet", day, ch)

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
            existing = await self._posted_ref(key)
            if existing and existing[1]:
                if await self.pub.edit(ch, existing[1], embed):
                    self._conf_sig[key] = sig
                    logger.debug("Konferenz %s aktualisiert", key)
                    continue
                if all(s.finished for s in snaps):
                    self._conf_sig[key] = sig  # gelöscht und Tag vorbei → nicht neu posten
                    continue
            msg = await self.pub.send(ch, embed)
            if msg:
                await self._remember(key, 0, ch, msg.id)
                self._conf_sig[key] = sig
                logger.info("Konferenz %s neu gepostet (msg %s)", key, msg.id)

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
            if lg in CUP_LEAGUES:
                continue  # Pokal hat keine Tabelle
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
            if await self._already(key):
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
                await self._remember(key, 0, ch, msg.id)
                logger.info("Tabelle %s gepostet", key)
