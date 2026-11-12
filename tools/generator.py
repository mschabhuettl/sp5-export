"""Synthetic demo dataset: invented people, invented shift times of an invented dispatch centre.

Everything is derived from random.Random instances seeded with the given seed, so the same
month, seed and options always give the same dataset.
"""

import argparse
import json
import random
import sys
from datetime import date, timedelta

from sp5export.demand import coverage_segments
from sp5export.feasibility import capacity_hours, check_feasibility, demand_hours
from sp5export.holidays import austrian_holidays
from sp5export.timeutil import DAY, add_months, hhmm, interval, month_days, parse_month

FORMAT = "dispofold/dataset"
TARGET_RATIO = 0.825

AREAS = [
    ("AN", "Notrufannahme", "#B45309"),
    ("DR", "Disposition Rettung", "#15803D"),
    ("TV", "Transportvermittlung", "#2563EB"),
    ("LF", "Lageführung", "#7C3AED"),
    ("TS", "Technik und Systeme", "#0E7490"),
]

# id, area, code (SP5 short name), name, start, end, category, colour
SHIFTS = [
    ("AN-F", "AN", "A1", "Annahme früh 06:30", "06:30", "14:30", "EARLY", "#F3E3D8"),
    ("AN-T", "AN", "A2", "Annahme Tag 07:00", "07:00", "19:00", "DAY", "#F6EAE1"),
    ("AN-S", "AN", "A3", "Annahme spät 14:00", "14:00", "22:00", "LATE", "#EED9C9"),
    ("AN-N", "AN", "A4", "Annahme Nacht 21:30", "21:30", "06:30", "NIGHT", "#E7C8B0"),
    ("DR-T", "DR", "R1", "Disposition Tag 07:00", "07:00", "19:00", "DAY", "#E3F0E8"),
    ("DR-M", "DR", "R2", "Disposition Mitte 09:00", "09:00", "17:00", "DAY", "#E3F0E8"),
    ("DR-A", "DR", "R3", "Disposition Abend 13:00", "13:00", "21:00", "LATE", "#CCE3D4"),
    ("DR-N", "DR", "R4", "Disposition Nacht 19:00", "19:00", "07:00", "NIGHT", "#B4D6C1"),
    ("TV-F", "TV", "V1", "Transport früh 07:30", "07:30", "15:30", "EARLY", "#DCE6FC"),
    ("TV-S", "TV", "V2", "Transport spät 11:30", "11:30", "19:30", "LATE", "#CFDDFB"),
    ("LF-T", "LF", "L1", "Lageführung Tag", "07:00", "19:00", "DAY", "#EFE7FD"),
    ("LF-N", "LF", "L2", "Lageführung Nacht", "19:00", "07:00", "NIGHT", "#D5C0F9"),
    ("TS-T", "TS", "S1", "Technik Tag 08:00", "08:00", "16:30", "DAY", "#E2EEF2"),
]

# Reference staffing as shift counts per day type. Demand windows are the exact coverage of these
# mixes, so every window can be met by the area's shifts. Night counts are the same on all day
# types, which makes the part after midnight independent of the next day's type.
DAY_TYPES = {"WD": ["MON", "TUE", "WED", "THU", "FRI"], "WE": ["SAT", "SUN"], "HOL": ["HOL"]}
MIXES = {
    "AN": {
        "WD": {"AN-F": 3, "AN-T": 4, "AN-S": 3, "AN-N": 3},
        "WE": {"AN-F": 2, "AN-T": 3, "AN-S": 3, "AN-N": 3},
    },
    "DR": {
        "WD": {"DR-T": 4, "DR-M": 2, "DR-A": 2, "DR-N": 3},
        "WE": {"DR-T": 3, "DR-A": 1, "DR-N": 3},
    },
    "TV": {
        "WD": {"TV-F": 3, "TV-S": 2},
        "WE": {"TV-F": 1, "TV-S": 1},
    },
    "LF": {"WD": {"LF-T": 1, "LF-N": 1}, "WE": {"LF-T": 1, "LF-N": 1}},
    "TS": {"WD": {"TS-T": 1}, "WE": {"TS-T": 1}},
}
SCALED_AREAS = ("AN", "DR", "TV")  # sized to the staff; the others are fixed one-person posts
HOLIDAY_AS_WEEKEND = ("AN", "TV")  # these areas have their own (weekend-like) HOL rules
MAX_IS_MIN = ("LF",)  # exactly one person in charge

