"""Spielstand-Karten (Tor / Halbzeit / Abpfiff) als Bild für die Ticker-Embeds.

Discord kann im Embed keine Schriftfarben – im Bild schon: großer Spielstand, die Zahl des
Torschützen-Teams in Gold, Wappen links/rechts, darunter Torschütze bzw. alle Tore.
Erzeugt wird im Thread (ca. 30–60 ms), die Wappen kommen aus dem Cache des image_composer.
Abschaltbar mit SCORE_CARDS=false – dann gibt es die reinen Text-Embeds wie bisher.
"""
from __future__ import annotations

import asyncio
import dataclasses
import io
import logging

from PIL import Image, ImageDraw, ImageFilter

from app.image_composer import _crest, _font, _placeholder
from app.matchdata import GoalEvent, MatchSnapshot

logger = logging.getLogger(__name__)

W = 1000
CREST = 170
CARD_BG = (30, 31, 34, 255)     # etwas dunkler als das Discord-Embed
PILL = (43, 45, 49, 255)
WHITE = (242, 243, 245, 255)
GRAY = (148, 155, 164, 255)
DIM = (90, 94, 102, 255)
GOLD = (255, 196, 0, 255)
ORANGE = (230, 126, 34, 255)
GREEN = (46, 204, 113, 255)

HEAD = {"goal": ("TOR!", GOLD), "halftime": ("HALBZEIT", ORANGE), "fulltime": ("ABPFIFF", GREEN)}
MAX_GOAL_LINES = 6


# ── Bausteine ────────────────────────────────────────────────────────────────

def _fit(d: ImageDraw.ImageDraw, text: str, max_w: int, start: int, bold: bool = True):
    size = start
    while size > 16 and d.textlength(text, font=_font(size, bold)) > max_w:
        size -= 2
    return _font(size, bold)


def _glow(size: int, color) -> Image.Image:
    g = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(g).ellipse([60, 60, size - 60, size - 60], fill=color[:3] + (80,))
    return g.filter(ImageFilter.GaussianBlur(26))


