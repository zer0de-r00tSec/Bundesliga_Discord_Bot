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


def _pokal_raw():
    # 1:1 nach 90, 2:2 nach Verlängerung, 4:3 im Elfmeterschießen
    goals = [goal(1, 1, 0, 20, "A"), goal(2, 1, 1, 70, "B"), goal(3, 2, 1, 100, "C", ot=True),
             goal(4, 2, 2, 118, "D", ot=True)]
    shootout = [goal(10 + i, h, a, None, f"E{i}", pen=True)
                for i, (h, a) in enumerate([(1, 0), (1, 1), (2, 1), (2, 2), (3, 2), (3, 3), (4, 3)])]
    res = [result(1, 1, 0), result(2, 1, 1),
           {**result(4, 2, 2), "resultName": "Verlängerung"}, {**result(5, 4, 3), "resultName": "Elfmeterschießen"}]
    return match(league="dfb", goals=goals + shootout, results=res, finished=True)


def test_cup_extra_time_and_penalties():
    snap = parse_match(_pokal_raw(), "dfb", 2026)
    assert snap.score == (2, 2) and snap.extra_time and snap.penalties == (4, 3)
    assert [g.score_key for g in snap.goals] == ["1-0", "1-1", "2-1", "2-2"]


def test_cup_shootout_live_without_result_is_ignored():
    raw = _pokal_raw()
    raw["matchResults"] = raw["matchResults"][:2]  # Ergebnisse noch nicht eingetragen
    raw["matchIsFinished"] = False
    snap = parse_match(raw, "dfb", 2026)
    assert snap.score == (2, 2) and len(snap.goals) == 4


def test_league_penalty_without_minute_still_counts():
    raw = match(goals=[goal(1, 1, 0, None, "Kane", pen=True)])
    assert parse_match(raw, "bl1", 2026).score == (1, 0)


def test_cup_fulltime_embed():
    from app import formatters as fmt
    snap = parse_match(_pokal_raw(), "dfb", 2026)
    e = fmt.embed_fulltime(snap)
    assert "2 : 2 i.E." in e.description and "4:3" in e.description and "Runde weiter" in e.description
    assert "FC Bayern" in e.description.split("🏆")[-1]


def test_sprueche_yaml_valid_and_used():
    import yaml
    from pathlib import Path
    from app import formatters as fmt, sprueche
    data = yaml.safe_load((Path(sprueche.__file__).parent / "assets" / "sprueche.yaml").read_text(encoding="utf-8"))
    for k in ("tor", "anpfiff", "halbzeit", "abpfiff_sieg", "willkommen", "elfmeter", "eigentor", "pokal_weiter"):
        assert len(data[k]) >= 6, k
    snap = parse_match(live_match(), "bl1", 2026)
    e1 = fmt.embed_goal(snap, snap.goals[1], "goal:1:1-1")
    e2 = fmt.embed_goal(snap, snap.goals[1], "goal:1:1-1")
    assert e1.description == e2.description  # gleicher Spruch beim Editieren
    assert "{" not in e1.description
    assert "Max" in sprueche.pick("willkommen", name="Max")
