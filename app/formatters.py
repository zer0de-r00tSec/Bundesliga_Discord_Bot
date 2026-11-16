"""Alle Discord-Embeds. Ticker-Embeds bekommen einen MatchSnapshot (API-Stand),
Listen-Embeds ORM-Matches aus der DB."""
from __future__ import annotations

import random
from datetime import date, datetime
from pathlib import Path

import discord

from app.config import LEAGUE_NAMES, LEAGUE_SHORT
from app.image_composer import compose_vs_image
from app.matchdata import GoalEvent, MatchSnapshot
from app.timeutil import aware_utc, discord_ts, fmt_local, utcnow

FOOTER = "Stadion-Echo | BL-Ticker"

COLOR_DEFAULT = 0x1A1A2E
COLOR_KICKOFF = 0x3498DB
COLOR_GOAL = 0xF1C40F
COLOR_HT = 0xE67E22
COLOR_FT = 0x2ECC71
COLOR_WARN = 0xE74C3C
COLOR_LIVE = 0xC0392B

# Schlüsselwort im (kleingeschriebenen) Teamnamen → Vereinsfarbe
TEAM_COLORS: list[tuple[str, int]] = [
    ("bayern", 0xDC052D), ("dortmund", 0xFDE100), ("leverkusen", 0xE32221),
    ("leipzig", 0xDD0741), ("stuttgart", 0xE32219), ("frankfurt", 0xE1000F),
    ("freiburg", 0xEE1F23), ("köln", 0xED1C24), ("gladbach", 0x1B1B1B),
    ("werder", 0x1D9053), ("hamburger", 0x0A3F86), ("schalke", 0x004D9F),
    ("hertha", 0x005CA9), ("mainz", 0xC8102E), ("wolfsburg", 0x65B32E),
    ("hoffenheim", 0x1961AC), ("augsburg", 0xBA3733), ("bochum", 0x005CA9),
    ("düsseldorf", 0xE30613), ("hannover", 0x009639), ("union", 0xEB1923),
    ("pauli", 0x624839), ("heidenheim", 0xE2001A), ("kiel", 0x0E3F8F),
    ("nürnberg", 0xAA1124), ("karlsruher", 0x00559F), ("darmstadt", 0x004E9E),
    ("paderborn", 0x005EA8), ("magdeburg", 0x0067B1), ("bielefeld", 0x004E95),
    ("kaiserslautern", 0xE2001A), ("fürth", 0x00973B), ("elversberg", 0x000000),
    ("ulm", 0x111111), ("preußen", 0x00843D), ("braunschweig", 0xF4C400),
    ("regensburg", 0xD40511), ("dresden", 0xFFD400), ("münster", 0x00843D),
]

GOAL_LINES_FILE = Path(__file__).resolve().parent / "assets" / "goal_lines.txt"
DEFAULT_GOAL_LINES = [
    "Der Keeper hatte keine Chance!", "Was für ein Einschlag!", "Eiskalt vollendet!",
    "Das Stadion bebt!", "Unhaltbar ins Eck!", "Da ist das Ding drin!",
]
OPENERS = [
    "Der Rasen ist bereit. Bist du es? ⚽",
    "Die Kurve brennt. Es geht los! 🔥",
    "Heute wird Fußball gespielt – alles andere ist egal. 🏟️",
    "Anpfiff naht. Hol dir ein Kaltgetränk. 🍺",
]


