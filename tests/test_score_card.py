"""Spielstand-Karten: rendern ohne Fehler, auch ohne Wappen / ohne Torschütze / mit vielen Toren."""
import asyncio
import io

from PIL import Image

from app import formatters as fmt
from app import score_card
from app.matchdata import GoalEvent, parse_match
from tests.fixtures import goal, match, result


def _snap(goals=None, results=None, finished=False):
    return parse_match(match(1, 40, 7, goals=goals or [], results=results or [], finished=finished))


def _png(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    assert img.format == "PNG" and img.width == score_card.W
    return img


def test_goal_card_with_and_without_scorer():
    s = _snap([goal(1, 0, 1, 30, "Guirassy")], [result(2, 0, 1)])
    with_scorer = _png(score_card.render_goal(s, s.goals[0], "1. Bundesliga · 10. Spieltag"))
    without = _png(score_card.render_goal(s, GoalEvent(0, 1, "away"), "1. Bundesliga"))
    assert without.height < with_scorer.height  # Torschütze-Leiste fällt weg


def test_halftime_lists_only_first_half_goals():
    goals = [goal(1, 1, 0, 10, "A"), goal(2, 1, 1, 40, "B"), goal(3, 2, 1, 70, "C")]
    s = _snap(goals, [result(1, 1, 1), result(2, 2, 1)])
    one = _png(score_card.render_halftime(s, "x"))
    s2 = _snap(goals[:2] + [goal(3, 2, 1, 44, "C")], [result(1, 2, 1), result(2, 2, 1)])
    _png(score_card.render_halftime(s2, "x"))
    assert one.height > 300


def test_fulltime_many_goals_is_capped():
    goals = [goal(i, i, 0, i * 8, f"Spieler {i}") for i in range(1, 11)]
    s = _snap(goals, [result(1, 5, 0), result(2, 10, 0)], finished=True)
    img = _png(score_card.render_fulltime(s, "1. Bundesliga"))
    assert img.height < 700


def test_card_never_raises():
    """Fehler beim Rendern → None (Ticker fällt auf Text-Embed zurück), keine Exception."""
    assert asyncio.run(score_card.card("goal", _snap(), "x", None)) is None


def test_post_goal_builds_card_embed(monkeypatch):
    monkeypatch.setattr(fmt.settings, "score_cards", True)
    s = _snap([goal(1, 1, 0, 12, "Kane")], [result(2, 1, 0)])
    e, f = asyncio.run(fmt.post_goal(s, s.goals[0], "k"))
    assert f is not None and e.image == f"attachment://{f.filename}" and e.title.endswith("1:0")
    monkeypatch.setattr(fmt.settings, "score_cards", False)
    e, f = asyncio.run(fmt.post_goal(s, s.goals[0], "k"))
    assert f is None and e.fields
