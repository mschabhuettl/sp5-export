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

from . import corrections as corr
from .dbf import DbfError, read_table
from .demand import coverage_segments, median_up, percentile_up, start_day_windows
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
DEMAND_SOURCES = ("history", "sp5")
REPORT_COLUMNS = 29
MAX_PERCENT = 90  # demand from the history: max = this nearest-rank percentile of the counts
HIDDEN_SHIFTS_MONTHS = 3  # default: a hidden shift with duties in this many months before the plan is taken
MIX_MIN_DUTIES = 10  # default: people with fewer counted duties in the history window get no shift mix
RECENT_MONTHS = 3  # default: an area without duties in a person's last this many active months becomes on request
# Duties in the planning months on shifts in ignoreShifts become absences of this type (only when there are any).
IGNORED_SHIFT_ABSENCE = {"code": "EXT", "name": "Sonstiger Dienst (ignorierte Schicht)", "reducesTarget": True,
                         "chargesAccount": False, "compensatoryRest": False, "color": DEFAULT_ABSENCE_COLOR}


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
    for key in sorted(set(mapping) - {"areas", "shifts", "groups", "ignoreShiftsAsAbsence", *PATTERN_LISTS}):
        errors.append(f"unbekannter Schlüssel '{key}'")
    for key in ("areas", "shifts", "groups"):
        if key not in mapping:
            errors.append(f"Pflichtschlüssel '{key}' fehlt")
    for key in ("areas", "shifts", "groups", *PATTERN_LISTS):
        if key in mapping and not isinstance(mapping[key], list):
            errors.append(f"'{key}' muss eine Liste sein")
    if "ignoreShiftsAsAbsence" in mapping and not isinstance(mapping["ignoreShiftsAsAbsence"], bool):
        errors.append("'ignoreShiftsAsAbsence' muss true oder false sein")
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


