"""Austrian public holidays."""

from datetime import date, timedelta


def easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month, day = divmod(h + m - 7 * n + 114, 31)
    return date(year, month, day + 1)


def austrian_holidays(year: int) -> list[dict]:
    """Nationwide public holidays of Austria for one year, sorted by date."""
    easter = easter_sunday(year)
    days = [
        (date(year, 1, 1), "Neujahr"),
        (date(year, 1, 6), "Heilige Drei Könige"),
        (easter + timedelta(days=1), "Ostermontag"),
        (date(year, 5, 1), "Staatsfeiertag"),
        (easter + timedelta(days=39), "Christi Himmelfahrt"),
        (easter + timedelta(days=50), "Pfingstmontag"),
        (easter + timedelta(days=60), "Fronleichnam"),
        (date(year, 8, 15), "Mariä Himmelfahrt"),
        (date(year, 10, 26), "Nationalfeiertag"),
        (date(year, 11, 1), "Allerheiligen"),
        (date(year, 12, 8), "Mariä Empfängnis"),
        (date(year, 12, 25), "Christtag"),
        (date(year, 12, 26), "Stefanitag"),
    ]
    return [{"date": d.isoformat(), "name": n} for d, n in sorted(days)]
