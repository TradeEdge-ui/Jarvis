"""Injectable clock so time-dependent behaviour (reminders, expiry) is testable."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc


class Clock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock(Clock):
    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **kw) -> None:
        self._now = self._now + timedelta(**kw)

    def set(self, dt: datetime) -> None:
        self._now = dt


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
