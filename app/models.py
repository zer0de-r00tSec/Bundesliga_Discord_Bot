"""ORM-Modelle. Schema ist kompatibel zur bestehenden DB (keine Migration nötig)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.timeutil import utcnow


class League(Base):
    __tablename__ = "leagues"
    id: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    short_name: Mapped[Optional[str]] = mapped_column(String(100))
    aliases: Mapped[list["TeamAlias"]] = relationship("TeamAlias", back_populates="team")

    @property
    def display(self) -> str:
        return self.name


class TeamAlias(Base):
    __tablename__ = "team_aliases"
    alias: Mapped[str] = mapped_column(String(100), primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    team: Mapped[Team] = relationship("Team", back_populates="aliases")


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        Index("idx_kickoff", "kickoff_utc"),
        Index("idx_matchday", "league_id", "season", "matchday"),
    )

    match_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.id"))
    season: Mapped[int] = mapped_column(Integer)
    matchday: Mapped[int] = mapped_column(Integer)
    group_name: Mapped[Optional[str]] = mapped_column(String(100))
    kickoff_utc: Mapped[datetime] = mapped_column(DateTime)
    kickoff_local: Mapped[Optional[datetime]] = mapped_column(DateTime)
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    home_score: Mapped[Optional[int]] = mapped_column(Integer)
    away_score: Mapped[Optional[int]] = mapped_column(Integer)
    is_finished: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[Optional[str]] = mapped_column(String(50))
    last_update: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    raw_json: Mapped[Optional[dict]] = mapped_column(JSON)

    home_team: Mapped[Team] = relationship("Team", foreign_keys=[home_team_id], lazy="selectin")
    away_team: Mapped[Team] = relationship("Team", foreign_keys=[away_team_id], lazy="selectin")


class MatchEvent(Base):
    """Gemerkte Events (z.B. Tor mit Schütze) – erlaubt nachträgliches Editieren."""

    __tablename__ = "match_events"
    event_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.match_id"))
    event_type: Mapped[str] = mapped_column(String(50))
    minute: Mapped[Optional[int]] = mapped_column(Integer)
    home_score: Mapped[Optional[int]] = mapped_column(Integer)
    away_score: Mapped[Optional[int]] = mapped_column(Integer)
    player_name: Mapped[Optional[str]] = mapped_column(String(150))
    team_id: Mapped[Optional[int]] = mapped_column(Integer)
    raw_json: Mapped[Optional[dict]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PostedEvent(Base):
    """Was wurde wohin gepostet (Idempotenz + Message-ID zum Editieren)."""

    __tablename__ = "posted_events"
    __table_args__ = (Index("idx_match", "match_id"),)

    event_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    match_id: Mapped[int] = mapped_column(Integer)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    posted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApiCache(Base):
    __tablename__ = "api_cache"
    cache_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BotState(Base):
    __tablename__ = "bot_state"
    state_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    state_value: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
