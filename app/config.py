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
    # Weitere Ligen per ID: "dfb:123,ffb1:456" (hat Vorrang vor dem automatischen Anlegen)
    discord_ticker_channels: str = ""
    # 0 = Willkommensnachricht aus (dann braucht der Bot auch kein Members-Intent)
    discord_welcome_channel_id: int = 0
    # Kommagetrennt "1,2,3" oder JSON "[1,2,3]"
    discord_admin_user_ids: str = ""
    # Rolle, die neue Mitglieder automatisch bekommen (0 = aus). Braucht "Rollen verwalten"
    # und im Developer-Portal "Server Members Intent".
    discord_auto_role_id: int = 0

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

    # ── Automatische Channels (Bot braucht "Kanäle verwalten") ────────────────
    # Ligen mit eigener Kategorie + einem Channel pro Spiel (leer = aus)
    match_channel_leagues: str = "bl1,bl2"
    # Kategorienamen je Liga
    match_category_names: str = "bl1:Bundesliga 1,bl2:Bundesliga 2,ffb1:Frauen-Bundesliga,bl3:3. Liga"
    # So viele Stunden vor dem ersten Anstoß eines Spieltags werden dessen Channels angelegt
    # (gleichzeitig werden die Channels des alten, beendeten Spieltags gelöscht)
    match_channel_prepare_hours: int = 24
    # Trenner im Channelnamen: dortmund-vs-köln
    match_channel_separator: str = "-vs-"
    # 🔴 / ✅ vor den Channelnamen setzen, sobald das Spiel läuft / beendet ist
    match_channel_status_emoji: bool = True
    # Alte Spiel-Channels beim nächsten Spieltag löschen (false = stehen lassen)
    match_channel_cleanup: bool = True
    # Konferenz-Channels, die der Bot selbst anlegt, wenn keine ID gesetzt ist
    # (Ligen ohne Eintrag hier → DISCORD_TICKER_CHANNEL_ID = "Die Konferenz")
    conference_channel_names: str = "dfb:dfb-pokal,ffb1:frauen-bundesliga,dfbf:frauen-bundesliga"
    auto_create_conference_channels: bool = True

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

    @classmethod
    def _pairs(cls, v: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for item in cls._split(v):
            if ":" in item:
                k, val = item.split(":", 1)
                if k.strip() and val.strip():
                    out[k.strip().lower()] = val.strip()
        return out

    @property
    def match_channel_league_list(self) -> list[str]:
        wanted = [x.lower() for x in self._split(self.match_channel_leagues)]
        return [lg for lg in wanted if lg in self.league_list]

    @property
    def category_names(self) -> dict[str, str]:
        return self._pairs(self.match_category_names)

    @property
    def conference_names(self) -> dict[str, str]:
        return self._pairs(self.conference_channel_names)

    @property
    def explicit_ticker_channels(self) -> dict[str, int]:
        out = {}
        for k, v in self._pairs(self.discord_ticker_channels).items():
            try:
                if int(v):
                    out[k] = int(v)
            except ValueError:
                pass
        return out

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
        """Konferenz-Channel einer Liga: explizite ID → vom Bot angelegter Channel → Standard."""
        specific = {
            "bl1": self.discord_ticker_channel_bl1,
            "bl2": self.discord_ticker_channel_bl2,
        }.get(league, 0) or self.explicit_ticker_channels.get(league, 0)
        return specific or RUNTIME_CHANNELS.get(league, 0) or self.discord_ticker_channel_id

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


LEAGUE_NAMES = {
    "bl1": "1. Bundesliga", "bl2": "2. Bundesliga", "bl3": "3. Liga", "dfb": "DFB-Pokal",
    "ffb1": "Frauen-Bundesliga", "ffb2": "2. Frauen-Bundesliga", "dfbf": "DFB-Pokal der Frauen",
}
LEAGUE_SHORT = {
    "bl1": "1. BL", "bl2": "2. BL", "bl3": "3. Liga", "dfb": "Pokal",
    "ffb1": "Frauen-BL", "ffb2": "2. Frauen-BL", "dfbf": "Pokal Frauen",
}
# K.-o.-Wettbewerbe: keine Tabelle, Verlängerung/Elfmeterschießen möglich
CUP_LEAGUES = {"dfb", "dfbf"}

# Vom Bot zur Laufzeit angelegte Konferenz-Channels (Liga → Channel-ID)
RUNTIME_CHANNELS: dict[str, int] = {}

settings = Settings()
