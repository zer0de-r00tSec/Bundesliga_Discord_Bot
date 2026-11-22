"""Kombiniert zwei Vereinswappen + Spielinfo zu einem Bild (läuft im Thread, blockiert den Bot nicht)."""
from __future__ import annotations

import asyncio
import io
import logging
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    import cairosvg  # benötigt libcairo2 auf dem System
except Exception:  # pragma: no cover
    cairosvg = None

logger = logging.getLogger(__name__)

DARK_BG = (32, 32, 32, 255)
WHITE = (255, 255, 255, 255)
LIGHT_GRAY = (200, 200, 200, 255)
MID_GRAY = (150, 150, 150, 255)
LINE = (70, 70, 70, 255)

CRESTS_DIR = Path(__file__).resolve().parent / "assets" / "crests"
VALID_EXTS = (".png", ".svg", ".webp", ".jpg", ".jpeg")

_FONT_CANDIDATES = {
    True: [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
    False: [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ],
}


@lru_cache(maxsize=32)
def _font(size: int, bold: bool = True):
    for path in _FONT_CANDIDATES[bold]:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # sehr altes Pillow
        return ImageFont.load_default()


def get_crest_path(team_id: int) -> Path | None:
    for ext in VALID_EXTS:
        path = CRESTS_DIR / f"{team_id}{ext}"
        if path.exists():
            return path
    return None


def _fit_logo(img: Image.Image, size: int) -> Image.Image:
    ratio = size / max(img.width, img.height)
    new_w, new_h = max(1, int(img.width * ratio)), max(1, int(img.height * ratio))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=140, threshold=2))
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(img, ((size - new_w) // 2, (size - new_h) // 2), img)
    return canvas


@lru_cache(maxsize=256)  # Anpfiff-Bild + Karten, ~2 Größen × alle Teams des Tages
def _crest_png(team_id: int, size: int) -> bytes | None:
    path = get_crest_path(team_id)
    if not path:
        return None
    try:
        if path.suffix.lower() == ".svg":
            if cairosvg is None:
                logger.warning("cairosvg nicht verfügbar – SVG-Wappen können nicht gerendert werden.")
                return None
            png = cairosvg.svg2png(url=str(path), output_width=size * 3, output_height=size * 3)
            img = Image.open(io.BytesIO(png)).convert("RGBA")
        else:
            img = Image.open(path).convert("RGBA")
        out = io.BytesIO()
        _fit_logo(img, size).save(out, format="PNG")
        return out.getvalue()
    except Exception as e:
        logger.warning("Wappen %s nicht ladbar: %s", path, e)
        return None


def clear_cache() -> None:
    """Nach dem Nachladen von Wappen aufrufen – sonst bleibt ein 'fehlt' im Cache hängen."""
    _crest_png.cache_clear()


def _crest(team_id: int, size: int) -> Image.Image | None:
    data = _crest_png(team_id, size)
    return Image.open(io.BytesIO(data)).convert("RGBA") if data else None


_STOPWORDS = {"fc", "sv", "sc", "vfl", "vfb", "tsg", "tsv", "fsv", "bv", "sg", "spvgg", "ssv", "1.", "04", "05",
              "07", "09", "96", "98", "e.v.", "und", "de"}


def initials(name: str) -> str:
    """'Energie Cottbus' → 'EC', '1. FC Kaiserslautern' → 'K', 'Hertha BSC' → 'HB'."""
    words = [w for w in (name or "").replace("-", " ").split() if w.lower() not in _STOPWORDS]
    letters = "".join(w[0] for w in words if w[0].isalpha())[:2].upper()
    return letters or (name or "?")[:2].upper()


def _placeholder(name: str, size: int) -> Image.Image:
    """Neutrales Ersatz-Wappen: Kreis mit Kürzel."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = size // 10
    d.ellipse([m, m, size - m, size - m], fill=(58, 58, 64, 255), outline=MID_GRAY, width=max(2, size // 60))
    d.text((size // 2, size // 2), initials(name), font=_font(size // 3), fill=WHITE, anchor="mm")
    return img


def _compose(home_id, away_id, home_name, away_name, kickoff, league, size, title) -> bytes | None:
    img1, img2 = _crest(home_id, size), _crest(away_id, size)
    if not img1 and not img2:
        logger.info("Wappen fehlen lokal: home=%s away=%s – kein Bild", home_id, away_id)
        return None
    if not img1 or not img2:
        logger.info("Wappen fehlt lokal: home=%s(%s) away=%s(%s) – Platzhalter",
                    home_id, bool(img1), away_id, bool(img2))
        img1 = img1 or _placeholder(home_name, size)
        img2 = img2 or _placeholder(away_name, size)

    pad, gap, text_top, info_h, divider = 36, 110, 92, 64, 12
    w = size * 2 + gap + pad * 2
    h = text_top + divider + size + divider + info_h + pad

    canvas = Image.new("RGBA", (w, h), DARK_BG)
    draw = ImageDraw.Draw(canvas)
    draw.text((w // 2, pad), title, font=_font(26), fill=WHITE, anchor="mt")
    draw.text((w // 2, pad + 38), f"{home_name}  vs  {away_name}".strip(), font=_font(17), fill=LIGHT_GRAY, anchor="mt")

    line_y = text_top + divider // 2
    draw.line([(pad, line_y), (w - pad, line_y)], fill=LINE, width=1)
    logo_y = text_top + divider
    canvas.paste(img1, (pad, logo_y), img1)
    canvas.paste(img2, (pad + size + gap, logo_y), img2)
    draw.text((pad + size + gap // 2, logo_y + size // 2), "vs", font=_font(20), fill=MID_GRAY, anchor="mm")
    line2_y = text_top + divider + size + divider // 2
    draw.line([(pad, line2_y), (w - pad, line2_y)], fill=LINE, width=1)

    info_y = text_top + divider + size + divider + 12
    if kickoff:
        draw.text((pad, info_y), f"Anstoß: {kickoff}", font=_font(15, False), fill=LIGHT_GRAY, anchor="lt")
    if league:
        draw.text((w - pad, info_y), league, font=_font(15, False), fill=LIGHT_GRAY, anchor="rt")

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


async def compose_vs_image(
    home_team_id: int,
    away_team_id: int,
    home_name: str = "",
    away_name: str = "",
    kickoff: str = "",
    league: str = "",
    size: int = 240,
    title: str = "Anpfiff!",
) -> io.BytesIO | None:
    try:
        data = await asyncio.to_thread(
            _compose, home_team_id, away_team_id, home_name, away_name, kickoff, league, size, title
        )
    except Exception as e:
        logger.warning("VS-Bild fehlgeschlagen: %s", e)
        return None
    return io.BytesIO(data) if data else None
