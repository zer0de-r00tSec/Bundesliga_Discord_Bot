from app.matchdata import current_score, goal_events, parse_match
from tests.fixtures import goal, live_match, match, result


def test_live_match_goals_and_sides():
    snap = parse_match(live_match(), "bl1", 2026)
    assert snap.score == (1, 1)
    assert snap.halftime == (1, 1)
    assert [(g.score_key, g.side, g.scorer) for g in snap.goals] == [
        ("1-0", "home", "Harry Kane"), ("1-1", "away", "Serhou Guirassy")]
    assert snap.matchday == 10 and snap.league == "bl1"


def test_score_only_from_results_creates_synthetic_goals():
    raw = match(results=[result(2, 2, 0)])
    evs = goal_events(raw)
    assert [(e.score_key, e.side, e.synthetic) for e in evs] == [("1-0", "home", True), ("2-0", "home", True)]


def test_result_ahead_of_goal_list():
    raw = match(goals=[goal(1, 1, 0, 10, "A")], results=[result(2, 1, 1)])
    evs = goal_events(raw)
    assert [(e.score_key, e.side, e.synthetic) for e in evs] == [("1-0", "home", False), ("1-1", "away", True)]


def test_finished_uses_final_result():
    raw = match(goals=[goal(1, 1, 0, 10, "A")], results=[result(1, 1, 0), result(2, 3, 0)], finished=True)
    assert current_score(raw) == ((3, 0), True)


def test_duplicate_goal_entries_keep_named_one():
    raw = match(goals=[goal(1, 1, 0, None, None), goal(2, 1, 0, 17, "Musiala")])
    evs = goal_events(raw)
    assert len(evs) == 1 and evs[0].scorer == "Musiala" and evs[0].minute == 17


def test_minute_label_stoppage_and_tags():
    raw = match(goals=[goal(1, 0, 1, 93, "X", pen=True)])
    ev = goal_events(raw)[0]
    assert ev.minute_label == "90+3'" and "Elfmeter" in ev.tags and ev.side == "away"


def test_no_data_before_kickoff():
    snap = parse_match(match(), "bl1", 2026)
    assert snap.score == (0, 0) and not snap.has_score_data and snap.goals == []


def test_invalid_match_returns_none():
    assert parse_match({"matchID": 1}) is None
