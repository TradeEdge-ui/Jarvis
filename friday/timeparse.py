"""Small, deterministic natural-language time parser for the offline planner and for tools.

Handles: today, tonight, tomorrow, weekday names ("friday", "next monday"), "in 2 hours/days/minutes",
"at 8pm", "at 20:30", "by 5 pm", "this evening/morning/afternoon". Everything is interpreted in the user's
timezone and returned as UTC. When no time of day is given, `has_time` is False and 09:00 local is used.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from friday.clock import UTC

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
        "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "thirty": 30}

_TIME_AT = re.compile(r"\b(?:at|by|@)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b", re.I)
_TIME_BARE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
_IN = re.compile(r"\bin\s+(\d+|an?|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|thirty)\s*"
                 r"(minutes?|mins?|hours?|hrs?|days?|weeks?)\b", re.I)
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


@dataclass
class ParsedTime:
    when: datetime          # UTC
    has_time: bool
    matched: list[str]      # phrases consumed (so the caller can strip them from the title)


def _hm(m: re.Match, default_ampm: str | None = None) -> tuple[int, int] | None:
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    ap = (m.group(3) or default_ampm or "").lower().replace(".", "")
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return h, mi


def parse_when(text: str, now: datetime, tz: ZoneInfo, default_hour: int = 9) -> ParsedTime | None:
    low = text.lower()
    local_now = now.astimezone(tz)
    matched: list[str] = []

    m = _IN.search(low)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _NUM[m.group(1)]
        unit = m.group(2)
        delta = (timedelta(minutes=n) if unit.startswith(("min",)) else
                 timedelta(hours=n) if unit.startswith(("hour", "hr")) else
                 timedelta(days=n) if unit.startswith("day") else timedelta(weeks=n))
        has_time = not unit.startswith(("day", "week"))
        when = local_now + delta
        if not has_time:
            when = when.replace(hour=default_hour, minute=0, second=0, microsecond=0)
        return ParsedTime(when.astimezone(UTC), has_time, [m.group(0)])

    day = None
    mi = _ISO_DATE.search(low)
    if mi:
        day = datetime(int(mi.group(1)), int(mi.group(2)), int(mi.group(3)), tzinfo=tz).date()
        matched.append(mi.group(0))
    elif re.search(r"\bday after tomorrow\b", low):
        day = (local_now + timedelta(days=2)).date(); matched.append("day after tomorrow")
    elif re.search(r"\btomorrow\b", low):
        day = (local_now + timedelta(days=1)).date(); matched.append("tomorrow")
    elif re.search(r"\b(tonight|this evening)\b", low):
        day = local_now.date(); matched.append(re.search(r"\b(tonight|this evening)\b", low).group(0))
    elif re.search(r"\btoday\b", low):
        day = local_now.date(); matched.append("today")
    else:
        wd = re.search(r"\b(?:(next|this|on)\s+)?(" + "|".join(WEEKDAYS) + r")\b", low)
        if wd:
            target = WEEKDAYS.index(wd.group(2))
            ahead = (target - local_now.weekday()) % 7 or 7  # naming today's weekday means next week's
            day = (local_now + timedelta(days=ahead)).date()
            matched.append(wd.group(0))

    hm = None
    at = _TIME_AT.search(low) or _TIME_BARE.search(low)
    evening = re.search(r"\b(tonight|this evening)\b", low)
    if at:
        hm = _hm(at, "pm" if evening and not at.group(3) and int(at.group(1)) < 12 else None)
        if hm:
            matched.append(at.group(0))
    elif re.search(r"\bthis morning\b", low):
        hm = (9, 0); matched.append("this morning")
    elif re.search(r"\bthis afternoon\b", low):
        hm = (15, 0); matched.append("this afternoon")
    elif evening:
        hm = (20, 0)

    if day is None and hm is None:
        return None
    has_time = hm is not None
    h, mi_ = hm if hm else (default_hour, 0)
    if day is None:  # only a clock time: today if still ahead, else tomorrow
        cand = local_now.replace(hour=h, minute=mi_, second=0, microsecond=0)
        if cand <= local_now:
            cand += timedelta(days=1)
        return ParsedTime(cand.astimezone(UTC), True, matched)
    when = datetime(day.year, day.month, day.day, h, mi_, tzinfo=tz)
    return ParsedTime(when.astimezone(UTC), has_time, matched)


def strip_phrases(text: str, phrases: list[str]) -> str:
    out = text
    for p in sorted(phrases, key=len, reverse=True):
        out = re.sub(re.escape(p), " ", out, flags=re.I)
    out = re.sub(r"\b(?:at|by|on|for)\s*$", "", out.strip(), flags=re.I)
    return re.sub(r"\s+", " ", out).strip(" ,.;:-")
