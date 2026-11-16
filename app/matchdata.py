"""Reine Parsing-Logik für OpenLigaDB-Match-JSON (ohne DB/Discord – gut testbar).

Struktur eines Spiels (gekürzt):
  matchID, matchDateTimeUTC, matchIsFinished, leagueShortcut, leagueSeason,
  group{groupOrderID, groupName}, team1/team2{teamId, teamName, shortName, teamIconUrl},
  matchResults[{resultTypeID (1=Halbzeit, 2=Endergebnis), pointsTeam1, pointsTeam2, resultName}],
  goals[{goalID, scoreTeam1, scoreTeam2, matchMinute, goalGetterName, isPenalty, isOwnGoal, isOvertime}],
  location{locationCity, locationStadium}, lastUpdateDateTime
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.timeutil import parse_api_dt

HALFTIME_TYPE = 1
FINAL_TYPE = 2


@dataclass
class GoalEvent:
    home: int
    away: int
    side: str | None = None  # "home" | "away" | None (unbekannt)
    minute: int | None = None
    scorer: str | None = None
    penalty: bool = False
    own_goal: bool = False
    overtime: bool = False
    goal_id: int | None = None
    synthetic: bool = False  # nur aus Spielstand abgeleitet, noch ohne Tor-Details

    @property
    def score_key(self) -> str:
        return f"{self.home}-{self.away}"

    @property
    def total(self) -> int:
        return self.home + self.away

    @property
    def minute_label(self) -> str:
        if self.minute is None:
            return "–"
        if self.minute > 90 and not self.overtime:
            return f"90+{self.minute - 90}'"
        return f"{self.minute}'"

    @property
    def tags(self) -> str:
        t = []
        if self.penalty:
            t.append("Elfmeter")
        if self.own_goal:
            t.append("Eigentor")
        if self.overtime:
            t.append("Verlängerung")
        return f" ({', '.join(t)})" if t else ""


@dataclass
class TeamInfo:
    id: int
    name: str
    short: str | None = None
    icon_url: str | None = None


@dataclass
class MatchSnapshot:
    match_id: int
    league: str
    season: int
    matchday: int
    group_name: str | None
    kickoff_utc: datetime | None
    home: TeamInfo
    away: TeamInfo
    finished: bool
    score: tuple[int, int]
    has_score_data: bool
    halftime: tuple[int, int] | None
    goals: list[GoalEvent] = field(default_factory=list)
    stadium: str | None = None
    city: str | None = None
    viewers: int | None = None
    last_update: datetime | None = None
    raw: dict = field(default_factory=dict)


def _int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _result(raw: dict, type_id: int) -> tuple[int, int] | None:
    for r in raw.get("matchResults") or []:
        is_type = r.get("resultTypeID") == type_id
        name = (r.get("resultName") or "").lower()
        if not is_type and type_id == HALFTIME_TYPE and "halbzeit" in name:
            is_type = True
        if is_type:
            h, a = _int(r.get("pointsTeam1")), _int(r.get("pointsTeam2"))
            if h is not None and a is not None:
                return h, a
    return None


def halftime_result(raw: dict) -> tuple[int, int] | None:
    return _result(raw, HALFTIME_TYPE)


def final_result(raw: dict) -> tuple[int, int] | None:
    return _result(raw, FINAL_TYPE)


def _sorted_goals(raw: dict) -> list[dict]:
    goals = [g for g in (raw.get("goals") or []) if _int(g.get("scoreTeam1")) is not None
             and _int(g.get("scoreTeam2")) is not None]
    return sorted(
        goals,
        key=lambda g: (
            _int(g["scoreTeam1"]) + _int(g["scoreTeam2"]),
            _int(g.get("matchMinute")) if _int(g.get("matchMinute")) is not None else 999,
            _int(g.get("goalID")) or 0,
        ),
    )


def current_score(raw: dict) -> tuple[tuple[int, int], bool]:
    """(Spielstand, ob überhaupt Daten vorliegen).

    Bei beendeten Spielen ist das Endergebnis maßgeblich. Live wird der Stand mit
    den meisten Toren genommen (Tore werden manchmal vor/nach dem Ergebnis gepflegt).
    """
    final = final_result(raw)
    if raw.get("matchIsFinished") and final:
        return final, True

    candidates: list[tuple[int, int]] = []
    goals = _sorted_goals(raw)
    if goals:
        last = goals[-1]
        candidates.append((_int(last["scoreTeam1"]), _int(last["scoreTeam2"])))
    if final:
        candidates.append(final)
    ht = halftime_result(raw)
    if ht:
        candidates.append(ht)
    if not candidates:
        return (0, 0), False
    # stabil: bei Gleichstand gewinnt die erste Quelle (Tore vor Ergebnis)
    best = candidates[0]
    for c in candidates[1:]:
        if sum(c) > sum(best):
            best = c
    return best, True


def goal_events(raw: dict) -> list[GoalEvent]:
    """Alle Tore chronologisch, inkl. Seite (wer hat getroffen).

    Ist der Spielstand höher als die erfassten Tore, wird ein 'synthetisches' Tor
    angehängt – so wird das Tor sofort gemeldet und später mit Schütze ergänzt.
    """
    events: list[GoalEvent] = []
    by_score: dict[tuple[int, int], GoalEvent] = {}
    prev = (0, 0)
    for g in _sorted_goals(raw):
        h, a = _int(g["scoreTeam1"]), _int(g["scoreTeam2"])
        if (h, a) == prev and not events:
            continue  # 0:0-"Tor" o.ä. Datenmüll
        side = None
        if h == prev[0] + 1 and a == prev[1]:
            side = "home"
        elif a == prev[1] + 1 and h == prev[0]:
            side = "away"
        scorer = (g.get("goalGetterName") or "").strip() or None
        ev = GoalEvent(
            home=h,
            away=a,
            side=side,
            minute=_int(g.get("matchMinute")),
            scorer=scorer,
            penalty=bool(g.get("isPenalty")),
            own_goal=bool(g.get("isOwnGoal")),
            overtime=bool(g.get("isOvertime")),
            goal_id=_int(g.get("goalID")),
        )
        existing = by_score.get((h, a))
        if existing:  # doppelt erfasst: Version mit mehr Infos behalten
            if not existing.scorer and ev.scorer:
                existing.scorer, existing.minute = ev.scorer, ev.minute or existing.minute
                existing.penalty, existing.own_goal = ev.penalty, ev.own_goal
            continue
        by_score[(h, a)] = ev
        events.append(ev)
        prev = (h, a)

    (sh, sa), has = current_score(raw)
    if has and sh + sa > sum(prev) and sh >= prev[0] and sa >= prev[1]:
        side = None
        if sh > prev[0] and sa == prev[1]:
            side = "home"
        elif sa > prev[1] and sh == prev[0]:
            side = "away"
        # Mehrere fehlende Tore einer Seite: Zwischenstände einzeln melden
        if side == "home":
            for h in range(prev[0] + 1, sh + 1):
                events.append(GoalEvent(home=h, away=sa, side="home", synthetic=True))
        elif side == "away":
            for a in range(prev[1] + 1, sa + 1):
                events.append(GoalEvent(home=sh, away=a, side="away", synthetic=True))
        else:
            events.append(GoalEvent(home=sh, away=sa, side=None, synthetic=True))
    return events


def parse_match(raw: dict, league_fallback: str | None = None, season_fallback: int | None = None) -> MatchSnapshot | None:
    mid = _int(raw.get("matchID"))
    t1, t2 = raw.get("team1") or {}, raw.get("team2") or {}
    hid, aid = _int(t1.get("teamId")), _int(t2.get("teamId"))
    if not mid or not hid or not aid:
        return None
    group = raw.get("group") or {}
    loc = raw.get("location") or {}
    score, has = current_score(raw)
    return MatchSnapshot(
        match_id=mid,
        league=(raw.get("leagueShortcut") or league_fallback or "").lower(),
        season=_int(raw.get("leagueSeason")) or season_fallback or 0,
        matchday=_int(group.get("groupOrderID")) or 0,
        group_name=group.get("groupName"),
        kickoff_utc=parse_api_dt(raw.get("matchDateTimeUTC")),
        home=TeamInfo(hid, t1.get("teamName") or "Heim", t1.get("shortName"), t1.get("teamIconUrl")),
        away=TeamInfo(aid, t2.get("teamName") or "Gast", t2.get("shortName"), t2.get("teamIconUrl")),
        finished=bool(raw.get("matchIsFinished")),
        score=score,
        has_score_data=has,
        halftime=halftime_result(raw),
        goals=goal_events(raw),
        stadium=loc.get("locationStadium"),
        city=loc.get("locationCity"),
        viewers=_int(raw.get("numberOfViewers")),
        last_update=parse_api_dt(raw.get("lastUpdateDateTime"), assume_utc=False),  # Ortszeit
        raw=raw,
    )
