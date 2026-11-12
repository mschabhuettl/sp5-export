"""Small helpers for clock times and months."""

import re
from datetime import date, timedelta

DAY = 24 * 60
WEEKDAY_CODES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def minutes(hhmm: str) -> int:
    """Minutes since midnight of "HH:MM" or "HH:MM:SS"."""
    parts = hhmm.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def hhmm(total: int) -> str:
    total %= DAY
    return f"{total // 60:02d}:{total % 60:02d}"


def interval(start: str, end: str) -> tuple[int, int]:
    """Start and end in minutes; an end not after the start lies on the next day."""
    s, e = minutes(start), minutes(end)
    if e <= s:
        e += DAY
    return s, e


def parse_month(month: str) -> tuple[int, int]:
    if not re.match(r"^\d{4}-\d{2}$", month or "") or not 1 <= int(month[5:]) <= 12:
        raise ValueError("invalid month, expected YYYY-MM")
    return int(month[:4]), int(month[5:])


def month_days(month: str) -> list[date]:
    y, m = parse_month(month)
    d = date(y, m, 1)
    days = []
    while d.month == m:
        days.append(d)
        d += timedelta(days=1)
    return days


def add_months(month: str, delta: int) -> str:
    y, m = parse_month(month)
    index = y * 12 + (m - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"