# Qualification groups of the invented demo centre: overlapping, many dispatchers also take calls,
# the shift leaders come from dispatch.
AREA_GROUPS = [
    (("AN",), 28), (("AN", "TV"), 18), (("TV",), 10), (("AN", "DR"), 30), (("DR",), 14),
    (("DR", "LF"), 6), (("AN", "DR", "LF"), 4), (("TS",), 3), (("DR", "TS"), 3), (("AN", "TS"), 2),
    (("AN", "DR", "TV"), 8), (("DR", "TV"), 6),
]

# (code, name, reducesTarget, chargesAccount, compensatoryRest, color)
ABSENCE_TYPES = [
    ("U", "Urlaub", True, False, False, "#A3C585"),
    ("ER", "Ersatzruhe", True, False, True, "#8FD3C7"),
    ("K", "Krankenstand", True, False, False, "#E39B8F"),
    ("ZA", "Zeitausgleich", True, True, False, "#C8C08A"),
    ("KUR", "Kur und Rehabilitation", True, False, False, "#B9A3CF"),
    ("KA", "Karenz", True, False, False, "#BFBFBF"),
    ("PF", "Pflegefreistellung", True, False, False, "#E7B98C"),
    ("SU", "Sonderurlaub", True, False, False, "#A9C9E6"),
    ("FB", "Fortbildung", True, False, False, "#8FB3D9"),
    ("DR", "Dienstreise", True, False, False, "#A7B7C7"),
    ("AU", "Arbeitsunfall", True, False, False, "#D98F8F"),
    ("PM", "Papamonat", True, False, False, "#C7B6A0"),
]

FIRST_NAMES = [
    "Anna", "Bernhard", "Christa", "Daniel", "Elke", "Florian", "Gerda", "Harald", "Ines", "Johann",
    "Karin", "Lukas", "Martina", "Norbert", "Olivia", "Peter", "Renate", "Stefan", "Theresa", "Ulrich",
    "Verena", "Werner", "Andrea", "Bettina", "Clemens", "Doris", "Erwin", "Franziska", "Georg", "Hannah",
    "Isabella", "Jakob", "Katharina", "Leopold", "Magdalena", "Matthias", "Nina", "Oskar", "Paula",
    "Raphael", "Sabine", "Thomas", "Ursula", "Viktor", "Waltraud", "Alexander", "Birgit", "Christoph",
    "Elisabeth", "Fabian", "Gabriele", "Herbert", "Johanna", "Klaus", "Lena", "Manuel", "Monika", "Patrick",
    "Rosa", "Sebastian", "Tanja", "Valentin", "Barbara", "Dominik", "Eva", "Gregor", "Julia", "Markus",
]
NAME_STEMS = [
    "Ahorn", "Birken", "Eichen", "Feld", "Grün", "Hasel", "Kirch", "Linden", "Moos", "Reh", "Stein",
    "Tannen", "Wald", "Wiesen", "Zirben", "Brunn", "Hof", "Rosen", "Sonnen", "Lärchen", "Quellen", "Bach",
    "Erlen", "Fichten", "Hollen", "Kogel", "Leiten", "Rainer", "Schatten", "Weiden",
]
NAME_ENDINGS = [
    "er", "berger", "eder", "huber", "leitner", "mayr", "gruber", "hofer", "bichler", "egger", "steiner",
    "wieser", "bauer", "lechner", "auer", "reiter", "kogler", "winkler",
]
FAMILY_NOTES = ["Kinderbetreuung", "Pflege von Angehörigen", "Kinderbetreuung (Schule)"]


def _shift(sid: str) -> tuple:
    return next(s for s in SHIFTS if s[0] == sid)


def _shift_interval(sid: str) -> tuple[int, int]:
    s = _shift(sid)
    return interval(s[4], s[5])


