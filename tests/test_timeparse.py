from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from friday.timeparse import parse_when, strip_phrases

KL = ZoneInfo("Asia/Kuala_Lumpur")
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)     # Wed 20:00 MYT


def p(text):
    r = parse_when(text, NOW, KL)
    return None if r is None else (r.when.astimezone(KL).strftime("%a %Y-%m-%d %H:%M"), r.has_time)


@pytest.mark.parametrize("text,expected", [
    ("tomorrow", ("Thu 2026-10-08 09:00", False)),
    ("tomorrow at 8pm", ("Thu 2026-10-08 20:00", True)),
    ("remind me at 9 pm", ("Wed 2026-10-07 21:00", True)),
    ("at 7am", ("Thu 2026-10-08 07:00", True)),             # 7am already passed today -> tomorrow
    ("at 20:30", ("Wed 2026-10-07 20:30", True)),
    ("tonight", ("Wed 2026-10-07 20:00", True)),
    ("in 2 hours", ("Wed 2026-10-07 22:00", True)),
    ("in 30 minutes", ("Wed 2026-10-07 20:30", True)),
    ("in 3 days", ("Sat 2026-10-10 09:00", False)),
    ("friday", ("Fri 2026-10-09 09:00", False)),
    ("on monday at 5pm", ("Mon 2026-10-12 17:00", True)),
    ("by 2026-12-25", ("Fri 2026-12-25 09:00", False)),
    ("day after tomorrow 10am", ("Fri 2026-10-09 10:00", True)),
    ("12am tomorrow", ("Thu 2026-10-08 00:00", True)),
    ("12pm tomorrow", ("Thu 2026-10-08 12:00", True)),
])
def test_parse(text, expected):
    assert p(text) == expected


@pytest.mark.parametrize("text", ["finish the proposal", "call them sometime", "", "at 25:00"])
def test_no_time_found(text):
    assert p(text) is None


def test_naming_todays_weekday_means_next_week():
    assert p("wednesday") == ("Wed 2026-10-14 09:00", False)


def test_strip_phrases_leaves_a_clean_title():
    txt = "tomorrow at 8pm to call the supplier"
    r = parse_when(txt, NOW, KL)
    assert strip_phrases(txt, r.matched) == "to call the supplier"
