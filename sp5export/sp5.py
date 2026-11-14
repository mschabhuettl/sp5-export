"""Read-only import of a Schichtplaner5 folder into a dataset for one planning month.

Only sp5lib.dbf_reader is used; it opens every table with mode "rb". Warnings name SP5 ids and
short names only, never people's names.
"""

import json
import logging
import os
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from fnmatch import fnmatchcase
from itertools import pairwise
from pathlib import Path

from sp5lib import calculations as sp5calc
from sp5lib.color_utils import bgr_to_hex

from .dbf import DbfError, read_table
from .demand import coverage_segments
from .feasibility import rules_for_day
from .holidays import austrian_holidays
from .timeutil import DAY, WEEKDAY_CODES, add_months, hhmm, interval, minutes, month_days

log = logging.getLogger(__name__)

FORMAT = "dispofold/dataset"
VERSION = 2
MAX_DUTY_MINUTES = 12 * 60  # Dispofold's default maxDutyHours; longer shifts make its import fail

HOLIDAY_INDEX = 7  # SP5 day index: 0 = Monday .. 6 = Sunday, 7 = public holiday
HISTORY_MONTHS = 12  # default; Dispofold makes the 52-week average hard only with 11 months of history
MAX_HISTORY_MONTHS = 24
MAX_EXTRA_MONTHS = 12  # --to: at most this many months after the first planning month
PREVIOUS_DAYS = 7
DEFAULT_SHIFT_COLOR = "#E8E8E8"
DEFAULT_ABSENCE_COLOR = "#CCCCCC"
TIME_RANGE = re.compile(r"^\s*(\d{1,2}:\d\d)\s*-\s*(\d{1,2}:\d\d)\s*$")


class Sp5Error(Exception):
    """The folder cannot be imported (missing tables, unreadable mapping)."""


PATTERN_LISTS = ("ignoreShifts", "ignoreGroups", "excludeGroups", "compensatoryRestAbsences", "ignoreAbsences")
CATEGORIES = ("EARLY", "DAY", "LATE", "NIGHT")


