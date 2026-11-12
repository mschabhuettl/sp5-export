"""The import validator of Dispofold, ported rule by rule (see tests/dispofold/SOURCE.md for the commit).

The JSON schema (tests/dispofold/dataset.schema.json) covers the shape of the document; this module covers
what the schema cannot express: references between entries, unique ids and entries, dates inside the planning
month, shift lengths against maxDutyHours, the reserved shift id. It returns Dispofold's error codes, so it
can be checked against the expected codes of the shared reject corpus.
"""

import json
import re
from datetime import date
from pathlib import Path

from jsonschema import Draft202012Validator

FORMAT = "dispofold/dataset"
VERSION = 2
MAX_DUTY_HOURS = 12  # Dispofold's default rule
COMP_REST_ID = "ER"
DAY_CODES = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN", "HOL"}
MONTH = re.compile(r"\d{4}-\d{2}")
DISPOFOLD = Path(__file__).resolve().parent / "dispofold"
SCHEMA = Draft202012Validator(json.loads((DISPOFOLD / "dataset.schema.json").read_text(encoding="utf-8")))


def _list(items):
    return items if items is not None else []


def _date(value) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _month(value) -> tuple[int, int] | None:
    if not isinstance(value, str) or not MONTH.match(value) or len(value) != 7:
        return None
    y, m = int(value[:4]), int(value[5:])
    return (y, m) if 1 <= m <= 12 else None


def _minutes(t: str) -> int:
    h, m = t.split(":")[:2]
    return int(h) * 60 + int(m)