def _ball(d: ImageDraw.ImageDraw, x: int, y: int, r: int, bg) -> None:
    d.ellipse([x - r, y - r, x + r, y + r], fill=WHITE)
    d.regular_polygon((x, y, max(3, r // 2)), 5, fill=bg)


def _goal_line(g: GoalEvent) -> str:
    tag = " (ET)" if g.own_goal else " (11m)" if g.penalty else ""
    return f"{g.minute_label if g.minute is not None else ''} {g.scorer or 'Tor'}{tag}".strip()


def _header(img: Image.Image, d: ImageDraw.ImageDraw, kind: str, league: str) -> None:
    text, color = HEAD[kind]
    d.text((44, 30), text, font=_font(34), fill=color)
    d.text((W - 44, 36), league, font=_fit(d, league, 560, 24, bold=False), fill=GRAY, anchor="ra")


def _teams(img: Image.Image, d: ImageDraw.ImageDraw, snap: MatchSnapshot, bright: str | None, glow: bool,
           cy: int = 95) -> int:
    """Wappen + Namen. bright = 'home'/'away'/None (beide hell). Gibt die Unterkante zurück."""
    for side, team in (("home", snap.home), ("away", snap.away)):
        cx = 60 if side == "home" else W - 60 - CREST
        if glow and side == bright:
            img.alpha_composite(_glow(CREST + 160, GOLD), (cx - 80, cy - 80))
        crest = _crest(team.id, CREST) or _placeholder(team.name, CREST)
        img.alpha_composite(crest, (cx, cy))
        font = _fit(d, team.name, 290, 30)
        tw = d.textlength(team.name, font=font)
        tx = min(max(cx + CREST // 2, 40 + tw / 2), W - 40 - tw / 2)  # nie über den Rand
        d.text((tx, cy + CREST + 22), team.name, font=font,
               fill=WHITE if bright in (None, side) else GRAY, anchor="ma")
    return cy + CREST + 22 + 36


def _score(d: ImageDraw.ImageDraw, h: int, a: int, home_color, away_color, cy: int = 95) -> int:
    font = _font(120)
    mid, sy = W // 2, cy + CREST // 2
    colon = d.textlength(":", font=font)
    d.text((mid, sy), ":", font=font, fill=DIM, anchor="mm")
    d.text((mid - colon / 2 - 22, sy), str(h), font=font, fill=home_color, anchor="rm")
    d.text((mid + colon / 2 + 22, sy), str(a), font=font, fill=away_color, anchor="lm")
    return sy + 70


def _finish(img: Image.Image, h: int) -> bytes:
    img = img.crop((0, 0, W, h))
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, h - 1], 26, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _canvas(h: int = 700) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", (W, h), CARD_BG)
    return img, ImageDraw.Draw(img)


# ── Karten ───────────────────────────────────────────────────────────────────

def render_goal(snap: MatchSnapshot, ev: GoalEvent, league: str) -> bytes:
    img, d = _canvas()
    _header(img, d, "goal", league)
    side = ev.side if ev.side in ("home", "away") else None
    bottom = _teams(img, d, snap, side, glow=True)
    _score(d, ev.home, ev.away, GOLD if side == "home" else WHITE, GOLD if side == "away" else WHITE)

    if ev.scorer or ev.minute is not None:  # Torschütze-Leiste; fehlt er in der API → nur Minute
        by = bottom + 14
        minute = ev.minute_label if ev.minute is not None else ""
        name = f"{ev.scorer or ''}{ev.tags}".strip()
        f_min, f_name = _font(26), _fit(d, name, 520, 26, bold=False)
        inner = 26 + 16 + d.textlength(minute, font=f_min) + (18 + d.textlength(name, font=f_name) if name else 0)
        x0 = W / 2 - inner / 2 - 30
        d.rounded_rectangle([x0, by, W - x0, by + 52], 26, fill=PILL)
        _ball(d, int(x0 + 43), by + 26, 13, PILL)
        tx = x0 + 43 + 13 + 16
        d.text((tx, by + 26), minute, font=f_min, fill=GOLD, anchor="lm")
        if name:
            d.text((tx + d.textlength(minute, font=f_min) + 18, by + 26), name, font=f_name, fill=WHITE, anchor="lm")
        bottom = by + 52
    return _finish(img, bottom + 30)


def _goal_columns(d: ImageDraw.ImageDraw, snap: MatchSnapshot, top: int) -> int:
    """Torschützen je Team unter dem jeweiligen Wappen. Gibt die Unterkante zurück."""
    cols = {"home": [g for g in snap.goals if g.side == "home"], "away": [g for g in snap.goals if g.side == "away"]}
    if not any(cols.values()):
        return top
    font = _font(22, False)
    line_h = 32
    rows = 0
    for side, goals in cols.items():
        lines = [_goal_line(g) for g in goals]
        if len(lines) > MAX_GOAL_LINES:
            lines = lines[:MAX_GOAL_LINES - 1] + [f"+{len(lines) - MAX_GOAL_LINES + 1} weitere"]
        x = 60 if side == "home" else W - 60
        for i, text in enumerate(lines):
            text = text if d.textlength(text, font=font) <= 400 else text[:30] + "…"
            d.text((x, top + i * line_h), text, font=font, fill=GRAY, anchor="la" if side == "home" else "ra")
        rows = max(rows, len(lines))
    return top + rows * line_h


def render_halftime(snap: MatchSnapshot, league: str) -> bytes:
    img, d = _canvas()
    _header(img, d, "halftime", league)
    h, a = snap.halftime or snap.score
    bottom = _teams(img, d, snap, None, glow=False)
    _score(d, h, a, WHITE, WHITE)
    # Tore sind chronologisch – die ersten (h+a) gehören zur 1. Hälfte
    snap_ht = dataclasses.replace(snap, goals=[g for g in snap.goals if g.total <= h + a])
    bottom = _goal_columns(d, snap_ht, bottom + 8)
    return _finish(img, bottom + 30)


def render_fulltime(snap: MatchSnapshot, league: str) -> bytes:
    img, d = _canvas()
    _header(img, d, "fulltime", league)
    h, a = snap.score
    win = None
    if h != a:
        win = "home" if h > a else "away"
    elif snap.penalties and snap.penalties[0] != snap.penalties[1]:
        win = "home" if snap.penalties[0] > snap.penalties[1] else "away"
    bottom = _teams(img, d, snap, win, glow=bool(win))
    sy = _score(d, h, a, GOLD if win == "home" else WHITE, GOLD if win == "away" else WHITE)

    # Zusatz unter dem Spielstand: HZ / n.V. / i.E.
    extra = []
    if snap.penalties:
        extra.append(f"i.E. {snap.penalties[0]}:{snap.penalties[1]}")
    elif snap.extra_time:
        extra.append("n.V.")
    if snap.halftime:
        extra.append(f"HZ {snap.halftime[0]}:{snap.halftime[1]}")
    if extra:
        d.text((W // 2, sy + 4), "  ·  ".join(extra), font=_font(24, False), fill=GRAY, anchor="mt")
    bottom = _goal_columns(d, snap, bottom + 8)
    return _finish(img, bottom + 30)


# ── async API ────────────────────────────────────────────────────────────────

async def card(kind: str, snap: MatchSnapshot, league: str, ev: GoalEvent | None = None) -> io.BytesIO | None:
    """Karte als PNG-Puffer, oder None bei Fehlern (dann bleibt es beim Text-Embed)."""
    try:
        if kind == "goal":
            data = await asyncio.to_thread(render_goal, snap, ev, league)
        elif kind == "halftime":
            data = await asyncio.to_thread(render_halftime, snap, league)
        else:
            data = await asyncio.to_thread(render_fulltime, snap, league)
    except Exception as e:
        logger.warning("Spielstand-Karte (%s, Spiel %s) fehlgeschlagen: %s", kind, snap.match_id, e)
        return None
    return io.BytesIO(data)


async def warm_up(snap: MatchSnapshot) -> None:
    """Beim Anpfiff die Wappen schon in Kartengröße rendern → erstes Tor ohne SVG-Rendering."""
    try:
        await asyncio.to_thread(lambda: (_crest(snap.home.id, CREST), _crest(snap.away.id, CREST)))
    except Exception as e:  # pragma: no cover
        logger.debug("Karten-Warm-up %s: %s", snap.match_id, e)
