"""Zeit-Helfer. In der DB wird alles als *naive UTC* gespeichert."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from app.config import settings


def utcnow() -> datetime:
    """Naive UTC (passt zu den DATETIME-Spalten)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def to_local(dt_utc: datetime | None) -> datetime | None:
    if dt_utc is None:
        return None
    return aware_utc(dt_utc).astimezone(settings.tz)


def local_today() -> date:
    return datetime.now(settings.tz).date()


def local_day_bounds_utc(day: date | None = None) -> tuple[datetime, datetime]:
    """Start/Ende eines *lokalen* Kalendertags als naive UTC."""
    day = day or local_today()
    start = datetime.combine(day, time.min, tzinfo=settings.tz)
    end = start + timedelta(days=1)
    return (
        start.astimezone(timezone.utc).replace(tzinfo=None),
        end.astimezone(timezone.utc).replace(tzinfo=None),
    )


def local_date_of(dt_utc: datetime) -> date:
    return to_local(dt_utc).date()


def discord_ts(dt_utc: datetime | None, style: str = "f") -> str:
    """Discord-Timestamp – wird beim Leser in *seiner* Zeitzone angezeigt."""
    if dt_utc is None:
        return "?"
    return f"<t:{int(aware_utc(dt_utc).timestamp())}:{style}>"


def fmt_local(dt_utc: datetime | None, fmt: str = "%d.%m. %H:%M") -> str:
    if dt_utc is None:
        return "?"
    return to_local(dt_utc).strftime(fmt)


def parse_api_dt(value: str | None, assume_utc: bool = True) -> datetime | None:
    """OpenLigaDB liefert z.B. '2026-11-14T14:30:00Z' oder '2026-11-14T17:22:31.93'."""
    if not value:
        return None
    try:
        s = str(value).strip().rstrip("Z")
        if "." in s:
            base, frac = s.rsplit(".", 1)
            s = f"{base}.{frac.ljust(6, '0')[:6]}"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc if assume_utc else settings.tz)
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None