def _goal_lines() -> list[str]:
    try:
        lines = [l.strip() for l in GOAL_LINES_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        return lines or DEFAULT_GOAL_LINES
    except OSError:
        return DEFAULT_GOAL_LINES


def team_color(team_name: str | None, default: int = COLOR_GOAL) -> int:
    n = (team_name or "").lower()
    for key, color in TEAM_COLORS:
        if key in n:
            return color
    return default


def league_label(league: str, matchday: int | None = None, short: bool = False) -> str:
    name = (LEAGUE_SHORT if short else LEAGUE_NAMES).get(league, league.upper())
    return f"{name} · {matchday}. Spieltag" if matchday else name


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _base(title: str, color: int, description: str | None = None, ts: datetime | None = None) -> discord.Embed:
    e = discord.Embed(title=_cut(title, 256), color=color, description=_cut(description, 4096) if description else None,
                      timestamp=aware_utc(ts or utcnow()))
    e.set_footer(text=FOOTER)
    return e


def _nm(team, short: bool = False) -> str:
    """Name aus TeamInfo (Snapshot) oder Team (ORM)."""
    if team is None:
        return "?"
    if short:
        s = getattr(team, "short", None) or getattr(team, "short_name", None)
        if s:
            return s
    return getattr(team, "name", "?")


def goals_summary(snap: MatchSnapshot, limit: int = 1024) -> str:
    lines = []
    for g in snap.goals:
        who = g.scorer or "unbekannt"
        team = snap.home.name if g.side == "home" else snap.away.name if g.side == "away" else ""
        lines.append(f"`{g.minute_label:>6}` **{g.home}:{g.away}** {who}{g.tags}" + (f" · {team}" if team else ""))
    return _cut("\n".join(lines), limit) if lines else "Keine Tore."


# ── Ticker-Embeds ────────────────────────────────────────────────────────────

async def embed_kickoff(snap: MatchSnapshot) -> tuple[discord.Embed, discord.File | None]:
    ko_local = fmt_local(snap.kickoff_utc, "%d.%m.%Y %H:%M")
    e = _base(
        "🟢 Anpfiff!",
        COLOR_KICKOFF,
        f"## {snap.home.name} – {snap.away.name}",
        snap.kickoff_utc,
    )
    e.add_field(name="🏆 Wettbewerb", value=league_label(snap.league, snap.matchday), inline=True)
    if snap.stadium or snap.city:
        e.add_field(name="🏟 Stadion", value=" · ".join(x for x in (snap.stadium, snap.city) if x), inline=True)
    img = await compose_vs_image(
        snap.home.id, snap.away.id, snap.home.name, snap.away.name, ko_local, league_label(snap.league, short=True)
    )
    file = None
    if img:
        file = discord.File(img, filename=f"vs_{snap.match_id}.png")
        e.set_image(url=f"attachment://vs_{snap.match_id}.png")
    return e, file


def embed_goal(snap: MatchSnapshot, ev: GoalEvent, event_key: str = "") -> discord.Embed:
    team = snap.home if ev.side == "home" else snap.away if ev.side == "away" else None
    line = random.Random(event_key or f"{snap.match_id}{ev.score_key}").choice(_goal_lines())
    title = f"⚽ TOOOR für {team.name}!" if team else "⚽ TOOOR!"
    desc = f"*{line}*\n## {snap.home.name}  {ev.home} : {ev.away}  {snap.away.name}"
    e = _base(title, team_color(team.name if team else None), desc)
    if ev.scorer:
        scorer = f"{ev.scorer}{ev.tags}"
    else:
        scorer = "*wird nachgetragen …*"
    e.add_field(name="🎯 Torschütze", value=scorer, inline=True)
    e.add_field(name="⏱ Minute", value=ev.minute_label if ev.minute is not None else "–", inline=True)
    e.add_field(name="🏆", value=league_label(snap.league, snap.matchday, short=True), inline=True)
    return e


def embed_goal_correction(snap: MatchSnapshot, old: tuple[int, int], new: tuple[int, int]) -> discord.Embed:
    return _base(
        "⚠️ Korrektur – Tor zurückgenommen",
        COLOR_WARN,
        f"{snap.home.name} – {snap.away.name}\n"
        f"Spielstand korrigiert: ~~{old[0]}:{old[1]}~~ → **{new[0]}:{new[1]}**\n"
        f"*(VAR-Entscheidung oder Datenkorrektur)*",
    )


def embed_halftime(snap: MatchSnapshot) -> discord.Embed:
    h, a = snap.halftime or snap.score
    e = _base("⏸ Halbzeit", COLOR_HT, f"## {snap.home.name}  {h} : {a}  {snap.away.name}")
    if snap.goals:
        e.add_field(name="⚽ Tore", value=goals_summary(snap), inline=False)
    e.add_field(name="🏆", value=league_label(snap.league, snap.matchday, short=True), inline=False)
    return e


def embed_fulltime(snap: MatchSnapshot) -> discord.Embed:
    h, a = snap.score
    if h > a:
        result, color = f"🏆 **{snap.home.name}** gewinnt!", team_color(snap.home.name, COLOR_FT)
    elif a > h:
        result, color = f"🏆 **{snap.away.name}** gewinnt!", team_color(snap.away.name, COLOR_FT)
    else:
        result, color = "🤝 Unentschieden!", COLOR_FT
    e = _base("🏁 Abpfiff!", color, f"## {snap.home.name}  {h} : {a}  {snap.away.name}\n{result}")
    if snap.halftime:
        e.add_field(name="⏸ Halbzeit", value=f"{snap.halftime[0]}:{snap.halftime[1]}", inline=True)
    if snap.viewers:
        e.add_field(name="👥 Zuschauer", value=f"{snap.viewers:,}".replace(",", "."), inline=True)
    e.add_field(name="🏆", value=league_label(snap.league, snap.matchday, short=True), inline=True)
    if snap.goals:
        e.add_field(name="⚽ Tore", value=goals_summary(snap), inline=False)
    return e


def embed_matchday_opener(matches: list, day: date | None = None) -> discord.Embed:
    title = "🏟 Spieltag – Los geht's!"
    if day:
        title = f"🏟 Fußball am {day.strftime('%d.%m.%Y')} – Los geht's!"
    e = _base(title, COLOR_DEFAULT, random.choice(OPENERS))
    leagues: dict[str, list] = {}
    for m in matches:
        leagues.setdefault(m.league_id, []).append(m)
    for lg, lm in sorted(leagues.items()):
        lines = [
            f"{discord_ts(m.kickoff_utc, 't')} · {_nm(m.home_team)} – {_nm(m.away_team)}"
            for m in sorted(lm, key=lambda x: x.kickoff_utc)
        ]
        e.add_field(name=f"🏆 {league_label(lg, lm[0].matchday)}", value=_cut("\n".join(lines), 1024), inline=False)
    return e


def _live_status(snap: MatchSnapshot, now: datetime) -> tuple[str, str]:
    """(Icon, Status-Text) für Konferenz/Listen. OpenLigaDB hat keine Live-Uhr → geschätzt."""
    if snap.finished:
        return "✅", "Ende"
    if not snap.kickoff_utc or now < snap.kickoff_utc:
        return "⏳", fmt_local(snap.kickoff_utc, "%H:%M")
    elapsed = int((now - snap.kickoff_utc).total_seconds() // 60)
    if elapsed <= 47:
        return "🔴", f"~{max(elapsed, 1)}'"
    if elapsed <= 62 and (snap.halftime or elapsed <= 60):
        return "⏸", "HZ"
    minute = elapsed - 15
    return "🔴", f"~{min(minute, 90)}'" if minute <= 90 else "90+"


def embed_conference(snaps: list[MatchSnapshot], day: date, now: datetime | None = None) -> discord.Embed:
    now = now or utcnow()
    live = sum(1 for s in snaps if not s.finished and s.kickoff_utc and s.kickoff_utc <= now)
    title = f"📡 Live-Konferenz · {day.strftime('%d.%m.%Y')}"
    desc = f"**{live} Spiel(e) live** – wird automatisch aktualisiert." if live else "Aktuell kein Spiel live."
    e = _base(title, COLOR_LIVE if live else COLOR_DEFAULT, desc)
    by_lg: dict[str, list[MatchSnapshot]] = {}
    for s in snaps:
        by_lg.setdefault(s.league, []).append(s)
    for lg, items in sorted(by_lg.items()):
        lines = []
        for s in sorted(items, key=lambda x: (x.kickoff_utc or now, x.match_id)):
            icon, status = _live_status(s, now)
            started = s.kickoff_utc and s.kickoff_utc <= now
            score = f"**{s.score[0]}:{s.score[1]}**" if started else "– : –"
            line = f"{icon} `{status:>5}` {_nm(s.home, True)} {score} {_nm(s.away, True)}"
            if started and s.goals and not s.finished:
                g = s.goals[-1]
                line += f" · ⚽ {g.scorer or '?'} {g.minute_label if g.minute is not None else ''}".rstrip()
            lines.append(line)
        e.add_field(name=f"🏆 {league_label(lg, items[0].matchday)}", value=_cut("\n".join(lines), 1024), inline=False)
    return e


# ── Listen-/Info-Embeds ──────────────────────────────────────────────────────

def embed_match_list(matches, title: str, empty: str = "Keine Spiele gefunden.") -> discord.Embed:
    e = _base(title, COLOR_DEFAULT)
    if not matches:
        e.description = empty
        return e
    now = utcnow()
    lines = []
    for m in matches:
        home, away = _nm(m.home_team), _nm(m.away_team)
        if m.home_score is not None and m.away_score is not None and m.kickoff_utc <= now:
            live = "" if m.is_finished else " 🔴"
            mid = f"**{m.home_score}:{m.away_score}**{live}"
        else:
            mid = "vs"
        lines.append(f"{discord_ts(m.kickoff_utc, 'f')}\n└ {home} {mid} {away}")
    e.description = _cut("\n".join(lines), 4096)
    return e


SEPARATORS = {"bl1": (4, 15, 16), "bl2": (2, 3, 15, 16)}


def embed_table(rows: list[dict], league: str, highlight_team_id: int | None = None, limit: int = 18) -> discord.Embed:
    e = _base(f"📊 Tabelle – {LEAGUE_NAMES.get(league, league)}", COLOR_DEFAULT)
    if not rows:
        e.description = "Keine Tabellendaten."
        return e
    n = len(rows)
    lines = ["```", f"{'#':>2} {'Verein':<18}{'Sp':>3}{'TD':>5}{'Pkt':>5}"]
    for i, r in enumerate(rows[:limit], 1):
        name = (r.get("shortName") or r.get("teamName") or "?")[:18]
        gd = r.get("goalDiff") or 0
        mark = "»" if highlight_team_id and r.get("teamInfoId") == highlight_team_id else " "
        lines.append(f"{i:>2}{mark}{name:<18}{r.get('matches', 0):>3}{gd:>+5}{r.get('points', 0):>5}")
        if n >= 18 and i in SEPARATORS.get(league, ()):
            lines.append("-" * 33)
    lines.append("```")
    e.description = "\n".join(lines)
    return e


def embed_goalgetters(getters: list[dict], league: str, top: int) -> discord.Embed:
    e = _base(f"🥅 Torjäger – {LEAGUE_NAMES.get(league, league)}", COLOR_DEFAULT)
    ordered = sorted(getters, key=lambda g: g.get("goalCount") or 0, reverse=True)[:top]
    lines = [f"`{i:>2}.` **{g.get('goalGetterName') or 'Unbekannt'}** – {g.get('goalCount') or 0} Tore"
             for i, g in enumerate(ordered, 1)]
    e.description = _cut("\n".join(lines), 4096) or "Keine Daten."
    return e


def embed_match_detail(snap: MatchSnapshot) -> discord.Embed:
    now = utcnow()
    icon, status = _live_status(snap, now)
    started = snap.kickoff_utc and snap.kickoff_utc <= now
    score = f"{snap.score[0]} : {snap.score[1]}" if started else "– : –"
    e = _base(f"{icon} {snap.home.name} – {snap.away.name}", COLOR_LIVE if icon == "🔴" else COLOR_DEFAULT,
              f"## {score}\n{status if started else 'Anstoß ' + discord_ts(snap.kickoff_utc, 'F')}")
    e.add_field(name="🏆", value=league_label(snap.league, snap.matchday), inline=True)
    if snap.stadium:
        e.add_field(name="🏟", value=snap.stadium, inline=True)
    if snap.halftime and started:
        e.add_field(name="⏸ HZ", value=f"{snap.halftime[0]}:{snap.halftime[1]}", inline=True)
    if snap.goals:
        e.add_field(name="⚽ Tore", value=goals_summary(snap), inline=False)
    return e