def _quota(groups: list, n: int, rng: random.Random) -> list[tuple]:
    """Distribute n people over the groups proportionally (largest remainder), then shuffle."""
    total = sum(c for _, c in groups)
    raw = [(g, c * n / total) for g, c in groups]
    counts = [int(x) for _, x in raw]
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i][1] - counts[i]), i))
    for i in order[: n - sum(counts)]:
        counts[i] += 1
    result = [g for (g, _), k in zip(groups, counts) for _ in range(k)]
    rng.shuffle(result)
    return result


def _people(n: int, area_ids: list[str], rng: random.Random) -> list[dict]:
    groups = []
    for group, count in AREA_GROUPS:
        g = tuple(a for a in group if a in area_ids)
        # Nobody may be qualified only for one-person posts, or they cannot reach their hours.
        if g and any(a in SCALED_AREAS for a in g):
            groups.append((g, count))
    names = [(f, s + e) for f in FIRST_NAMES for s in NAME_STEMS for e in NAME_ENDINGS]
    picked = rng.sample(names, n)
    hours = rng.choices([39.0, 42.0, 34.0, 30.0, 25.0, 20.0], weights=[46, 10, 14, 14, 10, 6], k=n)
    people, short_names = [], set()
    for i, (group, (first, last)) in enumerate(zip(_quota(groups, n, rng), picked)):
        short = (first[0] + last[:2]).upper().replace("Ä", "AE").replace("Ö", "OE").replace("Ü", "UE")
        base, k = short, 1
        while short in short_names:
            k += 1
            short = f"{base}{k}"
        short_names.add(short)
        family = []
        if rng.random() < 0.1:
            kind = rng.choice(["latestEnd", "earliestStart", "noNights"])
            family.append({
                "weekdays": sorted(rng.sample(range(1, 6), rng.choice([1, 2]))),
                "latestEnd": "18:00" if kind == "latestEnd" else None,
                "earliestStart": "08:00" if kind == "earliestStart" else None,
                "noNights": kind == "noNights",
                "note": rng.choice(FAMILY_NOTES),
            })
        people.append({
            "id": f"P{i + 1:04d}",
            "firstName": first,
            "lastName": last,
            "shortName": short,
            "weeklyHours": hours[i],
            "areaIds": [a for a in area_ids if a in group],
            "hoursAccount": round(rng.uniform(-20, 30) * 2) / 2,
            "compRestBalance": rng.choices(range(7), weights=[20, 15, 20, 15, 12, 10, 8])[0],
            "familyRules": family,
            "externalId": str(1001 + i),
            "entryDate": None,
            "exitDate": None,
        })
    return people


def _absences(people: list[dict], days: list[date], rng: random.Random) -> tuple[list[dict], set[str]]:
    """Absences of the month and the ids of people on leave for the whole month."""
    taken: dict[str, dict[str, str]] = {p["id"]: {} for p in people}
    n = len(people)
    order = [p["id"] for p in people]
    rng.shuffle(order)
    karenz = set(order[: round(n * 0.02)])
    for pid in karenz:
        for d in days:
            taken[pid][d.isoformat()] = "KA"
    weekdays = [d for d in days if d.weekday() < 5]

    def block(pid: str, code: str, length: int, only_weekdays: bool = False) -> None:
        pool = weekdays if only_weekdays else days
        start = rng.randrange(len(pool))
        for d in pool[start:start + length]:
            taken[pid].setdefault(d.isoformat(), code)

    others = [p for p in people if p["id"] not in karenz]
    for p in others:
        pid = p["id"]
        r = rng.random()
        if r < 0.22:
            block(pid, "U", rng.randint(5, 10))
        elif r < 0.25:
            block(pid, "U", rng.randint(2, 4))
        if rng.random() < 0.12:
            block(pid, "K", rng.randint(1, 5))
        if rng.random() < 0.08:
            block(pid, "FB", rng.randint(1, 2), only_weekdays=True)
        if rng.random() < 0.08:
            block(pid, "ZA", 1, only_weekdays=True)
        if rng.random() < 0.03:
            block(pid, "PF", rng.randint(1, 3))
        if rng.random() < 0.02:
            block(pid, "SU", 1, only_weekdays=True)
        if p["compRestBalance"] >= 2 and rng.random() < 0.3:
            for d in rng.sample(days, rng.randint(1, 2)):
                taken[pid].setdefault(d.isoformat(), "ER")
    for p in others[: round(n * 0.007)]:
        block(p["id"], "KUR", 21)
    absences = [{"personId": pid, "date": d, "typeCode": code}
                for pid in sorted(taken) for d, code in sorted(taken[pid].items())]
    return absences, karenz


