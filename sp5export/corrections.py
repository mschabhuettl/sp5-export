"""Corrections file: person master data corrected by hand, applied to the exported dataset as the last step.

{"format": "dispofold/corrections", "version": 1, "people": {"E123": {"note": "...", "weeklyHours": 30.0, ...}}}

A field that is present replaces the exported value completely, an absent field leaves it alone. Problems with
single entries never stop the export: the field (or the person) is skipped and reported. Reports and warnings
name person ids, field names and reasons only, never free text from the file.
"""

import json
import math
import re
from datetime import date
from pathlib import Path

FORMAT = "dispofold/corrections"
VERSION = 1
CATEGORIES = ("EARLY", "DAY", "LATE", "NIGHT")
# The person fields of the dataset that can be corrected, in the order they are applied; restrictions last.
FIELDS = ("weeklyHours", "areaIds", "hoursAccount", "compRestBalance", "familyRules", "entryDate", "exitDate",
          "shiftMix", "restrictions")
FIXED = ("id", "externalId", "firstName", "lastName", "shortName")
SHIFT_MIX_TOLERANCE = 0.01  # Dispofold's allowed difference of a mix map's sum from 1
PERSON_ID = re.compile(r"E\d+$")
DATE = re.compile(r"\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
TIME = re.compile(r"([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")
RESTRICTION_KEYS = {"shiftId", "category", "weekday", "kind", "validFrom", "validTo"}
FAMILY_RULE_KEYS = {"weekdays", "latestEnd", "earliestStart", "noNights", "note"}


class CorrectionsError(Exception):
    """The corrections file cannot be used at all (unreadable, unknown format or version)."""


def load(path: str | Path) -> dict:
    """The people map of the corrections file; raises CorrectionsError (German) when the file cannot be used."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise CorrectionsError(f"Korrekturdatei {path} nicht lesbar ({type(e).__name__})") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise CorrectionsError(f"Korrekturdatei {path}: unbekanntes Format, erwartet \"{FORMAT}\"")
    version = data.get("version")
    if not _int(version) or not 1 <= version <= VERSION:
        raise CorrectionsError(f"Korrekturdatei {path}: Version nicht unterstützt, unterstützt bis {VERSION}")
    if not isinstance(data.get("people"), dict):
        raise CorrectionsError(f"Korrekturdatei {path}: 'people' muss ein Objekt sein")
    return data["people"]


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _date(v) -> bool:
    if not isinstance(v, str) or not DATE.match(v):
        return False
    try:
        date.fromisoformat(v)
    except ValueError:
        return False
    return True


def _shares(shares, keys_ok) -> str | None:
    if not isinstance(shares, dict):
        return "Anteile müssen ein Objekt sein"
    if not all(keys_ok(k) for k in shares):
        return "unbekannter Schlüssel in den Anteilen"
    if not all(_number(s) and 0 <= s <= 1 for s in shares.values()):
        return "Anteil nicht zwischen 0 und 1"
    if shares and abs(sum(shares.values()) - 1) > SHIFT_MIX_TOLERANCE:
        return "Anteile ergeben nicht 1"
    return None


def _mix_text(mix) -> str:
    if not mix:
        return "kein Mix"
    return " | ".join(f"{part} " + ", ".join(f"{k} {v:.3f}" for k, v in mix[part].items())
                     for part in ("categories", "areas") if mix.get(part))


def _text(field: str, value) -> str:
    """The value as short text: numbers, dates and ids as they are, lists of rules and restrictions as counts."""
    if value is None:
        return "null"
    if field == "areaIds":
        return " ".join(value)
    if field == "shiftMix":
        return _mix_text(value)
    if field == "familyRules":
        return f"{len(value)} Regeln"
    if field == "restrictions":
        return f"{len(value)} Einschränkungen"
    return str(value)


class _Check:
    """Checks one field value of one person like Dispofold's import; returns the reason or None."""

    def __init__(self, area_ids: set[str], shift_ids: set[str]):
        self.area_ids, self.shift_ids = area_ids, shift_ids

    def weeklyHours(self, v, person, entry):
        return None if _number(v) and v > 0 else "muss eine Zahl größer 0 sein"

    def hoursAccount(self, v, person, entry):
        return None if _number(v) else "muss eine Zahl sein"

    def compRestBalance(self, v, person, entry):
        return None if _int(v) else "muss eine ganze Zahl sein"

    def areaIds(self, v, person, entry):
        if not isinstance(v, list) or not v or not all(isinstance(a, str) for a in v) or len(set(v)) != len(v):
            return "muss eine nicht leere Liste verschiedener Bereichs-Ids sein"
        if not set(v) <= self.area_ids:
            return "unbekannte Bereichs-Id"
        # The mix the person will have: the corrected one when it is valid for these areas, else the exported one.
        mix = person.get("shiftMix")
        if "shiftMix" in entry and self.shiftMix(entry["shiftMix"], {**person, "areaIds": v}, entry) is None:
            mix = entry["shiftMix"]
        if mix and not set(mix.get("areas", {})) <= set(v):
            return "Schichtmix hat Anteile in einem Bereich, der nicht mehr in areaIds steht (shiftMix mitkorrigieren)"
        return None

    def familyRules(self, v, person, entry):
        if v is None:
            return None
        if not isinstance(v, list) or not all(isinstance(f, dict) and set(f) <= FAMILY_RULE_KEYS for f in v):
            return "muss eine Liste von Regeln (weekdays, latestEnd, earliestStart, noNights, note) sein"
        for f in v:
            days = f.get("weekdays")
            if days is not None and not (isinstance(days, list) and all(_int(d) and 1 <= d <= 7 for d in days)):
                return "weekdays muss eine Liste von 1 bis 7 sein"
            if any(f.get(k) is not None and not (isinstance(f[k], str) and TIME.match(f[k]))
                   for k in ("latestEnd", "earliestStart")):
                return "Uhrzeit nicht im Format HH:MM"
            if "noNights" in f and not isinstance(f["noNights"], bool):
                return "noNights muss true oder false sein"
            if f.get("note") is not None and not isinstance(f["note"], str):
                return "note muss Text sein"
        return None

    def entryDate(self, v, person, entry):
        if v is not None and not _date(v):
            return "Datum nicht im Format JJJJ-MM-TT"
        # Against the exit date the person will have: the corrected one when it is a date or null.
        leave = entry["exitDate"] if "exitDate" in entry and (entry["exitDate"] is None or _date(entry["exitDate"])) \
            else person.get("exitDate")
        return self._order(v, leave)

    def exitDate(self, v, person, entry):
        if v is not None and not _date(v):
            return "Datum nicht im Format JJJJ-MM-TT"
        return self._order(person.get("entryDate"), v)

    @staticmethod
    def _order(entry, leave):
        return "Austritt vor Eintritt" if _date(entry) and _date(leave) and leave < entry else None

    def shiftMix(self, v, person, entry):
        if v is None:
            return None
        if not isinstance(v, dict) or not set(v) <= {"categories", "areas"}:
            return "muss null oder ein Objekt mit categories und areas sein"
        problem = _shares(v.get("categories", {}), lambda k: k in CATEGORIES) \
            or _shares(v.get("areas", {}), lambda k: isinstance(k, str))
        if problem:
            return problem
        if not set(v.get("areas", {})) <= set(person["areaIds"]):
            return "Bereichsanteil für einen Bereich, der nicht in areaIds steht"
        return None

    def restrictions(self, v, person, entry):
        if not isinstance(v, list):
            return "muss eine Liste sein"
        for r in v:
            if not isinstance(r, dict) or not set(r) <= RESTRICTION_KEYS:
                return "Einschränkung mit unbekanntem Schlüssel (personId wird nicht angegeben)"
            sid, category = r.get("shiftId"), r.get("category")
            if (sid is None) == (category is None):
                return "Einschränkung braucht genau eines von shiftId und category"
            if sid is not None and sid not in self.shift_ids:
                return "unbekannte Schicht-Id"
            if category is not None and category not in CATEGORIES:
                return "unbekannte Kategorie"
            if not (_int(r.get("weekday")) and 0 <= r["weekday"] <= 7):
                return "weekday muss eine ganze Zahl von 0 bis 7 sein"
            if r.get("kind") not in ("NEVER", "ON_REQUEST"):
                return "kind muss NEVER oder ON_REQUEST sein"
            start, end = r.get("validFrom"), r.get("validTo")
            if any(d is not None and not _date(d) for d in (start, end)):
                return "Gültigkeit nicht im Format JJJJ-MM-TT"
            if start is not None and end is not None and end < start:
                return "Gültigkeit endet vor dem Beginn"
        return None


def apply(dataset: dict, people: dict) -> tuple[list[tuple], list[str], tuple[int, int, int]]:
    """Apply the corrections to `dataset` in place. Returns the report rows (person, field, status, reason,
    exported value, corrected value), the warnings and the counts (people corrected, fields applied, skipped)."""
    by_id = {p["id"]: p for p in dataset["people"]}
    check = _Check({a["id"] for a in dataset["areas"]}, {s["id"] for s in dataset["shifts"]})
    rows, warnings = [], []
    corrected, applied, skipped = 0, 0, 0
    for key in sorted(people, key=lambda k: (0, int(k[1:])) if PERSON_ID.match(k) else (1, k)):
        entry = people[key]
        pid = key if PERSON_ID.match(key) else ""  # anything else is not a dataset id; never echoed
        fields = [f for f in entry if f != "note"] if isinstance(entry, dict) else []
        person = by_id.get(pid)
        if person is None or not isinstance(entry, dict):
            why = "kein Objekt" if person is not None else (
                "Mitarbeiter nicht exportiert" if pid else "keine Mitarbeiter-Id (E und SP5-Id)")
            status = "ungueltig" if person is not None else "person_fehlt"
            rows += [(pid, f if f in FIELDS + FIXED else "", status, why, "", "") for f in fields or [""]]
            skipped += max(len(fields), 1)
            warnings.append(f"Korrektur für {pid or 'eine unbekannte Id'} übersprungen: {why}")
            continue
        changed = False
        own = [r for r in dataset["restrictions"] if r["personId"] == pid]
        for field in sorted(fields, key=lambda f: FIELDS.index(f) if f in FIELDS else len(FIELDS)):
            value = entry[field]
            old = own if field == "restrictions" else person.get(field)
            if field not in FIELDS:
                why = "nicht korrigierbar" if field in FIXED else "unbekanntes Feld"
                rows.append((pid, field if field in FIXED else "", "ungueltig", why, "", ""))
            elif (why := getattr(check, field)(value, person, entry)) is not None:
                rows.append((pid, field, "ungueltig", why, _text(field, old), ""))
            else:
                rows.append((pid, field, "angewendet", "", _text(field, old), _text(field, value)))
                if field == "restrictions":
                    rest = [r for r in dataset["restrictions"] if r["personId"] != pid]
                    at = next((i for i, r in enumerate(rest) if r["personId"] > pid), len(rest))
                    dataset["restrictions"] = rest[:at] + [{"personId": pid, **r} for r in value] + rest[at:]
                elif field == "shiftMix" and value is None:
                    person.pop("shiftMix", None)
                else:
                    person[field] = float(value) if field in ("weeklyHours", "hoursAccount") else value
                applied += 1
                changed = True
                continue
            skipped += 1
            warnings.append(f"Korrektur {pid} {field if field in FIELDS + FIXED else '(unbekanntes Feld)'} "
                            f"übersprungen: {why}")
        corrected += changed
    return rows, warnings, (corrected, applied, skipped)