def _shares(counts: Counter, keys: list[str]) -> dict[str, float]:
    """Shares of the counts in the order of `keys`, rounded half up to thousandths, without zero shares. The
    largest share (the first of equal ones) takes the rounding difference, so the shares sum to exactly 1."""
    total = sum(counts[k] for k in keys)
    if not total:
        return {}
    parts = {k: (2000 * counts[k] + total) // (2 * total) for k in keys}
    parts = {k: v for k, v in parts.items() if v}
    top = max(parts, key=parts.get)
    parts[top] += 1000 - sum(parts.values())
    return {k: v / 1000 for k, v in parts.items()}


def _mix_text(shares: dict[str, float] | None) -> str:
    return ", ".join(f"{k} {v:.3f}" for k, v in (shares or {}).items())


class _Import:
    def __init__(self, folder: Path, months: list[str], mapping: dict, history_months: int = HISTORY_MONTHS,
                 areas_from_history: int | None = None, shifts_from_history: int | None = None,
                 demand: str = "history", mix_min_duties: int | None = MIX_MIN_DUTIES,
                 hidden_shifts_months: int = HIDDEN_SHIFTS_MONTHS, recent_months: int = RECENT_MONTHS):
        self.folder, self.mapping = folder, mapping
        self.hidden_shifts_months = hidden_shifts_months
        self.recent_months = recent_months
        self.mix_min_duties = mix_min_duties
        self.demand_source = demand
        self.month, self.months = months[0], months
        self.history_months, self.areas_from_history = history_months, areas_from_history
        self.shifts_from_history = shifts_from_history
        self.warnings: list[str] = []
        self.days = [d for m in months for d in month_days(m)]
        self.first, self.last = self.days[0], self.days[-1]
        self.history_start = date.fromisoformat(add_months(self.month, -history_months) + "-01")
        # (check, person, area, shift code, duties, area from groups, restriction[, day type, peak from history,
        # peak from SP5, duties left out, hours, ..., reason, SP5 shift id, CALCBASE, HRSWEEK, CALCBASE hours])
        self.report_rows: list[tuple] = []
        self.shift_counts: Counter = Counter()  # (person id, shift id) -> duties in the history window
        self.area_months: defaultdict = defaultdict(Counter)  # (person id, area) -> month -> duties in the window
        self.last_duty: dict = {}  # (person id, area) -> date of the last duty in the history window
        self.past_areas: dict[str, set[str]] = {}  # person id -> areas no longer worked (areas_no_longer_worked)
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
        self.without_area: dict[int, tuple[str, str, float]] = {}  # SP5 id -> (short, why, hours of a duty)
        # The other shifts that are not taken: hidden, without times, longer than 12 hours (same tuples).
        self.dropped_shifts: dict[int, tuple[str, str, float]] = {}

        def times_of(r: dict) -> Counter:
            times = Counter()
            for k in range(8):
                m = TIME_RANGE.match(_text(r.get(f"STARTEND{k}")))
                if m:
                    times[(_norm_time(m.group(1)), _norm_time(m.group(2)))] += 1
            return times

        def hours_of(times: Counter) -> float:
            if not times:
                return 0.0
            a, b = interval(*times.most_common(1)[0][0])
            return (b - a) / 60

        hidden = {r.get("ID"): r for r in self.table("5SHIFT", required=True) if r.get("HIDE")}
        in_use, use = self._hidden_in_use(hidden)
        for r in self.table("5SHIFT", required=True):
            sp5_id, short = r.get("ID"), _text(r.get("SHORTNAME"))
            if r.get("HIDE") and sp5_id not in in_use:
                self.dropped_shifts[sp5_id] = (short, "ausgeblendet", hours_of(times_of(r)))
                continue
            if _any(ignore, short):
                self.without_area[sp5_id] = (short, "ignoriert", hours_of(times_of(r)))
                continue
            rule = _first(self.mapping["shifts"], short)
            if rule is None or rule["areaId"] not in area_ids:
                self.warn(f"Schicht ohne Bereich ignoriert: SP5-Id {sp5_id}, Kürzel {short!r}")
                self.without_area[sp5_id] = (short, "nicht zugeordnet", hours_of(times_of(r)))
                continue
            times = times_of(r)
            if not times:
                self.warn(f"Schicht ohne Dienstzeit ignoriert: SP5-Id {sp5_id}, Kürzel {short!r}")
                self.dropped_shifts[sp5_id] = (short, "ohne_dienstzeit", 0.0)
                continue
            (start, end), _ = times.most_common(1)[0]
            if len(times) > 1:
                self.warn(f"Schicht {short!r} (SP5-Id {sp5_id}) hat je Wochentag andere Zeiten; "
                          f"verwendet wird {start}-{end}")
            s, e = interval(start, end)
            if e - s > MAX_DUTY_MINUTES:
                self.warn(f"Schicht {short!r} (SP5-Id {sp5_id}) dauert mehr als 12 Stunden ({start}-{end}) und "
                          "wird nicht übernommen")
                self.dropped_shifts[sp5_id] = (short, "zu_lang", (e - s) / 60)
                continue
            category = rule.get("category") or _category(start, end)
            color = r.get("COLORBK")
            self.shift_by_sp5[sp5_id] = {
                "id": f"S{sp5_id}", "areaId": rule["areaId"], "code": short,
                "name": _text(r.get("NAME")) or short, "start": start, "end": end, "category": category,
                "color": bgr_to_hex(color) if isinstance(color, int) and color > 0 else DEFAULT_SHIFT_COLOR,
                "unpopular": rule.get("unpopular", category == "NIGHT"),
            }
        if not hidden:
            return
        for sp5_id, (n, last) in sorted(use.items()):
            short = _text(hidden[sp5_id].get("SHORTNAME"))
            hours = hours_of(times_of(hidden[sp5_id]))
            taken = sp5_id in self.shift_by_sp5
            why = "" if taken else "nicht_in_gebrauch" if sp5_id not in in_use else \
                (self.without_area.get(sp5_id) or self.dropped_shifts[sp5_id])[1].replace(" ", "_")
            self.report_rows.append(("ausgeblendet", "", "", short, n, *[""] * 6, f"{n * hours:.1f}", *[""] * 6,
                                     why, sp5_id, *[""] * 4, "uebernommen" if taken else "verworfen", *[""] * 3,
                                     last.isoformat()))
        taken = sum(1 for sid in hidden if sid in self.shift_by_sp5)
        self.warn(f"Ausgeblendete Schichten: {taken} übernommen, weil in Gebrauch (Dienste in den Planungsmonaten "
                  f"oder den {self.hidden_shifts_months} Monaten davor), {len(hidden) - taken} nicht übernommen")

    def _hidden_in_use(self, hidden: dict) -> tuple[set, dict]:
        """The hidden shifts in use (with a duty of anyone in the planning months or the `hidden_shifts_months`
        months before them, never with 0), and per hidden shift with duties from the start of the history window
        to the end of the planning months: (duties, date of the last one). A duty is the first row of a person's
        day in the order of `_rows`, so a row replaced by a deviation does not count."""
        if not hidden:
            return set(), {}
        recent = max(self.history_start, date.fromisoformat(add_months(self.month, -self.hidden_shifts_months)
                                                            + "-01"))
        in_use, use, days = set(), {}, set()
        for r in self._rows(self.history_start, self.last)[1]:
            sid, d = r.get("SHIFTID"), _date(r.get("DATE"))
            if d is None or not self.history_start <= d <= self.last or (r.get("EMPLOYEEID"), d) in days:
                continue
            days.add((r.get("EMPLOYEEID"), d))  # the first row of a person's day is the duty (as in duties())
            if sid not in hidden:
                continue
            n, last = use.get(sid, (0, d))
            use[sid] = (n + 1, max(last, d))
            if self.hidden_shifts_months and d >= recent:
                in_use.add(sid)
        return in_use, use

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
        # Their duties stay out of the demand from the history as well.
        self.excluded_people = {f"E{eid}" for eid, values in member.items() if "exclude" in values}
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
            # The value written is the one from CALCBASE (none when it is 0: the person is not exported).
            self.report_rows.append(("wochenstunden", f"E{eid}", *[""] * 11, hours if hours > 0 else "", *[""] * 6,
                                     base, float(e.get("HRSWEEK") or 0), hours))
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

    def ignored_duty_absences(self, absences: list[dict], types: list[dict]) -> list[dict]:
        """Absences of type IGNORED_SHIFT_ABSENCE for the duties in the planning months on shifts in ignoreShifts,
        read like the history: not on days with a full-day absence or a mapped duty, nor on days that already
        have an absence or are outside the employment. One warning per month with such days."""
        if not self.mapping.get("ignoreShiftsAsAbsence", True):
            return []
        lost: dict = {}
        self.duties(self.first, self.last, quiet=True, lost=lost)
        taken = {(a["personId"], a["date"]) for a in absences}
        code, result, per_month = IGNORED_SHIFT_ABSENCE["code"], [], defaultdict(lambda: [0, 0.0])
        for (pid, d), sp5_id in sorted(lost.items()):
            _, why, hours = self.without_area[sp5_id]
            start, end = self.employment[pid]
            if why != "ignoriert" or (pid, d.isoformat()) in taken or (start and d < start) or (end and d > end):
                continue
            result.append({"personId": pid, "date": d.isoformat(), "typeCode": code})
            per_month[d.isoformat()[:7]][0] += 1
            per_month[d.isoformat()[:7]][1] += hours
        if result and any(t["code"] == code for t in types):
            raise Sp5Error(f"Abwesenheitsart {code!r} gibt es schon in SP5 (5LEAVT); sie wird für Dienste auf "
                           "ignorierten Schichten gebraucht. In der Zuordnung 'ignoreAbsences' um "
                           f"{code!r} ergänzen oder 'ignoreShiftsAsAbsence' auf false setzen")
        for m, (n, hours) in sorted(per_month.items()):
            self.warn(f"Dienste auf ignorierten Schichten im {m}: {n} Tage, {hours:.1f} Stunden als Abwesenheit "
                      f"{code!r} übernommen")
        return result

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

    def demand(self, sp5_holiday_dates: set[str], holiday_dates: set[str], generic_only: bool = False) -> list[dict]:
        """5SHDEM (per group, shift and day index) and 5SPDEM (per date) summed per area into windows.
        `generic_only` returns the weekday and HOL rules only (for the comparison with the history)."""
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
        if generic_only:
            return rules
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

    def demand_from_history(self, holiday_dates: set[str], sp5_rules: list[dict]) -> list[dict]:
        """Demand from the real staffing in the history window: per area and day type (MON..SUN, HOL from the
        holidays of the history) the median over all days of that type of the people on duty at once, in segments
        between the boundaries of the area's worked shifts. A duty counts on the day it starts. The duties of
        everyone count, also of people not exported (left, not yet started, hidden, without area or weekly hours):
        others had to take them over. Only people in excludeGroups do not count; their duties are reported (counts
        only) next to the peak of the SP5 demand, and so are the counted ones of people not exported."""
        end = self.first - timedelta(days=1)
        everyone = {e.get("ID"): {"id": f"E{e.get('ID')}"} for e in self.table("5EMPL", required=True)}
        exported = {p["id"] for p in self.person_by_sp5.values()}
        by_day, counted, left_out, outside = defaultdict(list), Counter(), Counter(), Counter()
        self.rejected: list = []  # rows on shifts not taken, for dropped_report
        self.left_out_shifts: Counter = Counter()  # shift id -> duties of people not exported
        days = [self.history_start + timedelta(days=k) for k in range((end - self.history_start).days + 1)]

        def day_type(d: date) -> str:
            return "HOL" if d.isoformat() in holiday_dates else WEEKDAY_CODES[d.weekday()]

        for pid, d, s in self.duties(self.history_start, end, everyone, quiet=True, rejected=self.rejected):
            if pid not in exported:
                self.left_out_shifts[s["id"]] += 1
                if pid in self.excluded_people:
                    left_out[(s["areaId"], day_type(d))] += 1
                    continue
                outside[(s["areaId"], day_type(d))] += 1
            counted[(s["areaId"], day_type(d))] += 1
            by_day[(s["areaId"], d)].append(interval(s["start"], s["end"]))
        rules, months = [], self.history_months
        codes = [*WEEKDAY_CODES, "HOL"]
        for area in (a["id"] for a in self.mapping["areas"]):
            points = sorted({x for (a, _), spans in by_day.items() if a == area for span in spans for x in span})
            profiles = {}
            for code in codes:
                samples = [by_day.get((area, d), []) for d in days if day_type(d) == code]
                profile = []
                for a, b in pairwise(points):
                    counts = [sum(1 for s, e in spans if s <= a and b <= e) for spans in samples] or [0]
                    hi = percentile_up(counts, MAX_PERCENT)
                    if hi:
                        profile.append((a, b, median_up(counts), hi))
                profiles[code] = profile
            groups = defaultdict(list)
            for code in codes:
                key = (code == "HOL", tuple(start_day_windows(profiles[code])))
                groups[key].append(code)
            for (_, w), day_codes in groups.items():
                rules += self._rules(area, day_codes, None, w)
            if any(d.isoformat() in holiday_dates for d in days) and not profiles["HOL"] \
                    and any(profiles[c] for c in WEEKDAY_CODES):
                # Without HOL rules Dispofold takes the weekday rules on a holiday; a rule with 0 keeps it free.
                rules += self._rules(area, ["HOL"], None, [(points[0], min(points[-1], points[0] + DAY), 0, None)])
            peaks = []
            for i, code in enumerate(codes):
                # As for the SP5 demand: a calendar day with the end of the nights of the day before.
                before = profiles["HOL" if code == "HOL" else codes[(i - 1) % 7]]
                lows, highs = ([(a, b, x[i]) for a, b, *x in profiles[code]] for i in (0, 1))
                lows_before, highs_before = ([(a, b, x[i]) for a, b, *x in before] for i in (0, 1))
                low = max((c for _, _, c in coverage_segments(lows, lows_before)), default=0)
                high = max((c for _, _, c in coverage_segments(highs, highs_before)), default=0)
                sp5 = max((r["min"] for r in sp5_rules if r["areaId"] == area and not r["date"] and code in r["days"]),
                          default=0)
                peaks.append(f"{code} {low}-{high}/{sp5}")
                self.report_rows.append(("bedarf", "", area, "", counted[(area, code)], "", "", code, low, sp5,
                                         left_out[(area, code)], "", high, *[""] * 14, outside[(area, code)]))
            done, skipped = sum(counted[(area, c)] for c in codes), sum(left_out[(area, c)] for c in codes)
            others = sum(outside[(area, c)] for c in codes)
            self.warn(f"Bedarf im Bereich {area}, Spitze je Tagestyp aus der Historie/aus SP5: {', '.join(peaks)}; "
                      f"{done} Dienste in den {months} Monaten davor gezählt, davon {others} von Mitarbeitern, die "
                      f"nicht übernommen werden; {skipped} nicht gezählt (ausgeschlossene Gruppe)")
        if self.demand_source == "history":
            self.warn(f"Bedarf aus der Historie: Median je Bereich und Tagestyp über die {months} Monate davor")
        else:
            self.warn("Bedarf aus SP5 (5SHDEM, 5SPDEM); die Historie nur zum Vergleich")
        return rules

    def unmapped_report(self) -> None:
        """Duties of exported people in the history window on SP5 shifts without an area (not mapped, or in
        ignoreShifts): counts and hours per shift, most hours first, only numbers and shift codes."""
        lost: dict = {}
        self.duties(self.history_start, self.first - timedelta(days=1), quiet=True, lost=lost)
        counts = Counter(lost.values())
        rows = sorted(((code, why, n, n * hours) for sp5_id, (code, why, hours) in self.without_area.items()
                       if (n := counts[sp5_id])), key=lambda r: (-r[3], r[0]))
        months = self.history_months
        for code, why, n, hours in rows:
            self.warn(f"Schicht {code!r} {why}: {n} Dienste, {hours:.1f} Stunden in den {months} Monaten davor")
            self.report_rows.append(("ignoriert" if why == "ignoriert" else "nicht_zugeordnet", "", "", code, n,
                                     "", "", "", "", "", "", f"{hours:.1f}"))
        self.warn(f"Schichten ohne Bereich: {len(rows)} Schichten, {sum(r[2] for r in rows)} Dienste, "
                  f"{sum(r[3] for r in rows):.1f} Stunden in den {months} Monaten davor")

    def dropped_report(self) -> None:
        """The duties of the history window that the dataset leaves out because of their shift or their person,
        from the reading of demand_from_history: one line per reason (counts and hours) and report rows per shift.
        The rows of exported people on shifts not taken are exactly the rows of the warning "N Dienste ... mit
        nicht zugeordneter Schicht ignoriert" of the history window; not mapped and ignored are exactly the
        duties of unmapped_report, hidden and long or timeless shifts are counted the same way, "sonstige" are the
        other rows (no shift of 5SHIFT, a day of full-day absence, a second row of the day). People not exported:
        their duties on shifts taken (counted in the demand lines unless in excludeGroups), and their rows on other
        shifts."""
        window = f"{self.history_start}..{self.first - timedelta(days=1)}"
        exported = {p["id"] for p in self.person_by_sp5.values()}
        counts = Counter()  # (reason, SP5 shift id) -> duties
        for pid, sid, what in self.rejected:
            if pid not in exported:
                counts[("mitarbeiter_und_schicht_nicht_uebernommen", sid)] += 1
            elif what != "gezaehlt":
                counts[(what, sid)] += 1
            else:
                counts[((self.without_area.get(sid) or self.dropped_shifts[sid])[1].replace(" ", "_"), sid)] += 1
        by_id = {s["id"]: (sp5_id, s) for sp5_id, s in self.shift_by_sp5.items()}
        for shift_id, n in self.left_out_shifts.items():
            sp5_id, _ = by_id[shift_id]
            counts[("mitarbeiter_nicht_uebernommen", sp5_id)] += n

        def code_hours(sid) -> tuple[str, float]:
            if sid in self.shift_by_sp5:
                a, b = interval(self.shift_by_sp5[sid]["start"], self.shift_by_sp5[sid]["end"])
                return self.shift_by_sp5[sid]["code"], (b - a) / 60
            short, _, hours = self.without_area.get(sid) or self.dropped_shifts.get(sid) or ("", "", 0.0)
            return short, hours

        totals = defaultdict(lambda: [0, 0.0])
        for (why, sid), n in sorted(counts.items(), key=lambda x: (x[0][0], str(x[0][1]))):
            code, hours = code_hours(sid)
            totals[why][0] += n
            totals[why][1] += n * hours
            self.report_rows.append(("verworfen", "", "", code, n, *[""] * 6, f"{n * hours:.1f}", *[""] * 6, why,
                                     "" if sid is None else str(sid)))

        def total(*whys) -> str:
            n, hours = (sum(totals[w][i] for w in whys) for i in (0, 1))
            return f"{n} Dienste, {hours:.1f} Stunden"

        head = f"Verworfene Dienste {window}"
        self.warn(f"{head}, Schicht nicht zugeordnet: {total('nicht_zugeordnet')}")
        self.warn(f"{head}, Schicht ignoriert (ignoreShifts): {total('ignoriert')}")
        self.warn(f"{head}, Schicht ausgeblendet (HIDE): {total('ausgeblendet')}")
        self.warn(f"{head}, Schicht länger als 12 Stunden oder ohne Dienstzeit: {total('zu_lang', 'ohne_dienstzeit')}")
        other = ("unbekannte_schicht", "abwesenheit", "anderer_eintrag")
        self.warn(f"{head}, sonstige: {total(*other)} ({totals[other[0]][0]} ohne Schicht in 5SHIFT, "
                  f"{totals[other[1]][0]} an Tagen mit ganztägiger Abwesenheit, {totals[other[2]][0]} neben einem "
                  "anderen Eintrag desselben Tags)")
        shift_rows = sum(n for (why, _), n in counts.items() if not why.startswith("mitarbeiter_"))
        self.warn(f"{head}: zusammen {shift_rows} Dienste übernommener Mitarbeiter auf Schichten, die nicht "
                  "übernommen werden (wie die Warnung '... Dienste "
                  f"{window} mit nicht zugeordneter Schicht ignoriert')")
        self.warn(f"{head}, Mitarbeiter nicht übernommen: {total('mitarbeiter_nicht_uebernommen')} auf übernommenen "
                  "Schichten (im Bedarf gezählt außer ausgeschlossene Gruppe), dazu "
                  f"{total('mitarbeiter_und_schicht_nicht_uebernommen')} auf Schichten, die nicht übernommen werden")

    # ── duties ───────────────────────────────────────────────

    def duties(self, start: date, end: date, people: dict[int, dict] | None = None,
               quiet: bool = False, lost: dict | None = None,
               rejected: list | None = None) -> list[tuple[str, date, dict]]:
        """(person id, date, shift) from 5MASHI and 5SPSHI in [start, end], in-scope only. `people` replaces the
        exported people (SP5 id -> person); `quiet` leaves out the warnings (a second pass over the same days);
        `lost` receives (person id, date) -> SP5 shift id of the duties left out because their shift is in
        `without_area` (not those of absent days or of days with a duty that is taken). `rejected` receives
        (person id, SP5 shift id, what) for every row the warning about duties with a shift not mapped counts:
        what is "unbekannte_schicht" (no shift of 5SHIFT), "abwesenheit" (a day of full-day absence),
        "gezaehlt" (one row per day without a duty taken: for `without_area` exactly the duties of `lost`, else
        the first row on a shift of `dropped_shifts`) or "anderer_eintrag" (any other row)."""
        result, unknown, loose, first, rows_out = {}, 0, {}, {}, []
        people = self.person_by_sp5 if people is None else people
        warn = (lambda text: None) if quiet else self.warn
        special, rows = self._rows(start, end)
        with_shift = [r for r in special if r.get("SHIFTID")]
        replaced = {id(r) for r in with_shift}
        other_type = sum(1 for r in with_shift if r.get("TYPE"))
        if other_type:
            warn(f"{other_type} Sonderdienste {start}..{end} (5SPSHI) mit TYPE ungleich 0 als Dienst übernommen")
        without_shift = len(special) - len(with_shift)
        if without_shift:
            warn(f"{without_shift} Sonderdienste {start}..{end} (5SPSHI) ohne Schicht nicht übernommen")
        for r in rows:
            d = _date(r.get("DATE"))
            person = people.get(r.get("EMPLOYEEID"))
            if d is None or person is None or not start <= d <= end:
                continue
            shift = self.shift_by_sp5.get(r.get("SHIFTID"))
            if shift is None:
                unknown += 1
                sid, key = r.get("SHIFTID"), (person["id"], d)
                rows_out.append((key, sid, id(r)))
                # Not when an earlier row (a deviation) already took the day: that duty was replaced.
                if sid in self.without_area and key not in result:
                    loose.setdefault(key, sid)
                if (sid in self.without_area or sid in self.dropped_shifts) and key not in result:
                    first.setdefault((key, sid in self.without_area), id(r))
                if id(r) in replaced:  # the deviation still replaces the planned duty of the day
                    result.setdefault((person["id"], d), None)
                continue
            result.setdefault((person["id"], d), shift)
        result = {k: s for k, s in result.items() if s is not None}
        if unknown:
            warn(f"{unknown} Dienste {start}..{end} mit nicht zugeordneter Schicht ignoriert")
        # A full-day absence (5ABSEN) wins over a duty of the same day: SP5 shows both as a conflict, and the
        # dataset has no absences outside the planning month to show it there.
        absent_days = self._absent(start, end, people=people)
        if lost is not None:
            lost.update({k: v for k, v in loose.items() if k not in result and k not in absent_days})
        if rejected is not None:
            firsts = {rid for (key, own), rid in first.items() if own or (key, True) not in first}
            for key, sid, rid in rows_out:
                what = "unbekannte_schicht" if sid not in self.without_area and sid not in self.dropped_shifts \
                    else "abwesenheit" if key in absent_days \
                    else "gezaehlt" if rid in firsts and key not in result else "anderer_eintrag"
                rejected.append((key[0], sid, what))
        absent = absent_days & result.keys()
        if absent:
            warn(f"{len(absent)} Dienste {start}..{end} an Tagen mit ganztägiger Abwesenheit nicht übernommen")
        return [(pid, d, s) for (pid, d), s in sorted(result.items(), key=lambda x: (x[0][0], x[0][1]))
                if (pid, d) not in absent]

    def _rows(self, start: date, end: date) -> tuple[list[dict], list[dict]]:
        """The 5SPSHI rows in [start, end], and the duty rows in their order of precedence: 5SPSHI rows with a
        shift, the actual plan 5MASHI (TYPE 1 is the target plan, left out), then the cycle duties of the range.
        5SPSHI rows with a shift replace the duty of the day (sp5lib: _replaced_days, get_schedule), whatever
        their TYPE; sp5lib counts them as work time, only its staffing count leaves TYPE != 0 out, and the
        meaning of TYPE is not documented. Rows without a shift carry their own times and have no shift here."""
        special = [r for r in self.table("5SPSHI") if self._in_range(r, start, end)]
        rows = [r for r in special if r.get("SHIFTID")]
        rows += [r for r in self.table("5MASHI") if r.get("TYPE") != 1]
        rows += sp5calc.expand_cycle_assignments(
            self.table("5CYASS", optional=True), cycles=self.table("5CYCLE", optional=True),
            cycle_entries=self.table("5CYENT", optional=True),
            cycle_exceptions=self.table("5CYEXC", optional=True), von=start, bis=end)
        return special, rows

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
            for pid, d, s in self.duties(self.history_start, self.first - timedelta(days=1), people, quiet=True):
                counts[(pid, s["areaId"])] += 1
                self.shift_counts[(pid, s["id"])] += 1
                self.area_months[(pid, s["areaId"])][d.isoformat()[:7]] += 1
                self.last_duty[(pid, s["areaId"])] = d  # the duties come in date order per person
        areas = [a["id"] for a in self.mapping["areas"]]
        group = {p["id"]: set(p["areaIds"]) for p in people.values()}
        for p in people.values():
            for a in areas:
                if counts[(p["id"], a)] or a in group[p["id"]]:
                    self.report_rows.append(("bereich", p["id"], a, "", counts[(p["id"], a)],
                                             "ja" if a in group[p["id"]] else "nein", ""))
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

    # ── shifts from history ──────────────────────────────────

    def shifts_from_duties(self) -> list[dict]:
        """Compare the shifts of each person's areas with the duties in the history window (counts only) and, with
        `shifts_from_history` N, return an ON_REQUEST restriction for every shift with fewer than N duties. A pair
        with any 5RESTR row keeps the SP5 rows only; people with too little history are left out."""
        months, n = self.history_months, self.shifts_from_history
        if not months:
            return []
        least = n or 1
        sp5 = {(r.get("EMPLOYEEID"), r.get("SHIFTID")) for r in self.table("5RESTR")}
        recent = date.fromisoformat(add_months(self.month, -3) + "-01")
        by_area = defaultdict(list)
        for sp5_id, s in self.shift_by_sp5.items():
            by_area[s["areaId"]].append((sp5_id, s))
        people, pairs, kept, skipped, result = Counter(), Counter(), 0, 0, []
        for eid, p in self.person_by_sp5.items():
            shifts = [x for a in p["areaIds"] for x in by_area[a]]
            count = {s["id"]: self.shift_counts[(p["id"], s["id"])] for _, s in shifts}
            entry = self.employment[p["id"]][0]
            short = sum(count.values()) < least or (entry is not None and entry > recent)
            skipped += short
            few = set()
            for sp5_id, s in shifts:
                mark = ""
                if (eid, sp5_id) in sp5:
                    mark, kept = "sp5", kept + 1
                elif short:
                    mark = "zu_kurze_historie"
                elif count[s["id"]] < least:
                    pairs[s["areaId"]] += 1
                    few.add(s["areaId"])
                    if n:
                        mark = "auf_anfrage"
                        result.append({"personId": p["id"], "shiftId": s["id"], "weekday": 0, "kind": "ON_REQUEST"})
                self.report_rows.append(("schicht", p["id"], s["areaId"], s["code"], count[s["id"]], "", mark))
            people.update(few)
        fewer = f"weniger als {n} Diensten" if n else "keinem Dienst"
        for a in (a["id"] for a in self.mapping["areas"]):
            self.warn(f"Schichten im Bereich {a}: {people[a]} Mitarbeiter mit {fewer} in mindestens einer Schicht "
                      f"des Bereichs in den {months} Monaten davor, {pairs[a]} Paare aus Mitarbeiter und Schicht")
        self.warn(f"Schichten aus Diensten: {skipped} Mitarbeiter mit zu kurzer Historie (weniger als {least} "
                  f"Dienste in ihren Bereichen oder Eintritt nach {recent}) nicht bewertet, {kept} Paare aus "
                  "Mitarbeiter und Schicht mit Einschränkung aus SP5 belassen")
        if n:
            self.warn(f"Schichten aus Diensten (mindestens {n}): {len(result)} Einschränkungen 'nur auf Anfrage' "
                      "ergänzt")
        return result

    def areas_no_longer_worked(self, restrictions: list[dict]) -> list[dict]:
        """With `recent_months` N: for each exported person, every area of their areaIds with duties in the history
        window but none in the person's last N active months (months with a counted duty on any mapped shift, so a
        long absence is skipped) becomes on request: a weekday-0 ON_REQUEST restriction for each of its shifts,
        unless `restrictions` already has a weekday-0 one for the pair (ON_REQUEST or the stricter NEVER). People
        with fewer than N active months or an entry in the last N months before the plan are not judged."""
        n = self.recent_months
        if not n or not self.history_months:
            return []
        start = date.fromisoformat(add_months(self.month, -n) + "-01")
        active = defaultdict(set)
        for (pid, _), months in self.area_months.items():
            active[pid].update(months)
        whole_week = {(r["personId"], r["shiftId"]) for r in restrictions if r["weekday"] == 0}
        by_area = defaultdict(list)
        for s in self.shift_by_sp5.values():
            by_area[s["areaId"]].append(s)
        result, skipped = [], 0
        for p in self.person_by_sp5.values():
            recent = sorted(active[p["id"]])[-n:]
            entry = self.employment[p["id"]][0]
            if len(recent) < n or (entry is not None and entry >= start):
                skipped += 1
                continue
            for a in p["areaIds"]:
                months = self.area_months.get((p["id"], a))
                if not months or any(months[m] for m in recent):
                    continue
                self.past_areas.setdefault(p["id"], set()).add(a)
                result += [{"personId": p["id"], "shiftId": s["id"], "weekday": 0, "kind": "ON_REQUEST"}
                           for s in by_area[a] if (p["id"], s["id"]) not in whole_week]
                self.report_rows.append(("nicht_mehr", p["id"], a, "", sum(months.values()), "", "auf_anfrage",
                                         *[""] * 21, self.last_duty[(p["id"], a)].isoformat()))
        pairs = sum(len(areas) for areas in self.past_areas.values())
        self.warn(f"Bereiche nicht mehr gearbeitet (kein Dienst in den letzten {n} Monaten mit Diensten): "
                  f"{len(self.past_areas)} Mitarbeiter, {pairs} Paare aus Mitarbeiter und Bereich, {len(result)} "
                  f"Einschränkungen 'nur auf Anfrage' ergänzt; {skipped} Mitarbeiter mit weniger als {n} Monaten mit "
                  f"Diensten oder Eintritt ab {start} nicht bewertet")
        return result

    # ── shift mix ────────────────────────────────────────────

    def shift_mix(self) -> None:
        """The usual shares of each exported person's counted duties in the history window (`shift_counts`, the
        reading of the history) per shift category (the category of the dataset's shift) and per area. Only
        for people with at least `mix_min_duties` counted duties. The category shares take all counted duties,
        the area shares only those in the person's areaIds (Dispofold accepts no other area); both leave out the
        duties in the person's areas no longer worked (`past_areas`)."""
        by_id = {s["id"]: s for s in self.shift_by_sp5.values()}
        self.duty_totals: Counter = Counter()
        categories, areas = defaultdict(Counter), defaultdict(Counter)
        for (pid, sid), n in self.shift_counts.items():
            self.duty_totals[pid] += n
            if by_id[sid]["areaId"] in self.past_areas.get(pid, ()):
                continue
            categories[pid][by_id[sid]["category"]] += n
            areas[pid][by_id[sid]["areaId"]] += n
        n = self.mix_min_duties
        if n is None:
            return
        given = 0
        for p in self.person_by_sp5.values():
            if self.duty_totals[p["id"]] >= n:
                p["shiftMix"] = {"categories": _shares(categories[p["id"]], list(CATEGORIES)),
                                 "areas": _shares(areas[p["id"]], p["areaIds"])}
                given += 1
        self.warn(f"Schichtmix aus der Historie: {given} Mitarbeiter mit Mix, {len(self.person_by_sp5) - given} "
                  f"mit weniger als {n} Diensten in den {self.history_months} Monaten davor ohne Mix")

    def person_report(self, restrictions: list[dict]) -> None:
        """One report row per exported person: weekly hours, areas, shift mix, counted duties in the history
        window, on-request restrictions; for the review of everyone in a spreadsheet."""
        on_request = Counter(r["personId"] for r in restrictions if r["kind"] == "ON_REQUEST")
        for p in self.person_by_sp5.values():
            mix = p.get("shiftMix") or {}
            self.report_rows.append(("person", p["id"], "", "", self.duty_totals[p["id"]], *[""] * 8,
                                     p["weeklyHours"], " ".join(p["areaIds"]), _mix_text(mix.get("categories")),
                                     _mix_text(mix.get("areas")), on_request[p["id"]]))

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
        restrictions = self.restrictions() + self.shifts_from_duties()
        restrictions += self.areas_no_longer_worked(restrictions)
        restrictions.sort(key=lambda r: (r["personId"], r["shiftId"], r["weekday"]))  # as restrictions() sorts
        self.shift_mix()
        self.person_report(restrictions)
        absences = self.absences()
        ignored = self.ignored_duty_absences(absences, absence_types)
        if ignored:
            absence_types.append(dict(IGNORED_SHIFT_ABSENCE))
            absences = sorted(absences + ignored, key=lambda x: (x["personId"], x["date"]))
        holidays, sp5_holiday_dates = self.holidays()
        holiday_dates = {h["date"] for h in holidays}
        demand = self.demand(sp5_holiday_dates, holiday_dates, generic_only=self.demand_source == "history")
        if self.history_months:
            from_history = self.demand_from_history(holiday_dates, demand)
            if self.demand_source == "history":
                demand = from_history
            self.unmapped_report()
            self.dropped_report()
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
               report: list | None = None, shifts_from_history: int | None = None,
               demand: str = "history", mix_min_duties: int | None = MIX_MIN_DUTIES,
               corrections: dict | None = None,
               hidden_shifts_months: int = HIDDEN_SHIFTS_MONTHS,
               recent_months: int = RECENT_MONTHS) -> tuple[dict, list[str]]:
    """Read the SP5 folder (never written) and return the dataset of the months `month`..`to` and the warnings.
    `report` (a list) receives the rows (check, person id, area, shift code, duties in the history window, area
    from groups, restriction, day type, peak demand from the history, peak demand from SP5, duties left out,
    hours): check "bereich" per person and area worked or held, check "schicht" per person and shift of their
    areas, check "bedarf" per area and day type (no person; duties counted and left out because the person is not
    exported), checks "nicht_zugeordnet" and "ignoriert" per shift without an area (no person; duties and hours),
    check "person" per exported person (duties in the history window and the appended columns weekly hours, areas,
    shift mix per category and per area, on-request restrictions), check "verworfen" per reason and shift of the
    dropped duties of the history window (shift code, duties, hours, reason, SP5 shift id; see dropped_report),
    check "wochenstunden" per person whose HRSWEEK differs from the CALCBASE hours (weekly hours written, empty
    when not exported; CALCBASE, HRSWEEK, CALCBASE hours), check "nicht_mehr" per person and area no longer worked
    (duties in the area in the history window, restriction "auf_anfrage", date of the last duty in the area).
    `demand` "history" takes the demand from the staffing in the history window, "sp5" from the SP5 demand.
    `recent_months` N makes the areas a person no longer worked in their last N active months on request (see
    areas_no_longer_worked); 0 switches it off.
    `mix_min_duties` N gives everyone with at least N counted duties in the history window a shift mix; None
    writes none.
    `corrections` (a corrections file as corrections.load returns it) is applied last; check "korrektur"
    per corrected field (person, reason; field, status, exported value, corrected value). The dataset lists the
    applied entries in its section "appliedCorrections" (empty without a file)."""
    validate_mapping(mapping)
    months = [month]
    while to and months[-1] < to:
        months.append(add_months(months[-1], 1))
    if (to and to < month) or len(months) > MAX_EXTRA_MONTHS + 1:
        raise Sp5Error(f"Zeitraum {month} bis {to}: Ende muss im selben Monat oder bis zu {MAX_EXTRA_MONTHS} "
                       "Monate danach liegen")
    if not 0 <= history_months <= MAX_HISTORY_MONTHS:
        raise Sp5Error(f"Historie {history_months} Monate: erlaubt sind 0 bis {MAX_HISTORY_MONTHS}")
    for option in (areas_from_history, shifts_from_history):
        if option is not None and (option < 1 or not history_months):
            raise Sp5Error("Bereiche und Schichten aus Diensten brauchen eine Mindestzahl ab 1 und eine Historie")
    if hidden_shifts_months < 0:
        raise Sp5Error("Ausgeblendete Schichten: Monate müssen 0 oder mehr sein")
    if recent_months < 0:
        raise Sp5Error("Bereiche nicht mehr gearbeitet: Monate müssen 0 oder mehr sein")
    if mix_min_duties is not None and mix_min_duties < 1:
        raise Sp5Error("Schichtmix: Mindestzahl der Dienste muss mindestens 1 sein")
    if demand not in DEMAND_SOURCES:
        raise Sp5Error(f"Bedarf {demand!r}: erlaubt sind {' oder '.join(DEMAND_SOURCES)}")
    if demand == "history" and not history_months:
        raise Sp5Error("Bedarf aus der Historie braucht eine Historie (--history-months ab 1); ohne Historie "
                       "--demand sp5 angeben")
    folder = Path(folder)
    if not folder.is_dir():
        raise Sp5Error(f"SP5-Ordner nicht gefunden: {folder}")
    job = _Import(folder, months, mapping, history_months, areas_from_history, shifts_from_history, demand,
                  mix_min_duties, hidden_shifts_months, recent_months)
    dataset = job.run()
    rows = []
    if corrections is not None:
        rows, warnings, (people, applied, skipped), summary = corr.apply(dataset, corrections)
        job.report_rows += [("korrektur", pid, *[""] * 16, why, "", "", "", "", field, status, old, new)
                            for pid, field, status, why, old, new in rows]
        job.warnings += warnings
        job.warn(f"Korrekturen: {people} Mitarbeiter korrigiert, {applied} Felder angewendet, {skipped} Felder "
                 "übersprungen")
        if summary:
            job.warn(summary)
    dataset["appliedCorrections"] = corr.section(corrections, rows)
    if report is not None:
        rows = [r + ("",) * (REPORT_COLUMNS - len(r)) for r in job.report_rows]
        report.extend(sorted(rows, key=lambda r: (r[0], int(r[1][1:]) if r[1] else 0, r[2], r[3])))
    return dataset, job.warnings