def _windows(counts: dict[str, int]) -> list[tuple[int, int, int]]:
    """Coverage of a shift mix on one day; night counts are equal every day, so yesterday is today."""
    spans = [(*_shift_interval(sid), k) for sid, k in counts.items()]
    return coverage_segments(spans, spans)


def _day_type(area: str, day: date, holiday_dates: set[str]) -> str:
    if area in HOLIDAY_AS_WEEKEND and day.isoformat() in holiday_dates:
        return "HOL"
    return "WD" if day.weekday() < 5 else "WE"


def _size_mixes(area_ids: list[str], days: list[date], holiday_dates: set[str],
                targets: dict[str, float]) -> dict[str, dict[str, dict[str, int]]]:
    """Shift mixes per area and day type. Scaled areas get shifts added one at a time, in the
    proportions of MIXES, until their hours reach the target hours of the area."""
    mixes = {a: {t: dict(m) for t, m in MIXES[a].items()} for a in area_ids if a not in SCALED_AREAS}
    units: dict[str, list[dict]] = {}
    for area in (a for a in area_ids if a in SCALED_AREAS):
        types = ["WD", "WE"] + (["HOL"] if area in HOLIDAY_AS_WEEKEND else [])
        counts = {t: sum(1 for d in days if _day_type(area, d, holiday_dates) == t) for t in types}
        units[area] = []
        for sid in dict.fromkeys(sid for m in MIXES[area].values() for sid in m):
            s, e = _shift_interval(sid)
            groups = [types] if e > DAY else [[t] for t in types]  # nights: same count every day
            for group in groups:
                base = MIXES[area]["WE" if group[0] == "HOL" else group[0]].get(sid, 0)
                if base:
                    hours = (e - s) / 60 * sum(counts[t] for t in group)
                    units[area].append({"sid": sid, "types": group, "base": base, "hours": hours, "count": 0})
    done = {a: 0.0 for a in units}
    last = None
    while units and sum(done.values()) < sum(targets[a] for a in units):
        area = max(units, key=lambda a: (targets[a] - done[a]) / targets[a])
        unit = min(units[area], key=lambda u: (u["count"] + 1) / u["base"])
        unit["count"] += 1
        done[area] += unit["hours"]
        last = (area, unit)
    if last and sum(done.values()) - sum(targets[a] for a in units) > last[1]["hours"] / 2:
        last[1]["count"] -= 1
    for area, area_units in units.items():
        mixes[area] = {}
        for u in area_units:
            for t in u["types"]:
                if u["count"]:
                    mixes[area].setdefault(t, {})[u["sid"]] = u["count"]
    return mixes


def _demand(mixes: dict[str, dict[str, dict[str, int]]]) -> list[dict]:
    rules = []
    for area, area_mixes in mixes.items():
        for day_type in DAY_TYPES:
            for a, b, c in _windows(area_mixes[day_type]) if day_type in area_mixes else []:
                rules.append({
                    "areaId": area, "days": DAY_TYPES[day_type], "date": None,
                    "from": hhmm(a), "to": hhmm(b), "min": c,
                    "max": c if area in MAX_IS_MIN else None,
                })
    return rules


