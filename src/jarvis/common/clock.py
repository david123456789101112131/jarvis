"""Time helpers. Everything internal is timezone-aware UTC; display converts to local time."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat()


def local_hms(dt: datetime | None = None) -> str:
    """'17:32:10' in local time, used by the activity log."""
    return (dt or utcnow()).astimezone().strftime("%H:%M:%S")


def parse_iso(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
