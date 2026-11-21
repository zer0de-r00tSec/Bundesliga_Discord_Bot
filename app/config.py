"""Zentrale Konfiguration (liest .env)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Discord ──────────────────────────────────────────────────────────────
    discord_bot_token: str
    discord_guild_id: int
    # Standard-Channel für den Ticker
    discord_ticker_channel_id: int
    # Optional: eigener Channel pro Liga (0 = Standard-Channel nutzen)
    discord_ticker_channel_bl1: int = 0
    discord_ticker_channel_bl2: int = 0
    # 0 = Willkommensnachricht aus (dann braucht der Bot auch kein Members-Intent)
    discord_welcome_channel_id: int = 0
    # Kommagetrennt "1,2,3" oder JSON "[1,2,3]"
    discord_admin_user_ids: str = ""

    # ── MariaDB ──────────────────────────────────────────────────────────────
    mariadb_host: str = "127.0.0.1"
    mariadb_port: int = 3306
    mariadb_database: str = "football_bot"
    mariadb_user: str
    mariadb_password: str

    # ── App ──────────────────────────────────────────────────────────────────
    # 0 = automatisch (ab Juli neue Saison). OpenLigaDB: Saison 2026/27 = 2026
    season: int = 0
    leagues: str = "bl1,bl2"
    timezone: str = "Europe/Berlin"

    poll_interval_live_seconds: int = 15
    pre_kickoff_wake_minutes: int = 30
    # Wie lange nach Anpfiff ein Spiel maximal beobachtet wird (Sicherheitsnetz)
    match_max_duration_minutes: int = 180
    # Voller Saison-Sync alle N Stunden (Anstoßzeiten ändern sich!)
    full_sync_interval_hours: int = 12

    # Features
    conference_enabled: bool = True      # Live-Konferenz-Embed, das laufend editiert wird
    post_table_after_matchday: bool = True
    post_matchday_opener: bool = True

    log_level: str = "INFO"          # Konsole / journalctl
    log_file_level: str = "DEBUG"    # log/bot.log – "alles" zum Auswerten
    log_dir: str = ""                # leer = <projekt>/log

    # ── Parser ───────────────────────────────────────────────────────────────
    @staticmethod
    def _split(v: str) -> list[str]:
        v = (v or "").strip()
        if v.startswith("["):
            return [str(x).strip() for x in json.loads(v)]
        return [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self._split(self.discord_admin_user_ids)}

    @property
    def league_list(self) -> list[str]:
        return [x.lower() for x in self._split(self.leagues)] or ["bl1", "bl2"]

    # ── Abgeleitete Werte ────────────────────────────────────────────────────
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def current_season(self) -> int:
        if self.season:
            return self.season
        now = datetime.now(self.tz)
        return now.year if now.month >= 7 else now.year - 1

    def ticker_channel_for(self, league: str) -> int:
        specific = {
            "bl1": self.discord_ticker_channel_bl1,
            "bl2": self.discord_ticker_channel_bl2,
        }.get(league, 0)
        return specific or self.discord_ticker_channel_id

    @property
    def all_ticker_channels(self) -> set[int]:
        return {self.ticker_channel_for(lg) for lg in self.league_list}

    @property
    def log_path(self) -> Path:
        if self.log_dir:
            return Path(self.log_dir).expanduser()
        return Path(__file__).resolve().parent.parent / "log"

    @property
    def db_url(self) -> str:
        from urllib.parse import quote_plus

        return (
            f"mysql+aiomysql://{quote_plus(self.mariadb_user)}:{quote_plus(self.mariadb_password)}"
            f"@{self.mariadb_host}:{self.mariadb_port}/{self.mariadb_database}"
            f"?charset=utf8mb4"
        )


LEAGUE_NAMES = {"bl1": "1. Bundesliga", "bl2": "2. Bundesliga", "bl3": "3. Liga", "dfb": "DFB-Pokal"}
LEAGUE_SHORT = {"bl1": "1. BL", "bl2": "2. BL", "bl3": "3. Liga", "dfb": "Pokal"}

settings = Settings()
