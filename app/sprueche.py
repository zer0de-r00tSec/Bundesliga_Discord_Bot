"""Zufällige Sprüche aus app/assets/sprueche.yaml.

- Datei wird bei jeder Änderung automatisch neu geladen (kein Neustart nötig).
- Platzhalter: {team} {gegner} {heim} {gast} {sieger} {verlierer} {name} {stand}
  Unbekannte Platzhalter bleiben einfach stehen, kaputte Zeilen werfen keinen Fehler.
- seed: gleicher Seed → gleicher Spruch (wichtig, wenn eine Tor-Nachricht später editiert wird).
"""
from __future__ import annotations

import logging
import random
from pathlib import Path

try:
    import yaml
except Exception:  # pragma: no cover - PyYAML fehlt → Standardsprüche
    yaml = None

logger = logging.getLogger(__name__)

FILE = Path(__file__).resolve().parent / "assets" / "sprueche.yaml"

DEFAULTS: dict[str, list[str]] = {
    "anpfiff": ["Der Ball rollt!"],
    "tor": ["Da zappelt das Netz!"],
    "halbzeit": ["Pause. Erstmal durchatmen."],
    "abpfiff_sieg": ["{sieger} holt sich die Punkte!"],
    "abpfiff_remis": ["Punkteteilung."],
    "tagesstart": ["Heute wird Fußball gespielt – alles andere ist egal. 🏟️"],
    "willkommen": ["**{name}** betritt die Kurve – willkommen!"],
}

_cache: dict[str, list[str]] = {}
_mtime: float | None = None


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _load() -> dict[str, list[str]]:
    global _cache, _mtime
    try:
        mtime = FILE.stat().st_mtime
    except OSError:
        return DEFAULTS
    if _mtime == mtime and _cache:
        return _cache
    if yaml is None:
        logger.warning("PyYAML fehlt (pip install pyyaml) – nutze Standardsprüche.")
        _mtime, _cache = mtime, DEFAULTS
        return _cache
    try:
        data = yaml.safe_load(FILE.read_text(encoding="utf-8")) or {}
        _cache = {str(k): [str(x).strip() for x in (v or []) if str(x).strip()]
                  for k, v in data.items() if isinstance(v, list)}
        _mtime = mtime
        logger.info("Sprüche geladen: %s", ", ".join(f"{k} ({len(v)})" for k, v in _cache.items()))
    except Exception as e:
        logger.error("sprueche.yaml fehlerhaft – nutze bisherigen Stand: %s", e)
        _mtime = mtime
        _cache = _cache or DEFAULTS
    return _cache


def pick(kind: str, seed=None, fallback: str | None = None, **values) -> str:
    """Zufälliger Spruch der Kategorie `kind` (fällt auf `fallback`-Kategorie zurück)."""
    data = _load()
    lines = data.get(kind) or (data.get(fallback) if fallback else None) or DEFAULTS.get(kind) \
        or DEFAULTS.get(fallback or "", [""])
    rnd = random.Random(seed) if seed is not None else random
    line = rnd.choice(lines) if lines else ""
    try:
        return line.format_map(_Safe(values))
    except (ValueError, IndexError):
        return line


def counts() -> dict[str, int]:
    return {k: len(v) for k, v in _load().items()}