def _restrictions(people: list[dict], karenz: set[str], rng: random.Random) -> list[dict]:
    result = []
    for p in people:
        if p["id"] in karenz or rng.random() > 0.55:
            continue
        # Every-day NEVER only on shifts of scaled areas, and never on all shifts of an area.
        candidates = [s[0] for s in SHIFTS if s[1] in p["areaIds"]]
        seen = set()
        for _ in range(rng.randint(1, 6)):
            sid = rng.choice(candidates)
            weekday = 0 if rng.random() < 0.6 else rng.randint(1, 7)
            kind = "NEVER" if rng.random() < 0.8 else "ON_REQUEST"
            area = _shift(sid)[1]
            if kind == "NEVER" and weekday == 0:
                area_shifts = [s[0] for s in SHIFTS if s[1] == area]
                blocked = {s for s, w, k in seen if w == 0 and k == "NEVER"} | {sid}
                if area not in SCALED_AREAS or all(s in blocked for s in area_shifts):
                    continue
            if any(s == sid for s, _, _ in seen):  # one restriction per person and shift
                continue
            seen.add((sid, weekday, kind))
            result.append({"personId": p["id"], "shiftId": sid, "weekday": weekday, "kind": kind})
    return result


def _allowed(pid: str, sid: str, day: date, never: set) -> bool:
    return (pid, sid, 0) not in never and (pid, sid, day.isoweekday()) not in never


def _wishes_and_fixed(people, days, absences, karenz, restrictions, rng):
    absent = {(a["personId"], a["date"]) for a in absences}
    never = {(r["personId"], r["shiftId"], r["weekday"]) for r in restrictions if r["kind"] == "NEVER"}
    wishes, fixed = [], []
    used: set[tuple[str, str]] = set()
    free_people = [p for p in people if p["id"] not in karenz]
    for p in free_people:
        if rng.random() > 0.6:
            continue
        shifts = [s[0] for s in SHIFTS if s[1] in p["areaIds"]]
        for d in sorted(rng.sample(days, rng.randint(1, 6))):
            key = (p["id"], d.isoformat())
            if key in absent or key in used:
                continue
            kind = rng.choices(["OFF", "DUTY", "SHIFT", "NOT_SHIFT"], weights=[60, 10, 20, 10])[0]
            sid = None
            if kind in ("SHIFT", "NOT_SHIFT"):
                options = [s for s in shifts if kind == "NOT_SHIFT" or _allowed(p["id"], s, d, never)]
                if options:
                    sid = rng.choice(options)
                else:
                    kind = "OFF"  # no shift of the person is possible that day
            used.add(key)
            wishes.append({"personId": p["id"], "date": d.isoformat(), "kind": kind, "shiftId": sid})
    # A few fixed duties (scaled areas only, from day 3 on so the previous month cannot clash) and days off.
    late_days = days[2:]
    for i, p in enumerate(rng.sample(free_people, min(len(free_people), max(2, round(len(people) * 0.05))))):
        for _ in range(10):
            d = rng.choice(late_days)
            key = (p["id"], d.isoformat())
            if key in absent or key in used:
                continue
            if i % 3 == 2:
                fixed.append({"personId": p["id"], "date": d.isoformat(), "shiftId": None})
            else:
                options = [s[0] for s in SHIFTS if s[1] in p["areaIds"] and s[1] in SCALED_AREAS
                           and _allowed(p["id"], s[0], d, never)]
                if not options:
                    continue
                fixed.append({"personId": p["id"], "date": d.isoformat(), "shiftId": rng.choice(options)})
            used.add(key)
            break
    wishes.sort(key=lambda w: (w["personId"], w["date"]))
    fixed.sort(key=lambda f: (f["personId"], f["date"]))
    return wishes, fixed


