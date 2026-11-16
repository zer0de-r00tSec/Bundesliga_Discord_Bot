"""Realistische OpenLigaDB-Match-Objekte für Tests."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta

TEAMS = {
    40: ("FC Bayern München", "Bayern"),
    7: ("Borussia Dortmund", "Dortmund"),
    6: ("Bayer 04 Leverkusen", "Leverkusen"),
    1635: ("RB Leipzig", "Leipzig"),
    16: ("VfB Stuttgart", "Stuttgart"),
    91: ("Eintracht Frankfurt", "Frankfurt"),
}


def team(tid: int) -> dict:
    name, short = TEAMS.get(tid, (f"Team {tid}", f"T{tid}"))
    return {"teamId": tid, "teamName": name, "shortName": short,
            "teamIconUrl": f"https://example.invalid/{tid}.png", "teamGroupName": None}


def goal(gid, s1, s2, minute, name, **flags) -> dict:
    return {"goalID": gid, "scoreTeam1": s1, "scoreTeam2": s2, "matchMinute": minute,
            "goalGetterID": gid * 10, "goalGetterName": name,
            "isPenalty": flags.get("pen", False), "isOwnGoal": flags.get("og", False),
            "isOvertime": flags.get("ot", False), "comment": None}


def result(type_id, p1, p2) -> dict:
    return {"resultID": 1000 + type_id, "resultName": "Endergebnis" if type_id == 2 else "Halbzeitergebnis",
            "pointsTeam1": p1, "pointsTeam2": p2, "resultOrderID": type_id, "resultTypeID": type_id,
            "resultDescription": ""}


def match(mid=70001, home=40, away=7, kickoff: datetime | None = None, day=10, league="bl1",
          goals=None, results=None, finished=False) -> dict:
    kickoff = kickoff or datetime(2026, 11, 14, 14, 30)
    return {
        "matchID": mid,
        "matchDateTime": (kickoff + timedelta(hours=1)).isoformat(),
        "timeZoneID": "W. Europe Standard Time",
        "leagueId": 5000,
        "leagueName": "1. Fußball-Bundesliga 2026/2027",
        "leagueSeason": 2026,
        "leagueShortcut": league,
        "matchDateTimeUTC": kickoff.isoformat() + "Z",
        "group": {"groupName": f"{day}. Spieltag", "groupOrderID": day, "groupID": 50000 + day},
        "team1": team(home),
        "team2": team(away),
        "lastUpdateDateTime": "2026-11-14T16:20:31.93",
        "matchIsFinished": finished,
        "matchResults": results or [],
        "goals": goals or [],
        "location": {"locationID": 1, "locationCity": "München", "locationStadium": "Allianz Arena"},
        "numberOfViewers": 75000 if finished else None,
    }


def live_match() -> dict:
    return match(goals=[goal(1, 1, 0, 12, "Harry Kane"), goal(2, 1, 1, 34, "Serhou Guirassy")],
                 results=[result(1, 1, 1)])


def clone(d: dict) -> dict:
    return copy.deepcopy(d)