def load_mapping(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        mapping = json.load(f)
    validate_mapping(mapping)
    return mapping


def _pattern_error(pattern) -> str | None:
    if not isinstance(pattern, str) or not pattern:
        return "ist kein Text"
    if pattern.startswith("re:"):
        try:
            re.compile(pattern[3:])
        except re.error as e:
            return f"ist kein gültiger regulärer Ausdruck ({e.msg}, Position {e.pos})"
    return None


def validate_mapping(mapping) -> None:
    """Check the whole mapping before anything is read; all problems are reported in one German error."""
    if not isinstance(mapping, dict):
        raise Sp5Error("die Zuordnung muss ein JSON-Objekt sein")
    errors = []
    for key in sorted(set(mapping) - {"areas", "shifts", "groups", *PATTERN_LISTS}):
        errors.append(f"unbekannter Schlüssel '{key}'")
    for key in ("areas", "shifts", "groups"):
        if key not in mapping:
            errors.append(f"Pflichtschlüssel '{key}' fehlt")
    for key in ("areas", "shifts", "groups", *PATTERN_LISTS):
        if key in mapping and not isinstance(mapping[key], list):
            errors.append(f"'{key}' muss eine Liste sein")
    areas = mapping.get("areas") if isinstance(mapping.get("areas"), list) else []
    shifts = mapping.get("shifts") if isinstance(mapping.get("shifts"), list) else []
    groups = mapping.get("groups") if isinstance(mapping.get("groups"), list) else []
    if "areas" in mapping and not areas:
        errors.append("'areas' ist leer")

    area_ids = set()
    for i, a in enumerate(areas, 1):
        where = f"areas, Eintrag {i}"
        if not isinstance(a, dict):
            errors.append(f"{where}: muss ein Objekt sein")
            continue
        if not isinstance(a.get("id"), str) or not a["id"].strip():
            errors.append(f"{where}: 'id' fehlt oder ist kein Text")
        elif a["id"] in area_ids:
            errors.append(f"{where}: Bereich '{a['id']}' doppelt")
        else:
            area_ids.add(a["id"])
        for key in ("name", "color"):
            if not isinstance(a.get(key), str):
                errors.append(f"{where}: '{key}' fehlt oder ist kein Text")

    def rule(where: str, r) -> bool:
        if not isinstance(r, dict):
            errors.append(f"{where}: muss ein Objekt sein")
            return False
        if "match" not in r:
            errors.append(f"{where}: 'match' fehlt")
        elif (problem := _pattern_error(r["match"])) is not None:
            errors.append(f"{where}: Muster {r['match']!r} {problem}")
        return True

    for i, r in enumerate(shifts, 1):
        where = f"shifts, Eintrag {i}"
        if not rule(where, r):
            continue
        if not isinstance(r.get("areaId"), str) or r["areaId"] not in area_ids:
            errors.append(f"{where}: 'areaId' {r.get('areaId')!r} ist kein Bereich aus 'areas'")
        if "category" in r and r["category"] not in CATEGORIES:
            errors.append(f"{where}: 'category' muss {', '.join(CATEGORIES)} sein")
        if "unpopular" in r and not isinstance(r["unpopular"], bool):
            errors.append(f"{where}: 'unpopular' muss true oder false sein")
    for i, r in enumerate(groups, 1):
        where = f"groups, Eintrag {i}"
        if not rule(where, r):
            continue
        ids = r.get("areaIds")
        if not isinstance(ids, list) or not ids:
            errors.append(f"{where}: 'areaIds' muss eine nicht leere Liste sein")
        else:
            for a in ids:
                if not isinstance(a, str) or a not in area_ids:
                    errors.append(f"{where}: 'areaIds' enthält {a!r}, keinen Bereich aus 'areas'")
    for key in PATTERN_LISTS:
        if isinstance(mapping.get(key), list):
            for i, p in enumerate(mapping[key], 1):
                if (problem := _pattern_error(p)) is not None:
                    errors.append(f"{key}, Eintrag {i}: Muster {p!r} {problem}")
    if errors:
        raise Sp5Error("; ".join(errors))


def matches(pattern: str, value: str) -> bool:
    """Glob pattern (fnmatch, case-sensitive) or, with prefix "re:", a regular expression."""
    if pattern.startswith("re:"):
        return re.match(pattern[3:], value) is not None
    return fnmatchcase(value, pattern)


def _first(rules: list[dict], value: str) -> dict | None:
    return next((r for r in rules if matches(r["match"], value)), None)


def _any(patterns: list[str], value: str) -> bool:
    return any(matches(p, value) for p in patterns)


def table_path(folder: Path, name: str) -> Path | None:
    wanted = f"{name}.dbf".lower()
    for entry in os.listdir(folder):
        if entry.lower() == wanted:
            return folder / entry
    return None


# Bases of the nominal hours in 5EMPL.CALCBASE.
BASE_NAMES = {0: "Tag", 1: "Woche", 2: "Monat", 3: "gesamt"}


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _date(value) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _category(start: str, end: str) -> str:
    s, e = interval(start, end)
    if e > DAY or s >= 17 * 60:
        return "NIGHT"
    if s < 7 * 60:
        return "EARLY"
    return "LATE" if s >= 10 * 60 else "DAY"


def _norm_time(t: str) -> str:
    return hhmm(minutes(t))


class _Import:
    def __init__(self, folder: Path, months: list[str], mapping: dict, history_months: int = HISTORY_MONTHS,
                 areas_from_history: int | None = None):
        self.folder, self.mapping = folder, mapping
        self.month, self.months = months[0], months
        self.history_months, self.areas_from_history = history_months, areas_from_history
        self.warnings: list[str] = []
        self.days = [d for m in months for d in month_days(m)]
        self.first, self.last = self.days[0], self.days[-1]
        self.history_start = date.fromisoformat(add_months(self.month, -history_months) + "-01")
        self.report_rows: list[tuple[str, str, int, bool]] = []
        self._group_regular: dict = {}
        self._tables: dict[str, list[dict]] = {}
        self.employment: dict[str, tuple[date | None, date | None]] = {}

    def warn(self, text: str) -> None:
        self.warnings.append(text)

    def table(self, name: str, required: bool = False, optional: bool = False) -> list[dict]:
        """Rows of a table, read once per import."""
        if name not in self._tables:
            path = table_path(self.folder, name)
            if path is None:
                if required:
                    raise Sp5Error(f"Tabelle {name} fehlt im SP5-Ordner")
                if not optional:
                    self.warn(f"Tabelle {name} fehlt, wird als leer behandelt")
                self._tables[name] = []
            else:
                try:
                    self._tables[name] = read_table(path)
                except DbfError as e:
                    raise Sp5Error(f"Tabelle {name}: {e}") from None
                if required and not self._tables[name]:
                    raise Sp5Error(f"Tabelle {name} ist leer")
        return self._tables[name]

    # ── master data ──────────────────────────────────────────

    def shifts(self) -> None:
        area_ids = {a["id"] for a in self.mapping["areas"]}
        ignore = self.mapping.get("ignoreShifts", [])
        self.shift_by_sp5: dict[int, dict] = {}
        for r in self.table("5SHIFT", required=True):
            sp5_id, short = r.get("ID"), _text(r.get("SHORTNAME"))
            if r.get("HIDE") or _any(ignore, short):
                continue
            rule = _first(self.mapping["shifts"], short)
            if rule is None or rule["areaId"] not in area_ids:
                self.warn(f"Schicht ohne Bereich ignoriert: SP5-Id {sp5_id}, Kürzel {short!r}")
                continue
            times = Counter()
            for k in range(8):
                m = TIME_RANGE.match(_text(r.get(f"STARTEND{k}")))
                if m:
                    times[(_norm_time(m.group(1)), _norm_time(m.group(2)))] += 1
            if not times:
                self.warn(f"Schicht ohne Dienstzeit ignoriert: SP5-Id {sp5_id}, Kürzel {short!r}")
                continue
            (start, end), _ = times.most_common(1)[0]
            if len(times) > 1:
                self.warn(f"Schicht {short!r} (SP5-Id {sp5_id}) hat je Wochentag andere Zeiten; "
                          f"verwendet wird {start}-{end}")
            s, e = interval(start, end)
            if e - s > MAX_DUTY_MINUTES:
                self.warn(f"Schicht {short!r} (SP5-Id {sp5_id}) dauert mehr als 12 Stunden ({start}-{end}) und "
                          "wird nicht übernommen")
                continue
            category = rule.get("category") or _category(start, end)
            color = r.get("COLORBK")
            self.shift_by_sp5[sp5_id] = {
                "id": f"S{sp5_id}", "areaId": rule["areaId"], "code": short,
                "name": _text(r.get("NAME")) or short, "start": start, "end": end, "category": category,
                "color": bgr_to_hex(color) if isinstance(color, int) and color > 0 else DEFAULT_SHIFT_COLOR,
                "unpopular": rule.get("unpopular", category == "NIGHT"),
            }

    def people(self) -> None:
        own, parent, unmapped = {}, {}, []
        exclude, ignore = self.mapping.get("excludeGroups", []), self.mapping.get("ignoreGroups", [])
        for g in self.table("5GROUP", required=True):
            name, gid = _text(g.get("NAME")), g.get("ID")
            parent[gid] = g.get("SUPERID") or 0
            if _any(exclude, name):
                own[gid] = "exclude"
            elif (rule := _first(self.mapping["groups"], name)) is not None:
                own[gid] = set(rule["areaIds"])
            elif _any(ignore, name):
                own[gid] = "ignore"
            else:
                unmapped.append(g)
        # A subgroup without its own rule takes the rule of the nearest parent group (SUPERID).
        groups = {}
        for gid in parent:
            seen, cur = set(), gid
            while cur and cur not in own and cur not in seen:
                seen.add(cur)
                cur = parent.get(cur, 0)
            if cur in own:
                groups[gid] = own[cur]
        for g in unmapped:
            if g.get("ID") not in groups:
                self.warn(f"Gruppe ohne Zuordnung: SP5-Id {g.get('ID')}, Kürzel {_text(g.get('SHORTNAME'))!r}")
        self.excluded_groups = {gid for gid, v in groups.items() if v == "exclude"}
        member = defaultdict(list)
        for a in self.table("5GRASG", required=True):
            if groups.get(a.get("GROUPID"), "ignore") != "ignore":
                member[a.get("EMPLOYEEID")].append(groups[a.get("GROUPID")])
        self.person_by_sp5: dict[int, dict] = {}
        self.no_area: dict[int, tuple[dict, date | None, date | None]] = {}
        dropped = self.dropped = Counter()
        self.bases, self.stale = Counter(), 0
        for e in self.table("5EMPL", required=True):
            eid = e.get("ID")
            start, end = _date(e.get("EMPSTART")), _date(e.get("EMPEND"))
            if e.get("HIDE"):
                dropped["ausgeblendet"] += 1
                continue
            if start and end and end < start:
                dropped["Austritt vor Eintritt"] += 1
                continue
            if (start and start > self.last) or (end and end < self.first):
                dropped["nicht beschäftigt im Monat"] += 1
                continue
            if "exclude" in member[eid]:
                dropped["ausgeschlossene Gruppe"] += 1
                continue
            areas = set().union(*member[eid]) if member[eid] else set()
            if not areas:
                self.no_area[eid] = (e, start, end)  # decided after the history (areas_from_history)
                continue
            self._add_person(e, start, end, areas)

    def _add_person(self, e: dict, start: date | None, end: date | None, areas: set[str]) -> None:
        eid = e.get("ID")
        base = int(e.get("CALCBASE") or 0)
        self.bases[base] += 1
        hours = self._weekly_hours(e, base, start, end)
        if hours is not None and base != 1 and abs(hours - float(e.get("HRSWEEK") or 0)) >= 0.005:
            self.stale += 1
        if hours is None or hours <= 0:
            if hours is not None:
                self.warn(f"Mitarbeiter E{eid} hat keine Wochenstunden (CALCBASE {base}) und wird nicht übernommen")
            self.dropped["ohne Wochenstunden"] += 1
            return
        self.employment[f"E{eid}"] = (start, end)
        self.person_by_sp5[eid] = {
            "id": f"E{eid}", "firstName": _text(e.get("FIRSTNAME")), "lastName": _text(e.get("NAME")),
            "shortName": _text(e.get("SHORTNAME")), "weeklyHours": hours,
            "areaIds": [a["id"] for a in self.mapping["areas"] if a["id"] in areas], "hoursAccount": 0.0,
            "compRestBalance": 0, "familyRules": [], "externalId": str(eid),
            "entryDate": start.isoformat() if start else None, "exitDate": end.isoformat() if end else None,
        }

    def _people_summary(self) -> None:
        partly = sum(1 for start, end in self.employment.values()
                     if (start and start > self.first) or (end and end < self.last))
        if partly:
            self.warn(f"{partly} Mitarbeiter nur teilweise im Monat beschäftigt; Ein- und Austritt sind übernommen")
        for reason, n in sorted(self.dropped.items()):
            self.warn(f"{n} Mitarbeiter nicht übernommen ({reason})")
        self.warn("Wochenstunden nach CALCBASE: "
                  + ", ".join(f"{b} ({BASE_NAMES[b]}) {self.bases[b]}" for b in BASE_NAMES)
                  + f"; HRSWEEK weicht bei {self.stale} davon ab")
        self.warn("Stundenkonto und offene Ersatzruhetage werden aus SP5 nicht übernommen (0)")

    def _weekly_hours(self, e: dict, base: int, start: date | None, end: date | None) -> float | None:
        """Weekly hours as SP5 computes the nominal hours (sp5lib calculations.get_nominal_hours, by CALCBASE),
        rounded to two decimals. None when they cannot be computed (warned here)."""
        eid = e.get("ID")
        if base == 0:
            days = sum(sp5calc.parse_day_mask(str(e.get("WORKDAYS") or ""), 8)[:7])
            hours = float(e.get("HRSDAY") or 0) * days
        elif base == 1:
            hours = float(e.get("HRSWEEK") or 0)
        elif base == 2:
            hours = float(e.get("HRSMONTH") or 0) * 12 / 52
        elif base == 3:
            if not (start and end):
                self.warn(f"Mitarbeiter E{eid} hat Gesamtstunden (CALCBASE 3) ohne abgeschlossene Beschäftigung"
                          " (EMPSTART bis EMPEND) und wird nicht übernommen")
                return None
            hours = float(e.get("HRSTOTAL") or 0) / (((end - start).days + 1) / 7)
        else:
            raise Sp5Error(f"Mitarbeiter E{eid}: unbekannte Berechnungsgrundlage CALCBASE {base}")
        return round(hours, 2)

    def restrictions(self) -> list[dict]:
        grades, odd = defaultdict(dict), 0
        for r in self.table("5RESTR"):
            person, shift = self.person_by_sp5.get(r.get("EMPLOYEEID")), self.shift_by_sp5.get(r.get("SHIFTID"))
            grade = r.get("RESTRICT")
            if person is None or shift is None or grade == 0:
                continue
            if grade not in (1, 2):
                odd += 1  # SP5 treats an existing row as a restriction
            kind = "ON_REQUEST" if grade == 1 else "NEVER"
            grades[(person["id"], shift["id"])][int(r.get("WEEKDAY") or 0)] = kind
        result, holiday_only = [], 0
        for (pid, sid), by_day in sorted(grades.items()):
            kinds = {by_day.get(i) for i in range(7)}
            if len(kinds) == 1 and None not in kinds:
                result.append({"personId": pid, "shiftId": sid, "weekday": 0, "kind": kinds.pop()})
                continue
            for i in range(7):
                if i in by_day:
                    result.append({"personId": pid, "shiftId": sid, "weekday": i + 1, "kind": by_day[i]})
            holiday_only += HOLIDAY_INDEX in by_day
        if holiday_only:
            self.warn(f"{holiday_only} Einschränkungen nur für Feiertage nicht übernommen")
        if odd:
            self.warn(f"{odd} Einschränkungen ohne gültigen Grad (RESTRICT) als 'nie' übernommen")
        return result

    def absence_types(self) -> list[dict]:
        compensatory = self.mapping.get("compensatoryRestAbsences", [])
        ignore = self.mapping.get("ignoreAbsences", [])
        types = {}
        for t in self.table("5LEAVT"):
            code = _text(t.get("SHORTNAME"))
            if not code or _any(ignore, code):
                continue
            color = t.get("COLORBK")
            # SP5 credits an absence to the actual hours when CHARGETYP is set; DEDUCTACT and DEDUCTOVT take
            # the hours from the actual hours or the overtime account (time off in lieu).
            credited = bool(t.get("CHARGETYP"))
            types[t.get("ID")] = {
                "code": code, "name": _text(t.get("NAME")) or code, "reducesTarget": credited,
                "chargesAccount": credited and bool(t.get("DEDUCTACT") or t.get("DEDUCTOVT")),
                "compensatoryRest": _any(compensatory, code),
                "color": bgr_to_hex(color) if isinstance(color, int) and color > 0 else DEFAULT_ABSENCE_COLOR,
            }
        self.absence_by_sp5 = types
        codes = list(dict.fromkeys(t["code"] for t in types.values()))
        return [next(t for t in types.values() if t["code"] == c) for c in codes]

    def absences(self) -> list[dict]:
        types = self.absence_by_sp5
        seen, result, double = set(), [], 0
        for a in self.table("5ABSEN"):
            person, kind, day = self.person_by_sp5.get(a.get("EMPLOYEEID")), types.get(a.get("LEAVETYPID")), \
                _date(a.get("DATE"))
            if person is None or kind is None or day is None or not self.first <= day <= self.last:
                continue
            start, end = self.employment[person["id"]]
            if (start and day < start) or (end and day > end):
                continue
            key = (person["id"], day.isoformat())
            if key in seen:
                double += 1
                continue
            seen.add(key)
            result.append({"personId": key[0], "date": key[1], "typeCode": kind["code"]})
        if double:
            self.warn(f"{double} weitere Abwesenheiten am selben Tag ignoriert")
        return sorted(result, key=lambda x: (x["personId"], x["date"]))

    def _absent(self, start: date, end: date, compensatory: bool = False,
                people: dict[int, dict] | None = None) -> set[tuple[str, date]]:
        """(person id, date) of full-day absences of exported types in [start, end]."""
        result, people = set(), self.person_by_sp5 if people is None else people
        for a in self.table("5ABSEN"):
            person, kind, d = people.get(a.get("EMPLOYEEID")), \
                self.absence_by_sp5.get(a.get("LEAVETYPID")), _date(a.get("DATE"))
            if person and kind and d and start <= d <= end and not a.get("INTERVAL") \
                    and (kind["compensatoryRest"] or not compensatory):
                result.add((person["id"], d))
        return result

    def holidays(self) -> tuple[list[dict], set[str]]:
        """Full public holidays for the dataset, and all SP5 holiday dates (half days included), which
        SP5 uses for the holiday demand slot."""
        # From the history (or at least the day before the first month: night demand crosses midnight).
        years = range(min(self.history_start, self.first - timedelta(days=1)).year, self.last.year + 1)
        full, sp5_dates = {}, set()
        for h in self.table("5HOLID"):
            d = _date(h.get("DATE"))
            if d and d.year in years:
                sp5_dates.add(d.isoformat())
                if not h.get("INTERVAL"):
                    full[d.isoformat()] = _text(h.get("NAME")) or "Feiertag"
        if not sp5_dates:
            self.warn("Keine Feiertage in SP5 (5HOLID); verwendet werden die österreichischen Feiertage")
            result = [h for y in years for h in austrian_holidays(y)]
            return result, {h["date"] for h in result}
        half = len(sp5_dates) - len(full)
        if half:
            self.warn(f"{half} halbe Feiertage (5HOLID INTERVAL) nicht als Feiertag übernommen")
        return [{"date": d, "name": n} for d, n in sorted(full.items())], sp5_dates

    # ── demand ───────────────────────────────────────────────

    def demand(self, sp5_holiday_dates: set[str], holiday_dates: set[str]) -> list[dict]:
        """5SHDEM (per group, shift and day index) and 5SPDEM (per date) summed per area into windows."""
        regular = defaultdict(lambda: [0, 0, True])  # (area, idx, sp5 shift) -> [min, max, max complete]
        for r in self.table("5SHDEM"):
            shift = self.shift_by_sp5.get(r.get("SHIFTID"))
            if shift is None or r.get("GROUPID") in self.excluded_groups:
                continue
            entry = regular[(shift["areaId"], int(r.get("WEEKDAY") or 0), r.get("SHIFTID"))]
            entry[0] += int(r.get("MIN") or 0)
            entry[1] += int(r.get("MAX") or 0)
            entry[2] = entry[2] and bool(r.get("MAX"))
        special = defaultdict(lambda: defaultdict(lambda: [0, 0, True]))  # date -> (group, shift) -> ...
        for r in self.table("5SPDEM"):
            shift, d = self.shift_by_sp5.get(r.get("SHIFTID")), _date(r.get("DATE"))
            if shift is not None and d is not None and r.get("GROUPID") not in self.excluded_groups:
                e = special[d][(r.get("GROUPID"), r.get("SHIFTID"))]
                e[0] += int(r.get("MIN") or 0)
                e[1] += int(r.get("MAX") or 0)
                e[2] = e[2] and bool(r.get("MAX"))
        if special:
            # SP5 keeps special demand per group; the regular rows of the same group and shift are replaced.
            groups = defaultdict(lambda: [0, 0, True])
            for r in self.table("5SHDEM"):
                if r.get("SHIFTID") in self.shift_by_sp5 and r.get("GROUPID") not in self.excluded_groups:
                    e = groups[(r.get("GROUPID"), int(r.get("WEEKDAY") or 0), r.get("SHIFTID"))]
                    e[0] += int(r.get("MIN") or 0)
                    e[1] += int(r.get("MAX") or 0)
                    e[2] = e[2] and bool(r.get("MAX"))
            self._group_regular = groups

        areas = [a["id"] for a in self.mapping["areas"]]

        def mix_for_index(area: str, idx: int) -> dict[int, list]:
            return {sid: v for (a, i, sid), v in regular.items() if a == area and i == idx and (v[0] or v[1])}

        def mix_for_date(area: str, d: date) -> dict[int, list]:
            idx = HOLIDAY_INDEX if d.isoformat() in sp5_holiday_dates else d.weekday()
            mix = {sid: list(v) for sid, v in mix_for_index(area, idx).items()}
            for (gid, sid), v in special.get(d, {}).items():
                if self.shift_by_sp5[sid]["areaId"] != area:
                    continue
                old = self._group_regular.get((gid, idx, sid), [0, 0, True])
                cur = mix.setdefault(sid, [0, 0, True])
                cur[0] += v[0] - old[0]
                cur[1] += v[1] - old[1]
                cur[2] = cur[2] and v[2]
            return mix

        def windows(today: dict, yesterday: dict) -> list[tuple]:
            def spans(mix, pos):
                return [(*interval(self.shift_by_sp5[sid]["start"], self.shift_by_sp5[sid]["end"]), v[pos])
                        for sid, v in mix.items() if v[pos]]
            mins = coverage_segments(spans(today, 0), spans(yesterday, 0))
            limited = all(v[2] for v in today.values()) and all(v[2] for v in yesterday.values())
            maxes = coverage_segments(spans(today, 1), spans(yesterday, 1)) if limited else []
            result = []
            for a, b, c in mins:
                # split at the boundaries of the maximum, too
                cuts = sorted({a, b} | {x for s, e, _ in maxes for x in (s, e) if a < x < b})
                for lo, hi in pairwise(cuts):
                    top = next((m for s, e, m in maxes if s <= lo < e), None) if limited else None
                    result.append((lo, hi, c, max(top, c) if top is not None else None))
            return result

        rules = []
        has_holiday = {a: bool(mix_for_index(a, HOLIDAY_INDEX)) for a in areas}
        for area in areas:
            by_code = {}
            for idx, code in enumerate(WEEKDAY_CODES):
                by_code[code] = tuple(windows(mix_for_index(area, idx), mix_for_index(area, (idx - 1) % 7)))
            groups = defaultdict(list)
            for code, w in by_code.items():
                groups[w].append(code)
            for w, codes in groups.items():
                rules += self._rules(area, codes, None, w)
            if has_holiday[area]:
                hol = mix_for_index(area, HOLIDAY_INDEX)
                rules += self._rules(area, ["HOL"], None, windows(hol, hol))
        # Dated rules where the generic rules do not match the exact staffing of a day (the day after a
        # holiday, holidays, special demand, the first day of the month).
        draft = {"demand": rules}
        dated = []
        for area in areas:
            for d in self.days:
                exact = windows(mix_for_date(area, d), mix_for_date(area, d - timedelta(days=1)))
                generic = [(*interval(r["from"], r["to"]), r["min"], r["max"])
                           for r in rules_for_day(draft, area, d, holiday_dates)]
                if sorted(generic) != sorted(exact):
                    if exact:
                        dated += self._rules(area, [], d.isoformat(), exact)
                    else:
                        self.warn(f"Bereich {area}: am {d} kein Bedarf in SP5, es gilt der Wochentagsbedarf")
        return rules + dated

    @staticmethod
    def _rules(area: str, days: list[str], on: str | None, windows) -> list[dict]:
        return [{"areaId": area, "days": days, "date": on, "from": hhmm(a), "to": hhmm(b), "min": c, "max": m}
                for a, b, c, m in windows]

    # ── duties ───────────────────────────────────────────────

    def duties(self, start: date, end: date, people: dict[int, dict] | None = None,
               quiet: bool = False) -> list[tuple[str, date, dict]]:
        """(person id, date, shift) from 5MASHI and 5SPSHI in [start, end], in-scope only. `people` replaces the
        exported people (SP5 id -> person); `quiet` leaves out the warnings (a second pass over the same days)."""
        result, unknown = {}, 0
        people = self.person_by_sp5 if people is None else people
        warn = (lambda text: None) if quiet else self.warn
        # 5SPSHI rows with a shift replace the duty of the day (sp5lib: _replaced_days, get_schedule), whatever
        # their TYPE; sp5lib counts them as work time, only its staffing count leaves TYPE != 0 out, and the
        # meaning of TYPE is not documented. Rows without a shift carry their own times and have no shift here.
        special = [r for r in self.table("5SPSHI") if self._in_range(r, start, end)]
        rows = [r for r in special if r.get("SHIFTID")]
        replaced = {id(r) for r in rows}
        other_type = sum(1 for r in rows if r.get("TYPE"))
        if other_type:
            warn(f"{other_type} Sonderdienste {start}..{end} (5SPSHI) mit TYPE ungleich 0 als Dienst übernommen")
        without_shift = len(special) - len(rows)
        if without_shift:
            warn(f"{without_shift} Sonderdienste {start}..{end} (5SPSHI) ohne Schicht nicht übernommen")
        # Then the actual plan (5MASHI TYPE 1 is the target plan), then cycle duties.
        rows += [r for r in self.table("5MASHI") if r.get("TYPE") != 1]
        rows += sp5calc.expand_cycle_assignments(
            self.table("5CYASS", optional=True), cycles=self.table("5CYCLE", optional=True),
            cycle_entries=self.table("5CYENT", optional=True),
            cycle_exceptions=self.table("5CYEXC", optional=True), von=start, bis=end)
        for r in rows:
            d = _date(r.get("DATE"))
            person = people.get(r.get("EMPLOYEEID"))
            if d is None or person is None or not start <= d <= end:
                continue
            shift = self.shift_by_sp5.get(r.get("SHIFTID"))
            if shift is None:
                unknown += 1
                if id(r) in replaced:  # the deviation still replaces the planned duty of the day
                    result.setdefault((person["id"], d), None)
                continue
            result.setdefault((person["id"], d), shift)
        result = {k: s for k, s in result.items() if s is not None}
        if unknown:
            warn(f"{unknown} Dienste {start}..{end} mit nicht zugeordneter Schicht ignoriert")
        # A full-day absence (5ABSEN) wins over a duty of the same day: SP5 shows both as a conflict, and the
        # dataset has no absences outside the planning month to show it there.
        absent = self._absent(start, end, people=people) & result.keys()
        if absent:
            warn(f"{len(absent)} Dienste {start}..{end} an Tagen mit ganztägiger Abwesenheit nicht übernommen")
        return [(pid, d, s) for (pid, d), s in sorted(result.items(), key=lambda x: (x[0][0], x[0][1]))
                if (pid, d) not in absent]

    @staticmethod
    def _in_range(row: dict, start: date, end: date) -> bool:
        d = _date(row.get("DATE"))
        return d is not None and start <= d <= end

    def history(self, holiday_dates: set[str]) -> list[dict]:
        start = self.history_start
        counters = defaultdict(lambda: {"nights": 0, "weekends": set(), "holidays": 0, "unpopular": 0,
                                        "hours": 0.0, "earned": 0})
        for pid, d, s in self.duties(start, self.first - timedelta(days=1)):
            c = counters[(pid, d.isoformat()[:7])]
            a, b = interval(s["start"], s["end"])
            c["hours"] += (b - a) / 60
            c["nights"] += s["category"] == "NIGHT"
            c["unpopular"] += bool(s["unpopular"])
            c["holidays"] += d.isoformat() in holiday_dates
            c["earned"] += d.isoformat() in holiday_dates and d.weekday() != 6
            if d.weekday() >= 5:
                c["weekends"].add(d.isocalendar()[:2])
        comp_rest = Counter((pid, d.isoformat()[:7])
                            for pid, d in self._absent(start, self.first - timedelta(days=1), compensatory=True))
        result = []
        for k in range(self.history_months, 0, -1):
            m = add_months(self.month, -k)
            days = month_days(m)
            for p in self.person_by_sp5.values():
                # An entry for every month the person was employed in (also without duties: SP5 is the actual
                # record, so no duty means 0 hours), or that has a value. Dispofold's 52-week average is a hard
                # rule only for people with an entry in each of its months.
                entry, leave = self.employment[p["id"]]
                employed = (entry is None or entry <= days[-1]) and (leave is None or leave >= days[0])
                if not employed and (p["id"], m) not in counters and not comp_rest[(p["id"], m)]:
                    continue
                c = counters[(p["id"], m)]
                # Actual values from SP5 (source "import"); SP5 has no hours account or compensatory-rest
                # balance per month. Compensatory rest is earned by holiday duties not on a Sunday (Dispofold's
                # default rule).
                result.append({"personId": p["id"], "month": m, "source": "import", "nights": c["nights"],
                               "weekendsWorked": len(c["weekends"]), "holidays": c["holidays"],
                               "unpopular": c["unpopular"], "wishes": 0, "wishesMet": 0,
                               "hours": round(c["hours"], 2), "hoursAccountAfter": None,
                               "compRestBalanceAfter": None, "compRestUsed": comp_rest[(p["id"], m)],
                               "compRestEarned": c["earned"]})
        return result

    # ── areas from history ───────────────────────────────────

    def areas_from_duties(self) -> None:
        """Compare the areas from groups with the areas worked in the history window (counts only), add areas
        with at least `areas_from_history` duties, and decide about the people without an area from groups."""
        candidates = {eid: {"id": f"E{eid}", "areaIds": []} for eid in self.no_area}
        people = {**self.person_by_sp5, **candidates}
        counts: Counter = Counter()
        if self.history_months:
            for pid, _, s in self.duties(self.history_start, self.first - timedelta(days=1), people, quiet=True):
                counts[(pid, s["areaId"])] += 1
        areas = [a["id"] for a in self.mapping["areas"]]
        group = {p["id"]: set(p["areaIds"]) for p in people.values()}
        for p in people.values():
            for a in areas:
                if counts[(p["id"], a)] or a in group[p["id"]]:
                    self.report_rows.append((p["id"], a, counts[(p["id"], a)], a in group[p["id"]]))
        months = self.history_months
        if months:
            exported = [p["id"] for p in self.person_by_sp5.values()]
            for a in areas:
                worked = sum(1 for pid in exported if counts[(pid, a)] and a not in group[pid])
                idle = sum(1 for pid in exported if a in group[pid] and not counts[(pid, a)])
                self.warn(f"Bereich {a}: {worked} Mitarbeiter mit Diensten in den {months} Monaten davor ohne den "
                          f"Bereich aus Gruppen, {idle} mit dem Bereich aus Gruppen ohne Dienst darin")
            loose = sum(1 for c in candidates.values() if any(counts[(c["id"], a)] for a in areas))
            self.warn(f"{loose} Mitarbeiter ohne Bereich aus Gruppen haben in den {months} Monaten davor "
                      "zugeordnete Schichten gearbeitet")
        n, added, extra = self.areas_from_history, 0, 0
        if n:
            for p in self.person_by_sp5.values():
                more = {a for a in areas if counts[(p["id"], a)] >= n} - group[p["id"]]
                if more:
                    added += len(more)
                    p["areaIds"] = [a for a in areas if a in group[p["id"]] | more]
        for eid, (e, start, end) in self.no_area.items():
            worked = {a for a in areas if n and counts[(f"E{eid}", a)] >= n}
            if not worked:
                self.dropped["ohne Bereich"] += 1
                continue
            self._add_person(e, start, end, worked)
            added += len(worked) if eid in self.person_by_sp5 else 0
            extra += eid in self.person_by_sp5
        if n:
            self.warn(f"Bereiche aus Diensten (mindestens {n}): {added} Bereiche ergänzt, davon für {extra} "
                      "Mitarbeiter ohne Bereich aus Gruppen, die damit übernommen werden")

    def run(self) -> dict:
        self.shifts()
        if not self.shift_by_sp5:
            raise Sp5Error("keine Schicht übernommen; Zuordnung 'shifts' und 'ignoreShifts' prüfen")
        self.people()
        absence_types = self.absence_types()
        self.areas_from_duties()
        self._people_summary()
        if not self.person_by_sp5:
            raise Sp5Error("kein Mitarbeiter übernommen; Zuordnung 'groups' und 'excludeGroups' prüfen")
        restrictions = self.restrictions()
        absences = self.absences()
        holidays, sp5_holiday_dates = self.holidays()
        holiday_dates = {h["date"] for h in holidays}
        demand = self.demand(sp5_holiday_dates, holiday_dates)
        previous = [{"personId": pid, "date": d.isoformat(), "shiftId": s["id"]}
                    for pid, d, s in self.duties(self.first - timedelta(days=PREVIOUS_DAYS),
                                                 self.first - timedelta(days=1))]
        history = self.history(holiday_dates)
        people = sorted(self.person_by_sp5.values(), key=lambda p: int(p["externalId"]))
        log.info("SP5 import %s..%s: %d people, %d shifts, %d demand rules, %d warnings", self.month,
                 self.months[-1], len(people),
                 len(self.shift_by_sp5), len(demand), len(self.warnings))
        return {
            "format": FORMAT, "version": VERSION, "source": "sp5",
            "areas": self.mapping["areas"],
            "shifts": sorted(self.shift_by_sp5.values(), key=lambda s: int(s["id"][1:])),
            "demand": demand, "holidays": holidays, "absenceTypes": absence_types, "people": people,
            "restrictions": restrictions, "history": history,
            # Previous duties only for the first month: Dispofold takes those of a later month from the saved
            # plan of the month before it.
            "periods": [{"month": m, "absences": [a for a in absences if a["date"][:7] == m], "wishes": [],
                         "fixed": [], "previousDuties": previous if m == self.month else []} for m in self.months],
        }


def import_sp5(folder: str | Path, month: str, mapping: dict, to: str | None = None,
               history_months: int = HISTORY_MONTHS, areas_from_history: int | None = None,
               report: list | None = None) -> tuple[dict, list[str]]:
    """Read the SP5 folder (never written) and return the dataset of the months `month`..`to` and the warnings.
    `report` (a list) receives one row (person id, area, duties in the history window, area from groups) per
    person and area worked or held."""
    validate_mapping(mapping)
    months = [month]
    while to and months[-1] < to:
        months.append(add_months(months[-1], 1))
    if (to and to < month) or len(months) > MAX_EXTRA_MONTHS + 1:
        raise Sp5Error(f"Zeitraum {month} bis {to}: Ende muss im selben Monat oder bis zu {MAX_EXTRA_MONTHS} "
                       "Monate danach liegen")
    if not 0 <= history_months <= MAX_HISTORY_MONTHS:
        raise Sp5Error(f"Historie {history_months} Monate: erlaubt sind 0 bis {MAX_HISTORY_MONTHS}")
    if areas_from_history is not None and (areas_from_history < 1 or not history_months):
        raise Sp5Error("Bereiche aus Diensten brauchen eine Mindestzahl ab 1 und eine Historie")
    folder = Path(folder)
    if not folder.is_dir():
        raise Sp5Error(f"SP5-Ordner nicht gefunden: {folder}")
    job = _Import(folder, months, mapping, history_months, areas_from_history)
    dataset = job.run()
    if report is not None:
        report.extend(sorted(job.report_rows, key=lambda r: (int(r[0][1:]), r[1])))
    return dataset, job.warnings