def _previous_duties(people, first_day, karenz, restrictions, nights, rng):
    """Last 7 days of the previous month. The night duties that reach into the next morning are staffed
    as in the month itself (nights: shift id -> count); everybody else works blocks of 2-4 duties with
    2-3 days off. Rest between duties is at least 11 h, at most 4 duties in a row, forward rotation."""
    never = {(r["personId"], r["shiftId"], r["weekday"]) for r in restrictions if r["kind"] == "NEVER"}
    days = [first_day - timedelta(days=k) for k in range(7, 0, -1)]
    staff = [p for p in people if p["id"] not in karenz]
    state = {p["id"]: {"end": None, "start": None, "run": 0} for p in staff}
    pattern = {}
    for p in staff:
        cycle = []
        while len(cycle) < 14:
            cycle += [True] * rng.randint(2, 4) + [False] * rng.randint(2, 3)
        offset = rng.randrange(7)
        pattern[p["id"]] = cycle[offset:offset + 7]

    def fits(pid: str, sid: str, i: int) -> bool:
        st = state[pid]
        s, _ = _shift_interval(sid)
        return (_allowed(pid, sid, days[i], never) and st["run"] < 4
                and (st["end"] is None or i * DAY + s - st["end"] >= 11 * 60)
                and (st["start"] is None or s >= st["start"]))

    result = []
    for i, d in enumerate(days):
        assigned: dict[str, str] = {}
        for sid, count in nights.items():
            area = _shift(sid)[1]
            for _ in range(count):
                options = [p["id"] for p in staff if area in p["areaIds"] and p["id"] not in assigned
                           and fits(p["id"], sid, i)]
                if options:
                    # prefer people who are already on nights, then people the pattern has on duty
                    options.sort(key=lambda pid: (state[pid]["start"] != _shift_interval(sid)[0],
                                                  not pattern[pid][i], rng.random()))
                    assigned[options[0]] = sid
        for p in staff:
            if p["id"] in assigned or not pattern[p["id"]][i]:
                continue
            options = [s[0] for s in SHIFTS if s[1] in p["areaIds"] and s[1] not in MAX_IS_MIN
                       and _shift_interval(s[0])[1] <= DAY and fits(p["id"], s[0], i)]
            if options:
                assigned[p["id"]] = rng.choice(options)
        for p in staff:
            st = state[p["id"]]
            sid = assigned.get(p["id"])
            if sid is None:
                st["start"], st["run"] = None, 0
                continue
            s, e = _shift_interval(sid)
            st["end"], st["start"], st["run"] = i * DAY + e, s, st["run"] + 1
            result.append({"personId": p["id"], "date": d.isoformat(), "shiftId": sid})
    result.sort(key=lambda x: (x["personId"], x["date"]))
    return result


def _history(people, month, holidays, karenz, rng):
    result = []
    holiday_months = [h["date"][:7] for h in holidays]
    for k in (3, 2, 1):
        m = add_months(month, -k)
        for p in people:
            if p["id"] in karenz:
                entry = dict(nights=0, weekendsWorked=0, holidays=0, unpopular=0, wishes=0, wishesMet=0, hours=0.0)
            else:
                nights = rng.randint(2, 6) if set(p["areaIds"]) - {"TV", "TS"} else 0
                wishes = rng.randint(0, 6)
                entry = dict(
                    nights=nights,
                    weekendsWorked=rng.randint(1, 4),
                    holidays=rng.randint(0, min(2, holiday_months.count(m))),
                    unpopular=nights,
                    wishes=wishes,
                    wishesMet=rng.randint(max(0, wishes - 2), wishes),
                    hours=round(p["weeklyHours"] * 4.33 + rng.uniform(-10, 10), 1),
                )
            result.append({"personId": p["id"], "month": m, "source": "import", **entry, "hoursAccountAfter": None,
                           "compRestBalanceAfter": None, "compRestUsed": 0, "compRestEarned": entry["holidays"]})
    return result


def generate(month: str, seed: int = 1, people: int = 150, areas: list[str] | None = None,
             demand_factor: float = 1.0) -> dict:
    """Build a synthetic dataset for one planning month."""
    return generate_with_mixes(month, seed, people, areas, demand_factor)[0]


