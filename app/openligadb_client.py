"""OpenLigaDB API Client (kostenlos, ohne Key).

Genutzte Endpunkte:
  GET /getmatchdata/{league}/{season}              alle Spiele einer Saison
  GET /getmatchdata/{league}/{season}/{day}        ein Spieltag (alle Spiele in 1 Request)
  GET /getmatchdata/{match_id}                     Einzelspiel
  GET /getlastchangedate/{league}/{season}/{day}   Zeitstempel der letzten Änderung am Spieltag
  GET /getcurrentgroup/{league}                    aktueller Spieltag
  GET /getbltable/{league}/{season}                Tabelle
  GET /getgoalgetters/{league}/{season}            Torjäger

Polling-Strategie: Während Spielen wird pro Liga+Spieltag nur der billige
`getlastchangedate`-Endpunkt abgefragt. Erst wenn sich der Zeitstempel ändert
(oder spätestens alle `max_stale` Sekunden), wird der ganze Spieltag geladen.
Statt 9 Requests pro Liga und Poll sind das meist 1 Mini-Request.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.timeutil import parse_api_dt

logger = logging.getLogger(__name__)

BASE_URL = "https://api.openligadb.de"


class OpenLigaDBError(Exception):
    pass


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return False


class OpenLigaDBClient:
    def __init__(self, timeout: float = 15.0):
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "StadionEcho-DiscordTicker/2.0"},
            follow_redirects=True,
        )
        self.request_count = 0
        self.error_count = 0
        self.last_ok: float | None = None

    async def close(self) -> None:
        await self._client.aclose()

    @retry(
        retry=retry_if_exception(_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _get_raw(self, path: str) -> Any:
        self.request_count += 1
        resp = await self._client.get(path)
        resp.raise_for_status()
        if not resp.content:
            return None
        return resp.json()

    async def _get(self, path: str) -> Any:
        try:
            data = await self._get_raw(path)
            self.last_ok = time.time()
            return data
        except httpx.HTTPStatusError as e:
            self.error_count += 1
            raise OpenLigaDBError(f"HTTP {e.response.status_code}: {path}") from e
        except Exception as e:
            self.error_count += 1
            raise OpenLigaDBError(f"{type(e).__name__}: {path}: {e}") from e

    # ── Endpunkte ────────────────────────────────────────────────────────────
    async def get_season_matches(self, league: str, season: int) -> list[dict]:
        return await self._get(f"/getmatchdata/{league}/{season}") or []

    async def get_matchday(self, league: str, season: int, day: int) -> list[dict]:
        return await self._get(f"/getmatchdata/{league}/{season}/{day}") or []

    async def get_match(self, match_id: int) -> dict | None:
        try:
            return await self._get(f"/getmatchdata/{match_id}")
        except OpenLigaDBError:
            return None

    async def get_current_group(self, league: str) -> dict | None:
        try:
            return await self._get(f"/getcurrentgroup/{league}")
        except OpenLigaDBError:
            return None

    async def get_table(self, league: str, season: int) -> list[dict]:
        return await self._get(f"/getbltable/{league}/{season}") or []

    async def get_goalgetters(self, league: str, season: int) -> list[dict]:
        return await self._get(f"/getgoalgetters/{league}/{season}") or []

    async def get_last_change_date(self, league: str, season: int, day: int) -> str | None:
        """Roh-String des Änderungszeitpunkts (reicht zum Vergleichen)."""
        try:
            raw = await self._get(f"/getlastchangedate/{league}/{season}/{day}")
        except OpenLigaDBError as e:
            logger.debug("lastchangedate nicht verfügbar: %s", e)
            return None
        if isinstance(raw, str) and parse_api_dt(raw) is not None:
            return raw
        return None

    async def health_check(self) -> bool:
        try:
            await self._get("/getcurrentgroup/bl1")
            return True
        except OpenLigaDBError:
            return False