class _Validator:
    def __init__(self):
        self.errors: list[tuple[str, dict]] = []
        self.warnings: list[tuple[str, dict]] = []
        self.person_ids: set = set()
        self.shift_ids: set = set()
        self.type_codes: set = set()

    def error(self, code, **params):
        self.errors.append((code, {k: v for k, v in params.items() if v is not None}))

    def warning(self, code, **params):
        self.warnings.append((code, {k: v for k, v in params.items() if v is not None}))

    def missing(self, kind, field, index=None, month=None):
        self.error("import.missingField", kind=kind, field=field, index=index, month=month)

    def present(self, value, kind, field, index, month=None) -> bool:
        if value is None or isinstance(value, str) and not value.strip():
            self.missing(kind, field, index, month)
            return False
        return True

    def each(self, items, kind, month, check):
        for i, item in enumerate(_list(items)):
            if item is None:
                self.missing(kind, None, i, month)
            else:
                check(item, i)

    def unique(self, items, key, kind) -> set:
        seen = set()

        def check(item, i):
            k = item.get(key)
            if self.present(k, kind, key, i):
                if k in seen:
                    self.error("import.duplicateId", kind=kind, id=k)
                seen.add(k)

        self.each(items, kind, None, check)
        return seen

    def person(self, pid, kind, index, day, month) -> bool:
        if not self.present(pid, kind, "personId", index, month):
            return False
        if pid in self.person_ids:
            return True
        self.error("import.unknownPerson", kind=kind, personId=pid, date=day, month=month)
        return False

    def shift(self, sid, kind, pid, day):
        if sid is not None and sid not in self.shift_ids:
            self.error("import.unknownShift", kind=kind, personId=pid, shiftId=sid, date=day)

    def in_month(self, day, month, kind, pid) -> bool:
        d = _date(day)
        if month is None or d is None or (d.year, d.month) == _month(month):
            return True
        self.error("import.dateOutsideMonth", kind=kind, personId=pid, date=day, month=month)
        return False

    # ── master data ──────────────────────────────────────────

    def master(self, d):
        for key in ("areas", "shifts", "demand", "holidays", "absenceTypes", "people", "restrictions", "history",
                    "periods"):
            if d.get(key) is None:
                self.missing("dataset", key)
        if d.get("people") == []:
            self.error("import.noPeople")
        if d.get("shifts") == []:
            self.error("import.noShifts")
        area_ids = self.unique(d.get("areas"), "id", "area")
        self.shift_ids = self.unique(d.get("shifts"), "id", "shift")
        self.type_codes = self.unique(d.get("absenceTypes"), "code", "absenceType")
        self.person_ids = self.unique(d.get("people"), "id", "person")

        for i, s in enumerate(_list(d.get("shifts"))):
            if s is None:
                continue
            if self.present(s.get("areaId"), "shift", "areaId", i) and s["areaId"] not in area_ids:
                self.error("import.unknownArea", kind="shift", shiftId=s.get("id"), areaId=s["areaId"])
            if s.get("id") == COMP_REST_ID:
                self.error("import.shiftIdReserved", shiftId=s["id"])
            if s.get("start") is None or s.get("end") is None:
                self.error("import.missingTime", shiftId=s.get("id"))
                continue
            minutes = _minutes(s["end"]) - _minutes(s["start"])
            if minutes <= 0:
                minutes += 24 * 60
            if minutes > MAX_DUTY_HOURS * 60:
                self.error("import.shiftTooLong", shiftId=s.get("id"), minutes=minutes,
                           maxMinutes=MAX_DUTY_HOURS * 60)

        def demand(r, i):
            if self.present(r.get("areaId"), "demand", "areaId", i) and r["areaId"] not in area_ids:
                self.error("import.unknownArea", kind="demand", index=i, areaId=r["areaId"])
            for key in ("from", "to"):
                if r.get(key) is None:
                    self.missing("demand", key, i)
            for day in _list(r.get("days")):
                if day not in DAY_CODES:
                    self.error("import.badDemandDay", areaId=r.get("areaId"), index=i, day=day)
            low, high = r.get("min") or 0, r.get("max")
            if r.get("date") is None and not _list(r.get("days")) or low < 0 or high is not None and high < low:
                self.error("import.badDemand", areaId=r.get("areaId"), index=i)

        self.each(d.get("demand"), "demand", None, demand)
        self.each(d.get("holidays"), "holiday", None,
                  lambda h, i: h.get("date") is None and self.missing("holiday", "date", i))

        for i, p in enumerate(_list(d.get("people"))):
            if p is None:
                continue
            if not (p.get("weeklyHours") or 0) > 0:
                self.error("import.badWeeklyHours", personId=p.get("id"), weeklyHours=p.get("weeklyHours") or 0)
            if p.get("areaIds") is None:
                self.missing("person", "areaIds", i)
            for a in _list(p.get("areaIds")):
                if a not in area_ids:
                    self.error("import.unknownArea", kind="person", personId=p.get("id"), areaId=a)
            entry, leave = _date(p.get("entryDate")), _date(p.get("exitDate"))
            if entry and leave and leave < entry:
                self.error("import.badEmployment", personId=p.get("id"))
            for f in _list(p.get("familyRules")):
                if f is None:
                    self.error("import.missingField", kind="familyRule", field="familyRules", personId=p.get("id"))
                for w in _list(f.get("weekdays") if f else None):
                    if w is None or not 1 <= w <= 7:
                        self.error("import.badWeekday", kind="familyRule", personId=p.get("id"), weekday=w)

        keys = set()

        def restriction(r, i):
            pid, sid, category, weekday = r.get("personId"), r.get("shiftId"), r.get("category"), r.get("weekday") or 0
            self.person(pid, "restriction", i, None, None)
            if (sid is None) == (category is None):
                self.error("import.restrictionTarget", personId=pid)
            elif sid is not None and sid not in self.shift_ids:
                self.error("import.unknownShift", kind="restriction", personId=pid, shiftId=sid)
            if not 0 <= weekday <= 7:
                self.error("import.badWeekday", kind="restriction", personId=pid, weekday=weekday)
            if r.get("kind") is None:
                self.missing("restriction", "kind", i)
            start, end = _date(r.get("validFrom")), _date(r.get("validTo"))
            if start and end and end < start:
                self.error("import.badValidity", personId=pid)
            key = (pid, sid if sid is not None else str(category), weekday)
            if key in keys:
                self.warning("import.duplicateRestriction", personId=pid, shiftId=sid, category=category,
                             weekday=weekday)
            keys.add(key)

        self.each(d.get("restrictions"), "restriction", None, restriction)

        months = [_month(p.get("month")) for p in _list(d.get("periods")) if p is not None]
        first_planned = min((m for m in months if m), default=None)
        history_keys = set()

        def history(h, i):
            pid, month = h.get("personId"), h.get("month")
            self.person(pid, "history", i, None, month)
            if self.present(month, "history", "month", i):
                ym = _month(month)
                if ym is None:
                    self.error("import.badMonth", personId=pid, month=month)
                elif first_planned is not None and ym >= first_planned:
                    self.warning("import.historyInFuture", personId=pid, month=month)
                if pid is not None and (pid, month) in history_keys:
                    self.error("import.duplicateEntry", kind="history", personId=pid, month=month)
                history_keys.add((pid, month))
            if self.present(h.get("source"), "history", "source", i) and h["source"] not in ("import", "plan"):
                self.error("import.badHistorySource", personId=pid, month=month)

        self.each(d.get("history"), "history", None, history)

    # ── planning months ──────────────────────────────────────

    def periods(self, periods):
        months = set()

        def check(p, i):
            m = p.get("month")
            if m is None:
                self.missing("period", "month", i)
            elif m in months:
                self.error("import.duplicateId", kind="period", id=m)
            months.add(m)
            self.period(p, i)

        self.each(periods, "period", None, check)

    def period(self, p, index):
        m = p.get("month")
        for key in ("absences", "wishes", "fixed", "previousDuties"):
            if p.get(key) is None:
                self.missing("period", key, index, m)
        days = set()

        def absence(a, i):
            pid, day = a.get("personId"), a.get("date")
            known = self.person(pid, "absence", i, day, m)
            dated = self.present(day, "absence", "date", i, m) and self.in_month(day, m, "absence", pid)
            code = a.get("typeCode")
            if self.present(code, "absence", "typeCode", i, m) and code not in self.type_codes:
                self.error("import.unknownAbsenceType", personId=pid, typeCode=code, date=day)
            if known and dated:
                if (pid, day) in days:
                    self.warning("import.duplicateAbsence", personId=pid, date=day)
                days.add((pid, day))

        self.each(p.get("absences"), "absence", m, absence)

        def wish(w, i):
            pid, day = w.get("personId"), w.get("date")
            self.person(pid, "wish", i, day, m)
            if self.present(day, "wish", "date", i, m):
                self.in_month(day, m, "wish", pid)
            if self.present(w.get("kind"), "wish", "kind", i, m) and w.get("shiftId") is None \
                    and w["kind"] in ("SHIFT", "NOT_SHIFT"):
                self.error("import.wishWithoutShift", personId=pid, date=day)
            self.shift(w.get("shiftId"), "wish", pid, day)

        self.each(p.get("wishes"), "wish", m, wish)
        fixed_days = set()

        def fixed(f, i):
            pid, day = f.get("personId"), f.get("date")
            known = self.person(pid, "fixed", i, day, m)
            dated = self.present(day, "fixed", "date", i, m) and self.in_month(day, m, "fixed", pid)
            self.shift(f.get("shiftId"), "fixed", pid, day)
            if known and dated:
                if (pid, day) in fixed_days:
                    self.error("import.duplicateEntry", kind="fixed", personId=pid, date=day)
                fixed_days.add((pid, day))

        self.each(p.get("fixed"), "fixed", m, fixed)

        def previous(v, i):
            pid, day = v.get("personId"), v.get("date")
            self.person(pid, "previousDuty", i, day, m)
            ym, d = _month(m), _date(day)
            if self.present(day, "previousDuty", "date", i, m) and ym and d and d >= date(*ym, 1):
                self.error("import.dateOutsideMonth", kind="previousDuty", personId=pid, date=day, month=m)
            if self.present(v.get("shiftId"), "previousDuty", "shiftId", i, m):
                self.shift(v["shiftId"], "previousDuty", pid, day)

        self.each(p.get("previousDuties"), "previousDuty", m, previous)


def validate(d) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]]:
    """Errors and warnings as (code, params), like Dispofold's DatasetValidator."""
    v = _Validator()
    if not isinstance(d, dict) or d.get("format") != FORMAT:
        v.error("import.format")
    elif d.get("version") != VERSION:
        v.error("import.version", version=d.get("version"))
    else:
        v.master(d)
        v.periods(d.get("periods"))
    return v.errors, v.warnings


def violations(d) -> list[tuple[str, dict]]:
    """Errors and warnings together: an export must have neither."""
    errors, warnings = validate(d)
    return errors + warnings


def schema_errors(d) -> list[str]:
    """Violations of Dispofold's JSON schema (version 2)."""
    return sorted(f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in SCHEMA.iter_errors(d))


def problems(d) -> list:
    """Everything that keeps Dispofold from importing the document cleanly: schema, errors, warnings."""
    return schema_errors(d) + violations(d)