def generate_with_mixes(month: str, seed: int = 1, people: int = 150, areas: list[str] | None = None,
                        demand_factor: float = 1.0) -> tuple[dict, dict]:
    """The dataset and the shift mixes behind its demand (area -> WD/WE/HOL -> shift id -> count)."""
    year, _ = parse_month(month)
    area_ids = [a[0] for a in AREAS if areas is None or a[0] in areas]
    if not area_ids or not any(a in SCALED_AREAS for a in area_ids):
        raise ValueError("at least one of AN, DR, TV is needed")
    days = month_days(month)
    first_history_year = int(add_months(month, -3)[:4])
    holidays = [h for y in range(first_history_year, year + 1) for h in austrian_holidays(y)]

    staff = _people(people, area_ids, random.Random(f"{seed}-people"))
    absences, karenz = _absences(staff, days, random.Random(f"{seed}-absences"))

    # Size the scaled areas so that the demand is about TARGET_RATIO of the staff's capacity. The
    # one-person posts are fixed; the rest is split over the scaled areas like the staff's hours.
    holiday_dates = {h["date"] for h in holidays}
    draft = {"holidays": holidays, "people": staff, "periods": [{"month": month, "absences": absences}]}
    capacity = capacity_hours(draft, month)
    fixed_mixes = _size_mixes([a for a in area_ids if a not in SCALED_AREAS], days, holiday_dates, {})
    draft.update(areas=[{"id": a} for a in fixed_mixes], demand=_demand(fixed_mixes))
    scaled_total = TARGET_RATIO * sum(capacity.values()) - sum(demand_hours(draft, month).values())
    share = {a: 0.0 for a in area_ids if a in SCALED_AREAS}
    for p in staff:
        own = [a for a in p["areaIds"] if a in share]
        for a in own:
            share[a] += capacity[p["id"]] / len(own)
    targets = {a: scaled_total * v / sum(share.values()) for a, v in share.items()}
    mixes = _size_mixes(area_ids, days, holiday_dates, targets)
    demand = _demand(mixes)
    nights = {sid: k for m in mixes.values() for sid, k in m.get("WD", {}).items() if _shift_interval(sid)[1] > DAY}
    if demand_factor != 1.0:
        for r in demand:
            r["min"] = max(1, round(r["min"] * demand_factor))
            if r["max"] is not None:
                r["max"] = max(1, round(r["max"] * demand_factor))

    restrictions = _restrictions(staff, karenz, random.Random(f"{seed}-restrictions"))
    wishes, fixed = _wishes_and_fixed(staff, days, absences, karenz, restrictions,
                                      random.Random(f"{seed}-wishes"))
    previous = _previous_duties(staff, days[0], karenz, restrictions, nights, random.Random(f"{seed}-previous"))
    history = _history(staff, month, holidays, karenz, random.Random(f"{seed}-history"))

    dataset = {
        "format": FORMAT,
        "version": 2,
        "source": "synthetic",
        "areas": [{"id": a, "name": n, "color": c} for a, n, c in AREAS if a in area_ids],
        "shifts": [{"id": s[0], "areaId": s[1], "code": s[2], "name": s[3], "start": s[4], "end": s[5],
                    "category": s[6], "color": s[7], "unpopular": s[6] == "NIGHT"}
                   for s in SHIFTS if s[1] in area_ids],
        "demand": demand,
        "holidays": holidays,
        "absenceTypes": [{"code": c, "name": n, "reducesTarget": t, "chargesAccount": a, "compensatoryRest": r,
                          "color": col} for c, n, t, a, r, col in ABSENCE_TYPES],
        "people": staff,
        "restrictions": restrictions,
        "history": history,
        "periods": [{"month": month, "absences": absences, "wishes": wishes, "fixed": fixed,
                     "previousDuties": previous}],
    }
    return dataset, mixes


def dumps(dataset: dict) -> str:
    return json.dumps(dataset, ensure_ascii=False, indent=1) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a synthetic dataset.")
    parser.add_argument("--month", required=True, help="planning month YYYY-MM")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--people", type=int, default=150)
    parser.add_argument("--areas", help="comma separated area ids (default: all)")
    parser.add_argument("--demand-factor", type=float, default=1.0,
                        help="multiply all staffing levels, e.g. 3 for an impossible month")
    parser.add_argument("--out", help="output file (default: stdout)")
    parser.add_argument("--check", action="store_true", help="print the feasibility check to stderr")
    args = parser.parse_args(argv)
    dataset = generate(args.month, args.seed, args.people, args.areas.split(",") if args.areas else None,
                       args.demand_factor)
    text = dumps(dataset)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    if args.check:
        print(json.dumps(check_feasibility(dataset, args.month), indent=1), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
