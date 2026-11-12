"""
High-Level-Datenbankzugriff auf Schichtplaner5-.DBF-Dateien.
"""

import calendar
import dataclasses
import hashlib
import json
import logging
import os
import tempfile
import threading
from datetime import date, datetime, timedelta
from typing import Any

from . import _resource_paths as _paths
from . import calculations as calc
from .color_utils import bgr_to_hex, is_light_color
from .dbf_reader import get_table_fields, read_dbf, read_dbf_buffer
from .dbf_writer import (
    _read_header_info as _dbf_header_info,
)
from .dbf_writer import (
    append_record as _dbf_append_record,
)
from .dbf_writer import (
    delete_record as _dbf_delete_record,
)
from .dbf_writer import (
    find_all_records,
)
from .dbf_writer import (
    pack_table as _dbf_pack_table,
)
from .dbf_writer import (
    update_record as _dbf_update_record,
)

_db_logger = logging.getLogger("sp5api")

# ── Globaler Request-übergreifender DBF-Cache ───────────────────
# Bildet (db_path, table_name) → (mtime, data) ab.
# Erspart das Neu-Einlesen unveränderter DBF-Dateien über Requests hinweg.
# RLock (re-entrant), weil _read() und _invalidate_cache() aus demselben
# Thread kommen können (z. B. ruft der Schreibpfad _invalidate_cache, dann _read).
_GLOBAL_DBF_CACHE: dict[tuple, tuple] = {}
# Lese-Schicht über dem DBF-Cache: (db_path, table, date_field) → (mtime,
# {YYYY-MM: [records]}). Gruppiert datierte Tabellen (5MASHI) nach Monat, damit
# get_schedule nur den angefragten Monat statt der ganzen Tabelle scannt. An die
# gleiche mtime gekoppelt wie _GLOBAL_DBF_CACHE → konsistent invalidiert.
_GLOBAL_MONTH_INDEX: dict[tuple, tuple] = {}
_CACHE_LOCK = threading.RLock()


def _invalidate_cache_for_file(filepath: str) -> None:
    """Globalen Cache-Eintrag der Tabelle von *filepath* verwerfen.

    Zentrale Invalidierung für alle Schreibpfade: der mtime-Check in
    ``_read()`` erkennt einen Write im selben Dateisystem-Zeittick nicht
    (mtime unverändert) und würde dann veraltete Daten liefern. Der
    Pfadvergleich normalisiert, weil ``db_path`` z. B. mit ``/`` enden kann.
    """
    target = os.path.normpath(filepath)
    with _CACHE_LOCK:
        stale = [
            key
            for key in _GLOBAL_DBF_CACHE
            if os.path.normpath(os.path.join(key[0], f"5{key[1]}.DBF")) == target
        ]
        for key in stale:
            _GLOBAL_DBF_CACHE.pop(key, None)
        # Den abgeleiteten Monatsindex derselben Tabelle ebenfalls verwerfen.
        stale_idx = [
            k
            for k in _GLOBAL_MONTH_INDEX
            if os.path.normpath(os.path.join(k[0], f"5{k[1]}.DBF")) == target
        ]
        for k in stale_idx:
            _GLOBAL_MONTH_INDEX.pop(k, None)


# dbf_writer-Funktionen mit zentraler Cache-Invalidierung umhüllt, damit
# JEDER Schreibpfad in dieser Datei den globalen DBF-Cache aktualisiert
# (Repro: Write + sofortiger Read im selben mtime-Tick lieferte alte Daten).
def append_record(
    filepath: str, fields: list[dict], record: dict, autoid_field: str | None = None
) -> int:
    result = _dbf_append_record(filepath, fields, record, autoid_field=autoid_field)
    _invalidate_cache_for_file(filepath)
    return result


def update_record(filepath: str, fields: list[dict], record_index: int, data: dict) -> None:
    _dbf_update_record(filepath, fields, record_index, data)
    _invalidate_cache_for_file(filepath)


def delete_record(filepath: str, fields: list[dict], record_index: int) -> None:
    _dbf_delete_record(filepath, fields, record_index)
    _invalidate_cache_for_file(filepath)


def pack_table(filepath: str) -> int:
    result = _dbf_pack_table(filepath)
    _invalidate_cache_for_file(filepath)
    return result


def _atomic_write_json(path: str, data, **dump_kwargs) -> None:
    """Schreibt ``data`` als JSON **atomar** nach ``path``: erst vollständig in
    eine Temp-Datei im selben Verzeichnis, dann ``os.replace`` (auf POSIX ein
    atomarer Rename). Bricht das Schreiben vorher ab (Prozess-``kill``, Disk-voll,
    OOM), bleibt die bestehende Datei unberührt — ohne das würde ``open(path,"w")``
    sie sofort truncaten und ein Teil-Dump hinterließe unlesbares JSON, das die
    Store-Reader hier still als leeren Store interpretieren (Totalverlust aller
    2FA-Geheimnisse / Passwort-Hashes / Wünsche / Tokens / Kommentare …).

    ``dump_kwargs`` werden an ``json.dump`` durchgereicht, damit jeder Store sein
    bestehendes On-Disk-Format (``indent``/``ensure_ascii``) unverändert behält.
    """
    dir_ = os.path.dirname(os.path.abspath(path)) or "."
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=dir_, delete=False, suffix=".tmp"
    ) as tmp:
        json.dump(data, tmp, **dump_kwargs)
        tmp_path = tmp.name
    os.replace(tmp_path, path)


class SP5Database:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _table(self, name: str) -> str:
        return os.path.join(self.db_path, f"5{name}.DBF")

    def _read(self, name: str) -> list[dict[str, Any]]:
        """Read a DBF table via a global, content-aware cache.

        Cache-Eintrag: ``(mtime, size, content_hash, data)``, geteilt über alle
        Requests/Instanzen desselben db_path.

        - **Schnellpfad** (unveränderte ``(mtime, size)``): liefert den Parse ohne
          jede Datei-I/O — der Normalfall zwischen zwei Änderungen.
        - **mtime/size geändert** (z. B. ein Mirror-Re-Sync, der die Dateien neu
          schreibt): die Bytes werden EINMAL gelesen und gehasht. Ist der Inhalt
          byte-identisch zur gecachten Version (No-op-Sync), wird der vorhandene
          Parse behalten und nur ``(mtime, size)`` aktualisiert — KEIN Re-Parse.
          Nur echt geänderter Inhalt wird neu geparst.

        Schreibzugriffe gehen über dbf_writer direkt auf die DBF (write-through);
        der Cache ist reine Lese-Beschleunigung, die DBF bleibt Quelle der Wahrheit.
        """
        path = self._table(name)
        key = (self.db_path, name)
        try:
            st = os.stat(path)
            mtime, size = st.st_mtime, st.st_size
        except OSError:
            mtime, size = 0.0, 0

        with _CACHE_LOCK:
            cached = _GLOBAL_DBF_CACHE.get(key)
            if cached is not None and cached[0] == mtime and cached[1] == size:
                return cached[3]

        # (mtime, size) geändert oder Erstzugriff: Bytes einmal lesen und hashen.
        try:
            with open(path, "rb") as fh:
                raw_bytes = fh.read()
        except OSError as _exc:
            _db_logger.error(
                "DBF read error: table=%s path=%s error=%s", name, path, _exc
            )
            return []

        content_hash = hashlib.blake2b(raw_bytes, digest_size=16).digest()
        with _CACHE_LOCK:
            cached = _GLOBAL_DBF_CACHE.get(key)
            if cached is not None and cached[2] == content_hash:
                # No-op-Sync: gleiche Bytes, nur neue mtime → Parse wiederverwenden.
                _GLOBAL_DBF_CACHE[key] = (mtime, size, content_hash, cached[3])
                return cached[3]

        data = read_dbf_buffer(raw_bytes)
        with _CACHE_LOCK:
            _GLOBAL_DBF_CACHE[key] = (mtime, size, content_hash, data)
        return data

    def _read_by_month(
        self, name: str, date_field: str = "DATE"
    ) -> dict[str, list[dict[str, Any]]]:
        """Datensätze einer datierten Tabelle nach Monats-Präfix (YYYY-MM) gruppiert.

        Reine Lese-Schicht über :meth:`_read`: der Index ist an den Inhalts-Hash
        des DBF-Caches gekoppelt und wird nur neu gebaut, wenn sich der Inhalt
        wirklich ändert (nicht schon bei einem No-op-Sync oder gleichem mtime-Tick).
        Erspart get_schedule den Volltabellen-Scan pro Request. Datumswerte sind
        ISO-zero-padded (YYYY-MM-DD), daher ist ``d[:7]`` exakt der Monats-Präfix,
        gegen den get_schedule früher mit ``startswith`` filterte.
        """
        # _read() hält den DBF-Cache (inkl. content_hash) aktuell und liefert die Daten.
        data = self._read(name)
        with _CACHE_LOCK:
            entry = _GLOBAL_DBF_CACHE.get((self.db_path, name))
            content_hash = entry[2] if entry is not None else None
            key = (self.db_path, name, date_field)
            cached = _GLOBAL_MONTH_INDEX.get(key)
            if cached is not None and cached[0] == content_hash:
                return cached[1]

        index: dict[str, list[dict[str, Any]]] = {}
        for r in data:
            d = r.get(date_field) or ""
            if len(d) >= 7:
                index.setdefault(d[:7], []).append(r)

        with _CACHE_LOCK:
            _GLOBAL_MONTH_INDEX[key] = (content_hash, index)
        return index

    def _invalidate_cache(self, name: str) -> None:
        """Verwirft den globalen Cache-Eintrag einer Tabelle nach einem Write.

        Meist unnötig, weil _read() die mtime automatisch neu prüft — explizit
        invalidieren hilft aber, wenn die Datei im selben OS-Zeittick
        geschrieben und sofort neu gelesen wird (mtime-Granularität).
        """
        key = (self.db_path, name)
        with _CACHE_LOCK:
            _GLOBAL_DBF_CACHE.pop(key, None)
            # Den abgeleiteten Monatsindex derselben Tabelle miträumen
            # (mehrere date_field-Varianten möglich).
            for ik in [k for k in _GLOBAL_MONTH_INDEX if k[0] == self.db_path and k[1] == name]:
                _GLOBAL_MONTH_INDEX.pop(ik, None)

    def _color_fields(self, record: dict) -> dict:
        """Convert BGR color fields to hex strings."""
        for key in (
            "COLORTEXT",
            "COLORBAR",
            "COLORBK",
            "CBKLABEL",
            "CBKSCHED",
            "CFGLABEL",
        ):
            if key in record and isinstance(record[key], int):
                record[key + "_HEX"] = bgr_to_hex(record[key])
                record[key + "_LIGHT"] = is_light_color(record[key])
        return record

    # ── Berechnungsschicht-Adapter (sp5lib.calculations, Spec Kap. 3) ──
    def _calc_holidays(self) -> dict[date, int]:
        """5HOLID als date->INTERVAL-Kalender (0 = ganztägig, sonst halb)."""
        return calc.holiday_calendar(self._read("HOLID"))

    @staticmethod
    def _calc_context(emp: dict) -> calc.EmployeeContext:
        """5EMPL-Berechnungsparameter; ohne WORKDAYS-Maske gilt Mo-Fr."""
        ctx = calc.EmployeeContext.from_record(emp)
        if not str(emp.get("WORKDAYS") or "").strip():
            ctx = dataclasses.replace(
                ctx, workdays=(True, True, True, True, True, False, False, False)
            )
        return ctx

    def _movement_by_employee(
        self,
        table: str,
        von: date,
        bis: date,
        employee_id: int | None = None,
    ) -> dict[int, list[dict]]:
        """Bewegungsdaten (DATE-Spalte) je Mitarbeiter im Zeitraum [von, bis]."""
        lo, hi = von.isoformat(), bis.isoformat()
        out: dict[int, list[dict]] = {}
        for r in self._read(table):
            d = r.get("DATE") or ""
            if not lo <= d <= hi:
                continue
            eid = r.get("EMPLOYEEID")
            if eid is None or (employee_id is not None and eid != employee_id):
                continue
            out.setdefault(eid, []).append(r)
        return out

    def _cycle_shifts_by_employee(
        self, von: date, bis: date, employee_id: int | None = None
    ) -> dict[int, list[dict]]:
        """Expandierte 5CYASS-Dienste je Mitarbeiter (Spec 3.4.2 Nr. 2).

        Tage, an denen derselbe Mitarbeiter bereits einen 5MASHI-Eintrag hat,
        werden übersprungen: die lib materialisiert Zyklen nach 5MASHI
        (generate_schedule_from_cycle); der Schutz verhindert Doppelzählung
        materialisierter Pläne.
        """
        assignments = []
        for a in self._read("CYASS"):
            if employee_id is not None and a.get("EMPLOYEEID") != employee_id:
                continue
            entrance = a.get("ENTRANCE")
            if not isinstance(entrance, (int, float)):
                a = dict(a, ENTRANCE=0)  # defensiv: Altbestand mit Datum im Feld
            assignments.append(a)
        if not assignments:
            return {}
        expanded = calc.expand_cycle_assignments(
            assignments,
            cycles=self._read("CYCLE"),
            cycle_entries=self._read("CYENT"),
            cycle_exceptions=self._read("CYEXC"),
            von=von,
            bis=bis,
        )
        mashi_days = {
            (r.get("EMPLOYEEID"), (r.get("DATE") or "")[:10])
            for r in self._read("MASHI")
        }
        out: dict[int, list[dict]] = {}
        for rec in expanded:
            if (rec["EMPLOYEEID"], rec["DATE"]) in mashi_days:
                continue
            out.setdefault(rec["EMPLOYEEID"], []).append(rec)
        return out

    def _calc_inputs(
        self, von: date, bis: date, employee_id: int | None = None
    ) -> dict:
        """Gemeinsame Eingaben der Berechnungsschicht für [von, bis].

        Plan-Quellen (5MASHI/5SPSHI/5CYASS) werden mit einem Tag Vorlauf
        gelesen, damit Tageswechsel-Fenster des Vortags (Spec 3.4.3 Nr. 8)
        in die Zuschlagsberechnung einfließen können.
        """
        margin = von - timedelta(days=1)
        return {
            "holidays": self._calc_holidays(),
            "shifts_by_id": {s["ID"]: s for s in self.get_shifts(include_hidden=True)},
            "leave_types_by_id": {
                lt["ID"]: lt for lt in self.get_leave_types(include_hidden=True)
            },
            "manual": self._movement_by_employee("MASHI", margin, bis, employee_id),
            "special": self._movement_by_employee("SPSHI", margin, bis, employee_id),
            "cycle": self._cycle_shifts_by_employee(margin, bis, employee_id),
            "absences": self._movement_by_employee("ABSEN", von, bis, employee_id),
            "bookings": self._movement_by_employee("BOOK", von, bis, employee_id),
            "overtimes": self._movement_by_employee("OVER", von, bis, employee_id),
        }

    @staticmethod
    def _plan_kwargs(inputs: dict, employee_id: int) -> dict:
        """Plan-Keyword-Argumente eines Mitarbeiters für die calc-Funktionen."""
        return {
            "holidays": inputs["holidays"],
            "shifts_by_id": inputs["shifts_by_id"],
            "manual_shifts": inputs["manual"].get(employee_id, ()),
            "cycle_shifts": inputs["cycle"].get(employee_id, ()),
            "special_shifts": inputs["special"].get(employee_id, ()),
        }

    @staticmethod
    def _last_of_month(year: int, month: int) -> date:
        return date(year, month, calendar.monthrange(year, month)[1])

    # ── Employees ──────────────────────────────────────────────
    def get_employees(self, include_hidden: bool = False) -> list[dict]:
        """Liefert die aktiven Mitarbeiter, optional inklusive versteckter."""
        rows = self._read("EMPL")
        result = []
        for r in rows:
            if not include_hidden and r.get("HIDE"):
                continue
            # Parse WORKDAYS: stored as plain ASCII "1 1 1 1 1 0 0 0"
            # (dbf_reader now correctly decodes ASCII vs UTF-16 LE fields)
            wd = r.get("WORKDAYS", "")
            if wd:
                r["WORKDAYS_LIST"] = [x == "1" for x in wd.split()]
            else:
                r["WORKDAYS_LIST"] = []
            # Auto-generate SHORTNAME if empty:
            # Format: Vorname-Initial + erste 2 Buchstaben Nachname (uppercase)
            # e.g. "Hans Mueller" → "HMU"  (H from Hans, MU from Mueller)
            original_shortname = (r.get("SHORTNAME") or "").strip()
            if not original_shortname:
                surname = (r.get("NAME", "") or "").strip()
                firstname = (r.get("FIRSTNAME", "") or "").strip()
                if firstname and surname:
                    r["SHORTNAME"] = (firstname[0] + surname[:2]).upper()
                elif surname:
                    r["SHORTNAME"] = surname[:3].upper()
                elif firstname:
                    r["SHORTNAME"] = firstname[:3].upper()
                else:
                    r["SHORTNAME"] = "???"
                r["SHORTNAME_GENERATED"] = (
                    True  # flag: was auto-generated, not stored in DB
                )
            else:
                r["SHORTNAME"] = original_shortname
                r["SHORTNAME_GENERATED"] = False
            # Convert color fields to hex
            self._color_fields(r)
            result.append(r)
        # Original-Default „Ansicht > Sortierung > Name": alphabetisch nach
        # Nachname, dann Vorname (Wine-Orakel; POSITION entspricht dem dortigen
        # Modus „Vorgabe" und bleibt als Feld erhalten).
        result.sort(
            key=lambda x: (
                (x.get("NAME") or "").lower(),
                (x.get("FIRSTNAME") or "").lower(),
                x.get("POSITION") or 0,
            )
        )
        return result

    def get_employee(self, emp_id: int) -> dict | None:
        """Return a single employee by ID, or None if not found."""
        for e in self.get_employees(include_hidden=True):
            if e.get("ID") == emp_id:
                return e
        return None

    # ── Groups ─────────────────────────────────────────────────
    def get_groups(self, include_hidden: bool = False) -> list[dict]:
        """Liefert die Gruppen, optional inklusive versteckter."""
        rows = self._read("GROUP")
        if not include_hidden:
            rows = [r for r in rows if not r.get("HIDE")]
        for r in rows:
            self._color_fields(r)
        rows.sort(key=lambda x: x.get("POSITION", 0))
        return rows

    def get_group_members(self, group_id: int) -> list[int]:
        """Liefert die Mitarbeiter-IDs einer Gruppe."""
        assignments = self._read("GRASG")
        return [a["EMPLOYEEID"] for a in assignments if a.get("GROUPID") == group_id]

    def get_all_group_members(self) -> dict[int, list[int]]:
        """Liefert group_id -> [employee_ids] in EINEM Durchlauf (vermeidet N+1)."""
        assignments = self._read("GRASG")
        result: dict[int, list[int]] = {}
        for a in assignments:
            gid = a.get("GROUPID")
            eid = a.get("EMPLOYEEID")
            if gid is not None and eid is not None:
                result.setdefault(gid, []).append(eid)
        return result

    def get_employee_groups(self, emp_id: int) -> list[int]:
        """Liefert die Gruppen-IDs, denen ein Mitarbeiter angehört."""
        assignments = self._read("GRASG")
        return [a["GROUPID"] for a in assignments if a.get("EMPLOYEEID") == emp_id]

    # ── Shifts ─────────────────────────────────────────────────
    def get_shifts(self, include_hidden: bool = False) -> list[dict]:
        """Return all shift definitions, optionally including hidden ones."""
        rows = self._read("SHIFT")
        if not include_hidden:
            rows = [r for r in rows if not r.get("HIDE")]
        for r in rows:
            self._color_fields(r)
            # Parse STARTEND per weekday
            times: dict[int, Any] = {}
            for i in range(7):
                key = f"STARTEND{i}"
                val = r.get(key, "").strip()
                if val and "-" in val:
                    parts = val.split("-")
                    if len(parts) == 2:
                        times[i] = {"start": parts[0].strip(), "end": parts[1].strip()}
                    else:
                        times[i] = None  # type: ignore[assignment]
                else:
                    times[i] = None  # type: ignore[assignment]
            r["TIMES_BY_WEEKDAY"] = times
        rows.sort(key=lambda x: x.get("POSITION", 0))
        return rows

    def get_shift(self, shift_id: int) -> dict | None:
        """Return a single shift definition by ID, or None if not found."""
        for s in self.get_shifts(include_hidden=True):
            if s.get("ID") == shift_id:
                return s
        return None

    # ── Leave Types ────────────────────────────────────────────
    def get_leave_types(self, include_hidden: bool = False) -> list[dict]:
        """Return all leave/absence type definitions."""
        rows = self._read("LEAVT")
        if not include_hidden:
            rows = [r for r in rows if not r.get("HIDE")]
        for r in rows:
            self._color_fields(r)
        rows.sort(key=lambda x: x.get("POSITION", 0))
        return rows

    def get_leave_type(self, lt_id: int) -> dict | None:
        """Return a single leave type by ID, or None if not found."""
        for lt in self.get_leave_types(include_hidden=True):
            if lt.get("ID") == lt_id:
                return lt
        return None

    # ── Workplaces ─────────────────────────────────────────────
    def get_workplaces(self, include_hidden: bool = False) -> list[dict]:
        """Return all workplace definitions."""
        rows = self._read("WOPL")
        if not include_hidden:
            rows = [r for r in rows if not r.get("HIDE")]
        for r in rows:
            self._color_fields(r)
        rows.sort(key=lambda x: x.get("POSITION", 0))
        return rows

    # ── Holidays ───────────────────────────────────────────────
    def get_holidays(self, year: int | None = None) -> list[dict]:
        """Liefert die Feiertage für Jahr und Bundesland."""
        rows = self._read("HOLID")
        if year is not None:
            result = []
            year_str = str(year)
            for r in rows:
                interval = r.get("INTERVAL", 0)
                date = r.get("DATE", "")
                if interval == 1:
                    # Recurring: show every year, replace year in date
                    if date and len(date) >= 10:
                        adjusted_date = year_str + date[4:]
                    else:
                        adjusted_date = date
                    row_copy = dict(r)
                    row_copy["DATE"] = adjusted_date
                    result.append(row_copy)
                elif date.startswith(year_str):
                    result.append(r)
            result.sort(key=lambda x: x.get("DATE", ""))
            return result
        rows.sort(key=lambda x: x.get("DATE", ""))
        return rows

    def get_holiday_dates(self, year: int) -> set:
        return {r["DATE"] for r in self.get_holidays(year) if r.get("DATE")}

    # ── Schedule ───────────────────────────────────────────────
    def get_schedule(
        self,
        year: int,
        month: int,
        group_id: int | None = None,
        plan: str = "ist",
    ) -> list[dict]:
        """
        Get schedule entries for a month.
        Returns list of {employee_id, date, type, shift_id, leave_type_id,
                          shift_name, shift_short, color_bk, color_text, workplace_id, ...}

        Unmaterialisierte Zyklusdienste (5CYASS, Spec 6.3/4.2) erscheinen als
        generierte Einträge mit ``"source": "cycle"``; materialisierte
        5MASHI-Tage und 5SPSHI-Arbeitszeitabweichungen (SHIFTID gesetzt,
        Spec 3.4.4 Nr. 12) gewinnen.

        Soll-/Istplan (Spec 4.12, D-58): das 5MASHI-Feld ``TYPE`` kodiert
        0=Istplan (Normaleintrag), 1=Sollplan (Zielvorgabe) — Dekompilat-belegt
        (SP5V/SP5R-Renderer, Konstruktor-Default 0). Jeder reguläre Dienst trägt
        ``schedule_type`` (0/1). ``plan`` wählt die Sicht:
          "ist"  (Vorgabe) = nur Istplan (schedule_type != 1) + Sonderdienste
                  + Abwesenheiten,
          "soll" = nur Sollplan (schedule_type == 1) + Sonderdienste + Abwesenheiten,
          "both" = alle regulären Dienste beider Pläne (2-/3-Zeilen-Ansicht).
        Sonderdienste (5SPSHI) werden NICHT plan-gefiltert: ihr ``TYPE`` kodiert
        (Spec D-53/3.4.4, UNSICHER) Sonderdienst vs. Arbeitszeitabweichung,
        nicht Soll/Ist; sie erscheinen in jeder Sicht.
        """
        prefix = f"{year:04d}-{month:02d}"
        entries = []

        # Get shift assignments (MASHI) — nur den angefragten Monat (Monatsindex
        # statt Volltabellen-Scan; identische Auswahl wie d.startswith(prefix)).
        for r in self._read_by_month("MASHI").get(prefix, []):
            d = r.get("DATE", "")
            if d:
                entries.append(
                    {
                        "employee_id": r.get("EMPLOYEEID"),
                        "date": d,
                        "kind": "shift",
                        "shift_id": r.get("SHIFTID"),
                        "workplace_id": r.get("WORKPLACID"),
                        "leave_type_id": None,
                        "schedule_type": int(r.get("TYPE") or 0),
                    }
                )

        # Expand unmaterialised cycle duties (5CYASS); 5MASHI days win
        replaced_by_spshi = {
            (r.get("EMPLOYEEID"), r.get("DATE"))
            for r in self._read("SPSHI")
            if int(r.get("SHIFTID") or 0)
        }
        von = date(year, month, 1)
        bis = self._last_of_month(year, month)
        for eid, recs in self._cycle_shifts_by_employee(von, bis).items():
            for r in recs:
                if (eid, r["DATE"]) in replaced_by_spshi:
                    continue
                entries.append(
                    {
                        "employee_id": eid,
                        "date": r["DATE"],
                        "kind": "shift",
                        "source": "cycle",
                        "shift_id": r.get("SHIFTID"),
                        "workplace_id": r.get("WORKPLACID"),
                        "leave_type_id": None,
                        # Zyklusdienste sind Ist-Plan (geplante tatsächliche Dienste)
                        "schedule_type": 0,
                    }
                )

        # Get special shifts (SPSHI)
        for r in self._read("SPSHI"):
            d = r.get("DATE", "")
            if d and d.startswith(prefix):
                entries.append(
                    {
                        "employee_id": r.get("EMPLOYEEID"),
                        "date": d,
                        "kind": "special_shift",
                        "shift_id": r.get("SHIFTID"),
                        "workplace_id": r.get("WORKPLACID"),
                        "leave_type_id": None,
                        # 5SPSHI.TYPE kodiert NICHT Soll/Ist, sondern (Spec D-53/
                        # 3.4.4, UNSICHER) echten Sonderdienst vs. Arbeitszeit-
                        # abweichung → als spshi_type separat, nicht plan-gefiltert.
                        "spshi_type": int(r.get("TYPE") or 0),
                        "custom_name": r.get("NAME", ""),
                        "custom_short": r.get("SHORTNAME", ""),
                        "color_bk": bgr_to_hex(r.get("COLORBK", 16777215))
                        if r.get("COLORBK")
                        else None,
                        "color_text": bgr_to_hex(r.get("COLORTEXT", 0))
                        if r.get("COLORTEXT")
                        else None,
                    }
                )

        # Get absences (ABSEN)
        for r in self._read("ABSEN"):
            d = r.get("DATE", "")
            if d and d.startswith(prefix):
                # Teiltags-Abwesenheit (Spec 3.5.2/D-54): INTERVAL 0=ganz, 1=vorm.,
                # 2=nachm., 3=stundenweise mit START/END (Minuten ab Mitternacht).
                # Mitgeliefert (A10), damit das Raster Teiltage erkennt und Undo/Move
                # die Granularität erhalten kann (sonst kommt sie als ganztägig zurück).
                interval = int(r.get("INTERVAL") or 0)
                entries.append(
                    {
                        "employee_id": r.get("EMPLOYEEID"),
                        "date": d,
                        "kind": "absence",
                        "shift_id": None,
                        "workplace_id": None,
                        "leave_type_id": r.get("LEAVETYPID"),
                        "interval": interval,
                        "start_time": int(r.get("START") or 0) if interval == 3 else 0,
                        "end_time": int(r.get("END") or 0) if interval == 3 else 0,
                    }
                )

        # Mit Schicht-/Abwesenheitsart-/Arbeitsplatzdaten anreichern
        shifts_map = {s["ID"]: s for s in self.get_shifts(include_hidden=True)}
        lt_map = {lt["ID"]: lt for lt in self.get_leave_types(include_hidden=True)}
        wp_map = {w["ID"]: w for w in self.get_workplaces(include_hidden=True)}

        for e in entries:
            wp_id = e.get("workplace_id")
            e["workplace_name"] = (
                wp_map[wp_id].get("NAME", "") if wp_id and wp_id in wp_map else ""
            )
            if e["shift_id"] and e["shift_id"] in shifts_map:
                s = shifts_map[e["shift_id"]]
                e["display_name"] = s.get("SHORTNAME", s.get("NAME", ""))
                e["color_bk"] = e.get("color_bk") or bgr_to_hex(
                    s.get("COLORBK", 16777215)
                )
                e["color_text"] = e.get("color_text") or bgr_to_hex(
                    s.get("COLORTEXT", 0)
                )
                e["shift_name"] = s.get("NAME", "")
            elif e["leave_type_id"] and e["leave_type_id"] in lt_map:
                lt = lt_map[e["leave_type_id"]]
                e["display_name"] = lt.get("SHORTNAME", lt.get("NAME", ""))
                e["color_bk"] = bgr_to_hex(lt.get("COLORBK", 16777215))
                e["color_text"] = bgr_to_hex(lt.get("COLORBAR", 0))
                e["leave_name"] = lt.get("NAME", "")
            else:
                e["display_name"] = e.get("custom_short", "")
                e["color_bk"] = e.get("color_bk", "#FFFFFF")
                e["color_text"] = e.get("color_text", "#000000")

        # Soll-/Istplan-Filter (Spec 4.12): nur reguläre Dienste (kind=="shift",
        # inkl. Zyklus) tragen schedule_type (0/1). Sonderdienste und Abwesen-
        # heiten sind planart-neutral und bleiben in jeder Sicht sichtbar.
        if plan in ("ist", "soll"):
            want_soll = plan == "soll"
            entries = [
                e
                for e in entries
                if e.get("kind") != "shift"
                or (int(e.get("schedule_type") or 0) == 1) == want_soll
            ]

        # Filter by group
        if group_id is not None:
            member_ids = set(self.get_group_members(group_id))
            entries = [e for e in entries if e["employee_id"] in member_ids]

        return entries

    # ── Absence visibility / anonymisation (Spec 9.5.2, 9.2 Nr. 3, D-67) ──
    def _anon_display(self) -> dict:
        """Ersatzdarstellung anonymisierter Abwesenheiten aus 5USETT (ANOA*)."""
        u = self.get_usett()
        return {
            "name": u.get("ANOANAME") or "Abwesend",
            "short": u.get("ANOASHORT") or "X",
            "color_bk": bgr_to_hex(u.get("ANOACRBK", 16777215)),
            "color_text": bgr_to_hex(u.get("ANOACRTXT", 0)),
            "bold": bool(u.get("ANOABOLD")),
        }

    def apply_absence_visibility(self, data: Any, mode: int) -> Any:
        """Wende die SHOWABS-Sichtbarkeit auf eine beliebig verschachtelte
        Plan-Struktur an (Listen/Dicts mit Blatt-Einträgen ``{"kind": ...}``).

        ``mode`` (5USER.SHOWABS, dreiwertig, Spec 9.5.2 Nr. 2.1):
          0 = vollständig (unverändert),
          1 = anonymisiert (Abwesenheits-Blätter erhalten die 5USETT-ANOA*-
              Ersatzdarstellung, die echte Abwesenheitsart wird entfernt),
          2 = gar nicht (Abwesenheits-Blätter werden entfernt → leeres Feld).
        """
        if mode == 0 or not data:
            return data
        anon = self._anon_display() if mode == 1 else None
        return self._transform_absence(data, mode, anon)

    _DROP = object()

    def _transform_absence(self, node: Any, mode: int, anon: dict | None) -> Any:
        if isinstance(node, list):
            out = []
            for item in node:
                t = self._transform_absence(item, mode, anon)
                if t is self._DROP:
                    continue
                out.append(t)
            return out
        if isinstance(node, dict):
            if node.get("kind") == "absence":
                if mode == 2:
                    return self._DROP
                return {
                    **node,
                    "display_name": anon["short"],
                    "leave_name": anon["name"],
                    "shift_name": "",
                    "color_bk": anon["color_bk"],
                    "color_text": anon["color_text"],
                    "bold": anon["bold"],
                    "leave_type_id": None,
                    "anonymized": True,
                }
            out = {}
            for k, v in node.items():
                t = self._transform_absence(v, mode, anon)
                out[k] = None if t is self._DROP else t
            return out
        return node

    def anonymize_absence_rows(self, rows: list[dict], mode: int) -> list[dict]:
        """Wende die SHOWABS-Sichtbarkeit auf eine flache Abwesenheits-Liste an
        (``get_absences_list``-Form: Dicts mit ``leave_type_id`` /
        ``leave_type_name`` / ``leave_type_short``). Pendant zu
        :meth:`apply_absence_visibility` für Plan-Strukturen — damit die
        Abwesenheits-Liste dieselbe Sichtbarkeit hat wie das Dienstplan-Gitter
        (Spec 9.5.2 Nr. 2.1):

          0 = vollständig (unverändert),
          1 = anonymisiert (Art durch 5USETT-ANOA*-Ersatz ersetzt, ``leave_type_id``
              entfernt, damit die echte Art nicht rekonstruierbar ist),
          2 = gar nicht (Abwesenheiten ausgeblendet → leere Liste).
        """
        if mode == 0 or not rows:
            return rows
        if mode == 2:
            return []
        anon = self._anon_display()
        return [
            {
                **r,
                "leave_type_id": None,
                "leave_type_name": anon["name"],
                "leave_type_short": anon["short"],
            }
            for r in rows
        ]

    # ── Users ──────────────────────────────────────────────────
    # 5GRACC/5EMACC.RIGHTS nutzt denselben Zugriffsstufen-Enum wie 5USER.RIGHTS
    # (Original ``SP5Data.dll`` FUN_100435a0/FUN_100436e0): 0 = Schreiben (Stift),
    # 1 = nur Lesen (Brille), 2 = kein Zugriff (Verbotsschild); kleiner = mehr Recht.
    _GRANT_RIGHT_WRITE = 0

    @staticmethod
    def _rights_is_readonly(r: dict) -> bool:
        """True wenn 5USER.RIGHTS den reinen Nur-Lesemodus (1/3) kodiert.

        5USER.RIGHTS ist KEIN Boolean und KEIN Rang, sondern ein Modus-Enum
        (Original ``SP5Data.dll`` FUN_10043890 / FUN_100435a0 / FUN_100436e0,
        Getter FUN_10042a60 liest 2-Byte int @ ``user+0x8e``):
          0 = Lese- und Schreibrechte für alle Daten        → schreibberechtigt
          1 = Nur-Leserechte für alle Daten                 → read-only
          2 = Differenzierte Rechte (pro Gruppe/MA)          → siehe 5GRACC/5EMACC
          3 = gesperrt / kein Zugriff                        → read-only
        Der differenzierte Modus (2) ist hier bewusst NICHT read-only — ob er
        effektiv Schreibrecht hat, hängt von den Grants ab (siehe
        :meth:`_is_effectively_readonly`).
        """
        return int(r.get("RIGHTS") or 0) in (1, 3)

    def _grants_for(self, user_id: int) -> list[dict]:
        """Kombinierte 5GRACC-/5EMACC-Grants eines Users. Leer, wenn die
        (optionalen) Grant-Tabellen fehlen — die Existenzprüfung vermeidet ein
        irreführendes Fehler-Log für legitim abwesende Tabellen."""
        if not user_id:
            return []
        grants: list[dict] = []
        if os.path.exists(self._table("GRACC")):
            grants += self.get_group_access(user_id)
        if os.path.exists(self._table("EMACC")):
            grants += self.get_employee_access(user_id)
        return grants

    def _has_write_grant(self, user_id: int) -> bool:
        """True wenn der User mindestens einen 5GRACC-/5EMACC-Grant mit
        Schreibstufe (RIGHTS==0) trägt — Basis der differenzierten Rolle
        (5USER.RIGHTS==2). Konservativ: nur ein beweisbarer Schreib-Grant zählt."""
        return any(
            int(g.get("rights") or 0) == self._GRANT_RIGHT_WRITE
            for g in self._grants_for(user_id)
        )

    def _is_effectively_readonly(self, r: dict) -> bool:
        """True wenn der User effektiv KEIN Schreibrecht hat.

        RIGHTS 1/3 = Nur-Lesen. RIGHTS 2 (differenziert) zählt nur dann als
        schreibberechtigt, wenn mindestens ein 5GRACC-/5EMACC-Grant Schreibstufe
        trägt (Original FUN_10043890: RIGHTS==2 ⇒ schreibbar nur bei passendem
        Grant); ohne Schreib-Grant ist er effektiv Nur-Leser.
        """
        if self._rights_is_readonly(r):
            return True
        if int(r.get("RIGHTS") or 0) == 2:
            return not self._has_write_grant(int(r.get("ID") or 0))
        return False

    def _role_from_record(self, r: dict) -> str:
        """Effektive App-Rolle eines 5USER-Satzes.

        ADMIN (orthogonales Flag, schaltet nur die Benutzerverwaltung frei) ⇒
        „Admin". Sonst entscheidet die effektive Schreibfähigkeit: kein
        Schreibrecht ⇒ „Leser", sonst „Planer". Die frühere Logik kollabierte
        RIGHTS binär (0/2 → Planer) und übersah, dass RIGHTS==2 ohne Schreib-Grant
        effektiv Nur-Lesen ist → reale Leser wurden als Planer geführt
        (Rechte-Eskalation).
        """
        if r.get("ADMIN"):
            return "Admin"
        role = "Leser" if self._is_effectively_readonly(r) else "Planer"
        if _db_logger.isEnabledFor(logging.DEBUG):
            rights = int(r.get("RIGHTS") or 0)
            grants = self._grants_for(int(r.get("ID") or 0)) if rights == 2 else []
            write_grants = sum(
                1 for g in grants
                if int(g.get("rights") or 0) == self._GRANT_RIGHT_WRITE
            )
            # Privacy-safe: nur ID/Enum/Zähler, KEIN Name/Passwort/Digest.
            _db_logger.debug(
                "role-resolve user_id=%s RIGHTS=%s ADMIN=%s "
                "write_grants=%d read_grants=%d -> role=%s",
                r.get("ID"), rights, bool(r.get("ADMIN")),
                write_grants, len(grants) - write_grants, role,
            )
        return role

    def _hash_password_md5(self, password: str) -> bytes:
        """Liefert den 16-Byte-MD5-Digest des Passworts (Legacy, wie 5USER.DIGEST)."""
        import hashlib

        return hashlib.md5(password.encode("utf-8")).digest()

    # Das Original-Schichtplaner5 (Delphi/Windows) speichert jedes Passwort als
    # ungesalzenen 16-Byte-MD5-Digest in 5USER.DIGEST. Seine Login-Routine
    # (SP5Data.dll FUN_100431a0) prüft ein getipptes Passwort gegen den Digest
    # unter ZWEI Kodierungen: zuerst Delphi-WideString (UTF-16-LE), bei
    # Nichtübereinstimmung die System-ANSI-Codepage (CP1252 auf deutschem
    # Windows, via WideCharToMultiByte/AtlGetThreadACP). Die Passwort-setzen-
    # Routine schreibt den UTF-16-LE-Digest, Vor-Unicode-Builds (Delphi <2009)
    # schrieben den ANSI-Digest — eine reale DB mischt also beide (Beispiel-DB:
    # Bartel "a" = MD5(b"a") ANSI vs. Leitung "a" = MD5(b"a\x00") UTF-16-LE).
    # Wir bilden diese Doppel-Kodierungs-Prüfung nach und akzeptieren
    # zusätzlich UTF-8, weil unser eigener Writer (_hash_password_md5) UTF-8
    # ausgibt (== CP1252 für ASCII). Ein erfolgreicher Legacy-Login migriert
    # auf bcrypt — das Ganze zählt also nur beim ersten Login eines noch nicht
    # migrierten Kontos.
    _LEGACY_MD5_ENCODINGS = ("utf-8", "cp1252", "utf-16-le")

    def _legacy_md5_encoding(self, password: str, digest_bytes: bytes) -> str | None:
        """Liefert die Kodierung, unter der MD5(password) dem gespeicherten
        16-Byte-Digest entspricht, sonst None. Siehe ``_LEGACY_MD5_ENCODINGS``."""
        import hashlib

        if len(digest_bytes) != 16:
            return None
        for enc in self._LEGACY_MD5_ENCODINGS:
            try:
                candidate = password.encode(enc)
            except (UnicodeEncodeError, LookupError):
                continue
            if hashlib.md5(candidate).digest() == digest_bytes:
                return enc
        return None

    # ── Bcrypt sidecar store ──────────────────────────────────
    # Bcrypt-Hashes haben 60 Zeichen und passen nicht ins 16-Byte-DIGEST-Feld
    # von 5USER.DBF — sie liegen daher in einer JSON-Sidecar-Datei neben der DBF.

    def _bcrypt_path(self) -> str:
        return os.path.join(self.db_path, "5USER_BCRYPT.json")

    # ── TOTP-2FA-Sidecar ──────────────────────────────────────

    def _totp_path(self) -> str:
        return os.path.join(self.db_path, "5USER_TOTP.json")

    def _load_totp_data(self) -> dict:
        """Lädt {user_id_str: {secret, backup_codes, enabled}} aus dem Sidecar."""
        path = self._totp_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_totp_data(self, data: dict) -> None:
        path = self._totp_path()
        _atomic_write_json(path, data, ensure_ascii=False)

    def totp_get_status(self, user_id: int) -> bool:
        """Liefert True, wenn TOTP-2FA für den Benutzer aktiviert ist."""
        data = self._load_totp_data()
        entry = data.get(str(user_id), {})
        return bool(entry.get("enabled"))

    def totp_generate_secret(self, user_id: int) -> str:
        """Erzeugt und speichert ein schwebendes TOTP-Secret (noch nicht aktiviert)."""
        import pyotp
        secret = pyotp.random_base32()
        data = self._load_totp_data()
        data[str(user_id)] = {
            "secret": secret,
            "enabled": False,
            "backup_codes": [],
        }
        self._save_totp_data(data)
        return secret

    def totp_enable(self, user_id: int, code: str) -> list[str] | None:
        """Prüft den Code gegen das schwebende Secret und aktiviert 2FA. Liefert Backup-Codes oder None."""
        import hashlib
        import secrets

        import pyotp
        data = self._load_totp_data()
        entry = data.get(str(user_id))
        if not entry or not entry.get("secret"):
            return None
        totp = pyotp.TOTP(entry["secret"])
        if not totp.verify(code, valid_window=1):
            return None
        # Generate backup codes
        backup_codes = [secrets.token_hex(4).upper() for _ in range(8)]
        backup_hashes = [hashlib.sha256(c.encode()).hexdigest() for c in backup_codes]
        entry["enabled"] = True
        entry["backup_codes"] = backup_hashes
        data[str(user_id)] = entry
        self._save_totp_data(data)
        return backup_codes

    def totp_verify(self, user_id: int, code: str) -> bool:
        """Prüft einen TOTP- oder Backup-Code. Liefert True bei Gültigkeit."""
        import hashlib

        import pyotp
        data = self._load_totp_data()
        entry = data.get(str(user_id))
        if not entry or not entry.get("enabled") or not entry.get("secret"):
            return False
        # Try TOTP code first
        totp = pyotp.TOTP(entry["secret"])
        if totp.verify(code, valid_window=1):
            return True
        # Try backup code
        code_hash = hashlib.sha256(code.strip().upper().encode()).hexdigest()
        if code_hash in entry.get("backup_codes", []):
            entry["backup_codes"].remove(code_hash)
            data[str(user_id)] = entry
            self._save_totp_data(data)
            return True
        return False

    def totp_disable(self, user_id: int) -> bool:
        """Deaktiviert 2FA für einen Benutzer."""
        data = self._load_totp_data()
        uid = str(user_id)
        if uid in data:
            del data[uid]
            self._save_totp_data(data)
            return True
        return False

    def _load_bcrypt_hashes(self) -> dict[str, str]:
        """Lädt {user_id_str: bcrypt_hash} aus der Sidecar-Datei."""
        path = self._bcrypt_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_bcrypt_hash(self, user_id: int, bcrypt_hash: str) -> None:
        """Persistiert den bcrypt-Hash eines Benutzers (Merge in die Sidecar-Datei)."""
        hashes = self._load_bcrypt_hashes()
        hashes[str(user_id)] = bcrypt_hash
        path = self._bcrypt_path()
        _atomic_write_json(path, hashes, ensure_ascii=False)

    def _hash_password(self, password: str) -> bytes:
        """Liefert den 16-Byte-MD5-Digest für die DBF-Ablage (Legacy-Kompatibilität).

        Neuer Code ruft stattdessen _hash_password_bcrypt.
        """
        return self._hash_password_md5(password)

    def _hash_password_bcrypt(self, password: str) -> str:
        """Liefert den bcrypt-Hash-String für das Passwort."""
        import bcrypt

        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    def get_users(self) -> list[dict]:
        rows = self._read("USER")
        result = []
        for r in rows:
            if r.get("HIDE"):
                continue
            result.append(
                {
                    "ID": r.get("ID"),
                    "POSITION": r.get("POSITION", 0),
                    "NAME": r.get("NAME", ""),
                    "DESCRIP": r.get("DESCRIP", ""),
                    "ADMIN": bool(r.get("ADMIN")),
                    "RIGHTS": r.get("RIGHTS", 0),
                    "HIDE": bool(r.get("HIDE")),
                    "WDUTIES": bool(r.get("WDUTIES")),
                    "WABSENCES": bool(r.get("WABSENCES")),
                    "WOVERTIMES": bool(r.get("WOVERTIMES")),
                    "BACKUP": bool(r.get("BACKUP")),
                    # SHOWABS dreiwertig (Spec 9.5.2): 0=vollständig,
                    # 1=anonymisiert, 2=gar nicht (Admin ⇒ immer 0)
                    "SHOWABS": 0
                    if bool(r.get("ADMIN"))
                    else int(r.get("SHOWABS") or 0),
                    "role": self._role_from_record(r),
                }
            )
        result.sort(key=lambda x: x.get("POSITION", 999))
        return result

    def create_user(self, data: dict) -> dict:
        """Create a new SP5 user in 5USER.DBF."""
        filepath = self._table("USER")
        fields = get_table_fields(filepath)
        new_id = self._next_id("USER")
        rows = self._read("USER")
        # Uniqueness check: NAME (username) must be unique among active users
        name_lower = (data.get("NAME") or "").strip().lower()
        for row in rows:
            if row.get("HIDE") or row.get("HIDE") == 1:
                continue
            if (row.get("NAME") or "").strip().lower() == name_lower:
                raise ValueError(f"DUPLICATE:USERNAME:{data.get('NAME')}")
        max_pos = max((r.get("POSITION", 0) or 0 for r in rows), default=0) + 1

        role = data.get("role", "Leser")
        is_admin = 1 if role == "Admin" else 0
        # 5USER.RIGHTS ist der Berechtigungs-MODUS (Original): 0 = volle Lese-/
        # Schreibrechte (Planer), 1 = Nur-Leserechte (Leser). Admin nutzt ADMIN=1
        # (RIGHTS bleibt 0). Früher wurde hier invertiert RIGHTS=1 für „Planer"
        # geschrieben — inkonsistent zur Original-Bedeutung (1 = read-only).
        rights = 1 if role == "Leser" else 0

        password = data.get("PASSWORD", "")
        digest = self._hash_password_md5(password) if password else b"\x00" * 16

        # Default permissions based on role
        write_perms = 1 if role in ("Admin", "Planer") else 0
        record = {
            "ID": new_id,
            "POSITION": max_pos,
            "NAME": data.get("NAME", ""),
            "DESCRIP": data.get("DESCRIP", ""),
            "ADMIN": is_admin,
            "DIGEST": digest,
            "RIGHTS": rights,
            "CATEGORY": " ".join(["1"] * 20),  # ASCII 20-slot mask (oracle format)
            "ADDEMPL": 0,
            "WDUTIES": write_perms,
            "WABSENCES": write_perms,
            "WOVERTIMES": write_perms,
            "WNOTES": write_perms,
            "WDEVIATION": write_perms,
            "WCYCLEASS": write_perms,
            "WSWAPONLY": 0,
            "WPAST": write_perms,
            "WACCEMWND": 1,
            "WACCGRWND": 1,
            "SHOWABS": 0,
            "SHOWNOTES": 1,
            "SHOWSTATS": 1,
            "RACCEMWND": 1,
            "RACCGRWND": 1,
            "BACKUP": is_admin,
            "HIDEBARIN": 0,
            "HIDEBARNO": 0,
            "ACCVOWND": 1,
            "ACCADMWND": is_admin,
            "MINITABLE": 0,
            "REPORT": " ".join(["1"] * 20),  # ASCII 20-slot mask (oracle format)
            "HIDE": 0,
            "RESERVED": "",
        }
        # Granulare Schreibrechte explizit übersteuern (Spec 9.6): überschreibt
        # die rollenbasierten Defaults, falls im data-Dict angegeben.
        for field in self._WRITE_PERMISSION_FIELDS:
            if field in data:
                record[field] = 1 if data[field] else 0
        append_record(filepath, fields, record, autoid_field="ID")
        # Store bcrypt hash in sidecar file
        if password:
            bcrypt_hash = self._hash_password_bcrypt(password)
            self._save_bcrypt_hash(record["ID"], bcrypt_hash)
        return {
            "ID": record["ID"],
            "NAME": record["NAME"],
            "DESCRIP": record["DESCRIP"],
            "ADMIN": bool(is_admin),
            "RIGHTS": rights,
            "HIDE": False,
            "role": role,
        }

    def update_user(self, user_id: int, data: dict) -> dict:
        """Update an existing SP5 user."""
        filepath = self._table("USER")
        fields = get_table_fields(filepath)
        raw_idx, existing = self._find_record("USER", user_id)
        if raw_idx is None:
            raise ValueError(f"User {user_id} not found")

        update_data: dict[str, Any] = {}

        if "NAME" in data:
            update_data["NAME"] = data["NAME"]
        if "DESCRIP" in data:
            update_data["DESCRIP"] = data["DESCRIP"]
        if "role" in data:
            role = data["role"]
            update_data["ADMIN"] = 1 if role == "Admin" else 0
            # RIGHTS = Modus: 1 = Nur-Leserechte (Leser), 0 = voll (Planer/Admin).
            update_data["RIGHTS"] = 1 if role == "Leser" else 0
            write_perms = 1 if role in ("Admin", "Planer") else 0
            update_data["WDUTIES"] = write_perms
            update_data["WABSENCES"] = write_perms
            update_data["WOVERTIMES"] = write_perms
            update_data["WNOTES"] = write_perms
            update_data["WDEVIATION"] = write_perms
            update_data["WCYCLEASS"] = write_perms
            update_data["WPAST"] = write_perms
            update_data["BACKUP"] = 1 if role == "Admin" else 0
            update_data["ACCADMWND"] = 1 if role == "Admin" else 0
        if "PASSWORD" in data and data["PASSWORD"]:
            update_data["DIGEST"] = self._hash_password_md5(data["PASSWORD"])
            # Also store bcrypt hash in sidecar
            bcrypt_hash = self._hash_password_bcrypt(data["PASSWORD"])
            self._save_bcrypt_hash(user_id, bcrypt_hash)
        if "SHOWABS" in data:
            # dreiwertig (Spec 9.5.2 Nr. 2.1): 0=vollständig, 1=anonymisiert,
            # 2=gar nicht — auf gültigen Bereich klemmen
            try:
                mode = int(data["SHOWABS"])
            except (TypeError, ValueError):
                mode = 0
            update_data["SHOWABS"] = mode if mode in (0, 1, 2) else 0
        # Granulare Schreibrechte explizit übersteuern (Spec 9.6): überschreibt
        # die oben aus der Rolle abgeleiteten Defaults, falls angegeben. Steht
        # bewusst NACH dem role-Block, damit explizite Flags Vorrang haben.
        for field in self._WRITE_PERMISSION_FIELDS:
            if field in data:
                update_data[field] = 1 if data[field] else 0

        update_record(filepath, fields, raw_idx, update_data)

        # Return updated user
        role = data.get("role")
        if role is None and existing:
            role = self._role_from_record(existing)
        return {
            "ID": user_id,
            "NAME": data.get("NAME", existing.get("NAME", "") if existing else ""),
            "DESCRIP": data.get(
                "DESCRIP", existing.get("DESCRIP", "") if existing else ""
            ),
            "role": role,
        }

    def delete_user(self, user_id: int) -> int:
        """Soft-delete (hide) a user. Returns 1 if found, 0 otherwise."""
        filepath = self._table("USER")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("USER", user_id)
        if raw_idx is None:
            return 0
        update_record(filepath, fields, raw_idx, {"HIDE": 1})
        return 1

    def check_user_permission(self, user_id: int, action: str) -> bool:
        """
        Check if a user has permission for a given action.
        Actions: 'admin', 'write_duties', 'write_absences', 'write_overtimes',
                 'write_notes', 'backup', 'read_employees', 'read_groups'
        """
        rows = self._read("USER")
        user = next(
            (r for r in rows if r.get("ID") == user_id and not r.get("HIDE")), None
        )
        if user is None:
            return False
        perm_map = {
            "admin": "ADMIN",
            "write_duties": "WDUTIES",
            "write_absences": "WABSENCES",
            "write_overtimes": "WOVERTIMES",
            "write_notes": "WNOTES",
            "backup": "BACKUP",
            "read_employees": "WACCEMWND",
            "read_groups": "WACCGRWND",
        }
        field = perm_map.get(action)
        if field:
            return bool(user.get(field))
        # For unknown actions, only allow admin
        return bool(user.get("ADMIN"))

    def _effective_write_perm(self, r: dict, field: str, is_admin: bool) -> bool:
        """Effektives Schreibrecht *field* unter Berücksichtigung von 5USER.RIGHTS.

        Admin ⇒ immer True. Effektiv im Nur-Lesemodus (RIGHTS 1/3, oder RIGHTS 2
        ohne Schreib-Grant, siehe :meth:`_is_effectively_readonly`) ⇒ immer False —
        das Original verweigert dort jeden Schreibzugriff, auch wenn die
        gespeicherten W*-Flags (Altbestand) noch gesetzt sind. Sonst (RIGHTS 0 voll
        / 2 mit Schreib-Grant) gilt das gespeicherte Flag (der Gruppen-/MA-Scope
        wird separat über 5GRACC/5EMACC geprüft).
        """
        if is_admin:
            return True
        if self._is_effectively_readonly(r):
            return False
        return bool(r.get(field))

    def _build_user_dict(self, r: dict) -> dict:
        """Build a sanitized user dict from a raw DBF record (no hash fields)."""
        role = self._role_from_record(r)
        is_admin = role == "Admin"
        return {
            "ID": r.get("ID"),
            "NAME": r.get("NAME", ""),
            "DESCRIP": r.get("DESCRIP", ""),
            "ADMIN": bool(r.get("ADMIN")),
            "RIGHTS": r.get("RIGHTS", 0),
            "role": role,
            "WDUTIES": self._effective_write_perm(r, "WDUTIES", is_admin),
            "WABSENCES": self._effective_write_perm(r, "WABSENCES", is_admin),
            "WOVERTIMES": self._effective_write_perm(r, "WOVERTIMES", is_admin),
            "WNOTES": self._effective_write_perm(r, "WNOTES", is_admin),
            "WDEVIATION": self._effective_write_perm(r, "WDEVIATION", is_admin),
            "WCYCLEASS": self._effective_write_perm(r, "WCYCLEASS", is_admin),
            "WSWAPONLY": self._effective_write_perm(r, "WSWAPONLY", is_admin),
            "WPAST": self._effective_write_perm(r, "WPAST", is_admin),
            "ADDEMPL": self._effective_write_perm(r, "ADDEMPL", is_admin),
            "WACCEMWND": bool(r.get("WACCEMWND")) if not is_admin else True,
            "WACCGRWND": bool(r.get("WACCGRWND")) if not is_admin else True,
            "BACKUP": self._effective_write_perm(r, "BACKUP", is_admin),
            # SHOWABS ist dreiwertig (N 5, Spec 9.5.2 Nr. 2.1): 0=vollständig,
            # 1=anonymisiert, 2=gar nicht. ``SHOWABS`` = „darf Abwesenheiten
            # überhaupt sehen" (mode != 2), ``SHOWABS_MODE`` = der Rohwert.
            # Admin sieht immer vollständig.
            "SHOWABS": True if is_admin else (int(r.get("SHOWABS") or 0) != 2),
            "SHOWABS_MODE": 0 if is_admin else int(r.get("SHOWABS") or 0),
            "SHOWNOTES": bool(r.get("SHOWNOTES")) if not is_admin else True,
            "SHOWSTATS": bool(r.get("SHOWSTATS")) if not is_admin else True,
            "ACCADMWND": is_admin,
        }

    # Granulare 5USER-Schreib-/Anzeige-Flags (Spec 9.6) → permissions-Schlüssel
    _USER_PERMISSION_FIELDS = {
        "wduties": "WDUTIES",
        "wabsences": "WABSENCES",
        "wovertimes": "WOVERTIMES",
        "wnotes": "WNOTES",
        "wdeviation": "WDEVIATION",
        "wcycleass": "WCYCLEASS",
        "wswaponly": "WSWAPONLY",
        "wpast": "WPAST",
        "addempl": "ADDEMPL",
        "showabs": "SHOWABS",  # dreiwertig (Spec 9.5.2) — hier Wahrheitswert
        "shownotes": "SHOWNOTES",
        "showstats": "SHOWSTATS",
        "backup": "BACKUP",
    }

    # Granulare 5USER-Schreibrechte (Spec 9.6), die in create_user/update_user
    # explizit übersteuerbar sind und die rollenbasierten Defaults überschreiben.
    # SHOW*/SHOWABS sind Anzeige-/Sichtbarkeitsflags und hier bewusst NICHT
    # enthalten (SHOWABS wird separat dreiwertig behandelt).
    _WRITE_PERMISSION_FIELDS = (
        "WDUTIES", "WABSENCES", "WOVERTIMES", "WNOTES", "WDEVIATION",
        "WCYCLEASS", "WSWAPONLY", "WPAST", "ADDEMPL", "BACKUP",
    )

    def get_user_permissions(self, user_id: int) -> dict | None:
        """Granulare 5USER-Flags eines Benutzers (Spec 9.6) als
        ``{permission: bool}``; Admin ⇒ alles True. None, wenn unbekannt."""
        rows = self._read("USER")
        r = next(
            (x for x in rows if x.get("ID") == user_id and not x.get("HIDE")), None
        )
        if r is None:
            return None
        if r.get("ADMIN"):
            return dict.fromkeys(self._USER_PERMISSION_FIELDS, True)
        # Effektiver Nur-Lesemodus (RIGHTS 1/3, oder RIGHTS 2 ohne Schreib-Grant)
        # sperrt jedes Schreibrecht, auch bei gesetzten Altbestands-Flags;
        # Anzeige-/Sichtbarkeitsflags (SHOW*) bleiben erhalten.
        readonly = self._is_effectively_readonly(r)
        perms = {
            key: (
                False
                if readonly and field in self._WRITE_PERMISSION_FIELDS
                else bool(r.get(field))
            )
            for key, field in self._USER_PERMISSION_FIELDS.items()
        }
        # SHOWABS dreiwertig (Spec 9.5.2, D-67): „darf Abwesenheiten sehen"
        # = mode != 2 (0=vollständig, 1=anonymisiert sind beide sichtbar).
        perms["showabs"] = int(r.get("SHOWABS") or 0) != 2
        return perms

    def get_user_identity(self, user_id: int) -> dict | None:
        """Identitäts-/Rechte-Dict eines Benutzers per ID — exakt das Shape, das
        ein erfolgreicher Login liefert (``_build_user_dict``), aber OHNE das
        Passwort/den Digest zu berühren. Für die Admin-Impersonation („Als
        Benutzer ansehen", P-B): die API übernimmt dieses Dict als
        Autorisierungs-Principal des Ziel-Users, sodass dessen Rolle/Rechte/
        Sichtbarkeit gelten. Versteckte Benutzer (HIDE) und unbekannte IDs ⇒ None.
        Bewusst kein Pfad durch ``verify_user_password``, damit die Login-/
        Digest-Prüfung (Multi-Encoding, 1.14.1) unberührt bleibt."""
        r = next(
            (x for x in self._read("USER") if x.get("ID") == user_id and not x.get("HIDE")),
            None,
        )
        return self._build_user_dict(r) if r is not None else None

    def verify_user_password(self, name: str, password: str) -> dict | None:
        """Verify username+password, return user dict (without hash) or None.

        Strategy (backward-compatible migration):
        1. Check bcrypt hash in sidecar file first (fast path for migrated users)
        2. Fall back to legacy MD5 digest in DBF
        3. If MD5 matches → auto-migrate to bcrypt on this login
        """
        import bcrypt as _bcrypt

        rows = self._read("USER")
        bcrypt_hashes = self._load_bcrypt_hashes()

        for r in rows:
            if r.get("HIDE"):
                continue
            if r.get("NAME", "").strip().lower() != name.strip().lower():
                continue

            user_id = r.get("ID")
            user_id_str = str(user_id)

            # ── 1. Try bcrypt (migrated users) ──
            stored_bcrypt = bcrypt_hashes.get(user_id_str)
            if stored_bcrypt:
                try:
                    if _bcrypt.checkpw(password.encode("utf-8"), stored_bcrypt.encode("utf-8")):
                        return self._build_user_dict(r)
                except Exception:
                    pass  # Corrupted hash — fall through to MD5

            # ── 2. Fall back to legacy MD5 ──
            digest = r.get("DIGEST", "")
            if isinstance(digest, bytes):
                digest_bytes = digest
            elif isinstance(digest, str):
                digest_bytes = digest.encode("latin-1")
            else:
                continue

            matched_encoding = self._legacy_md5_encoding(password, digest_bytes)
            if matched_encoding is not None:
                # ── 3. Auto-migrate to bcrypt on successful MD5 login ──
                # matched_encoding landet im Betreiber-Log, damit der Login-Pfad
                # einer echten DB (z. B. „via utf-16-le") ohne das Passwort sichtbar ist.
                try:
                    new_hash = self._hash_password_bcrypt(password)
                    self._save_bcrypt_hash(user_id, new_hash)
                    _db_logger.info(
                        "PASSWORD_MIGRATED | user_id=%d user=%s (MD5[%s] → bcrypt)",
                        user_id,
                        r.get("NAME", ""),
                        matched_encoding,
                    )
                except Exception as e:
                    _db_logger.warning("Bcrypt migration failed for user %d: %s", user_id, e)
                return self._build_user_dict(r)

        return None

    def login_diagnostics(self, name: str) -> dict:
        """Datenschutz-sichere Diagnose eines *fehlgeschlagenen* Logins — fasst
        weder Passwort noch Roh-Digest an. Erlaubt dem Betreiber, einen
        Echt-DB-Login-Grenzfall aus den Server-Logs zu erklären. Liefert:
        user_found, hidden, digest_len, digest_is_md5_shape, digest_all_zero,
        digest_is_empty_md5, has_bcrypt, encodings_tried.

        Reading the signature: a 16-byte non-zero MD5 that no encoding matched
        (digest_is_md5_shape=true, digest_all_zero=false, has_bcrypt=false) means
        the typed password is wrong (or, on an old build, used an unsupported
        codepage). digest_all_zero=true = no password set / account disabled.
        digest_is_empty_md5=true = the account's password is blank (logs in with
        an empty password — original SP5 parity, e.g. the sample "Admin").
        """
        import hashlib

        empty_md5 = hashlib.md5(b"").digest()
        try:
            rows = self._read("USER")
            bcrypt_hashes = self._load_bcrypt_hashes()
        except Exception as exc:  # noqa: BLE001 — diagnostics must never raise
            return {"error": type(exc).__name__}
        target_name = name.strip().lower()
        for r in rows:
            if (r.get("NAME", "") or "").strip().lower() != target_name:
                continue
            digest = r.get("DIGEST", "")
            if isinstance(digest, bytes):
                digest_bytes = digest
            elif isinstance(digest, str):
                digest_bytes = digest.encode("latin-1")
            else:
                digest_bytes = b""
            digest_len = len(digest_bytes)
            return {
                "user_found": True,
                "hidden": bool(r.get("HIDE")),
                "digest_len": digest_len,
                "digest_is_md5_shape": digest_len == 16,
                "digest_all_zero": digest_len == 16 and digest_bytes == b"\x00" * 16,
                "digest_is_empty_md5": digest_bytes == empty_md5,
                "has_bcrypt": str(r.get("ID")) in bcrypt_hashes,
                "encodings_tried": list(self._LEGACY_MD5_ENCODINGS),
            }
        return {"user_found": False}

    # ── Cycles ─────────────────────────────────────────────────
    def get_cycles(self) -> list[dict]:
        cycles = self._read("CYCLE")
        entries = self._read("CYENT")
        shifts_map = {s["ID"]: s for s in self.get_shifts(include_hidden=True)}

        for cycle in cycles:
            cyc_entries = [e for e in entries if e.get("CYCLEEID") == cycle.get("ID")]
            cyc_entries.sort(key=lambda x: x.get("INDEX", 0))
            cycle["entries"] = []
            for e in cyc_entries:
                sid = e.get("SHIFTID")
                shift = shifts_map.get(sid) if sid else None
                cycle["entries"].append(
                    {
                        "index": e.get("INDEX", 0),
                        "shift_id": sid,
                        "shift_name": shift.get("NAME", "") if shift else "",
                        "shift_short": shift.get("SHORTNAME", "") if shift else "",
                        "workplace_id": e.get("WORKPLACID"),
                    }
                )
        return cycles

    # ── Shift Cycles (rich) ───────────────────────────────────
    def get_shift_cycles(self) -> list[dict]:
        """Liefert alle Zyklen mit ihrem Wochenplan aus CYCLE + CYENT."""
        cycles = self._read("CYCLE")
        entries = self._read("CYENT")
        shifts_map = {s["ID"]: s for s in self.get_shifts(include_hidden=True)}

        result = []
        for cycle in cycles:
            if cycle.get("HIDE"):
                continue
            cid = cycle.get("ID")
            size = cycle.get("SIZE", 1)  # number of weeks
            unit = cycle.get("UNIT", 1)  # 1 = weeks

            # Flaches Tages-Array über alle Wochen bauen
            # INDEX in CYENT ist der flache Tages-Offset (0 = Mo Woche1, 7 = Mo Woche2, …)
            cyc_entries = {e["INDEX"]: e for e in entries if e.get("CYCLEEID") == cid}

            weeks = []
            for w in range(size):
                week_days = []
                for d in range(7):
                    idx = w * 7 + d
                    entry = cyc_entries.get(idx)
                    if entry:
                        sid = entry.get("SHIFTID", 0)
                        shift = shifts_map.get(sid) if sid else None
                        week_days.append(
                            {
                                "index": idx,
                                "weekday": d,
                                "shift_id": sid or None,
                                "shift_name": shift.get("NAME", "") if shift else "",
                                "shift_short": shift.get("SHORTNAME", "")
                                if shift
                                else "",
                                "color_bk": bgr_to_hex(shift.get("COLORBK", 16777215))
                                if shift
                                else "#FFFFFF",
                                "color_text": bgr_to_hex(shift.get("COLORTEXT", 0))
                                if shift
                                else "#000000",
                                "workplace_id": entry.get("WORKPLACID") or None,
                            }
                        )
                    else:
                        week_days.append(
                            {
                                "index": idx,
                                "weekday": d,
                                "shift_id": None,
                                "shift_name": "",
                                "shift_short": "",
                                "color_bk": "#FFFFFF",
                                "color_text": "#000000",
                                "workplace_id": None,
                            }
                        )
                weeks.append(week_days)

            # Build a simple pattern string like "F/F/F/S/S/–/– | S/S/S/F/F/–/–"
            pattern_parts = []
            for week in weeks:
                part = "/".join(
                    str(d["shift_short"]) if d["shift_short"] else "–" for d in week
                )  # type: ignore[misc]
                pattern_parts.append(part)
            pattern = "  |  ".join(pattern_parts)

            result.append(
                {
                    "ID": cid,
                    "name": cycle.get("NAME", ""),
                    "weeks": size,
                    "unit": unit,
                    "position": cycle.get("POSITION", 0),
                    "pattern": pattern,
                    "schedule": weeks,
                }
            )

        result.sort(key=lambda x: x.get("position", 0))
        return result

    def get_shift_cycle(self, cycle_id: int) -> dict | None:
        """Liefert einen einzelnen Zyklus per ID mit vollem Plan."""
        for c in self.get_shift_cycles():
            if c["ID"] == cycle_id:
                return c
        return None

    # ── Shift Cycle CRUD ──────────────────────────────────────

    def create_shift_cycle(self, name: str, size_weeks: int, unit: int = 1) -> dict:
        """Legt einen neuen Zyklus in 5CYCLE.DBF an. Liefert das neue Zyklus-dict.

        ``unit``: 1 = Wochen (Default, SIZE in Wochen), 0 = Tage (SIZE in
        Tagen — Original-Modus, Wine-belegt).
        """
        filepath = self._table("CYCLE")
        fields = get_table_fields(filepath)
        new_id = self._next_id("CYCLE")
        rows = self._read("CYCLE")
        max_pos = max((r.get("POSITION", 0) or 0 for r in rows), default=0) + 1
        record = {
            "ID": new_id,
            "NAME": name,
            "POSITION": max_pos,
            "SIZE": size_weeks,
            "UNIT": 1 if int(unit) == 1 else 0,
            "HIDE": False,
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return {
            "ID": record["ID"],
            "name": name,
            "weeks": size_weeks,
            "unit": 1 if int(unit) == 1 else 0,
            "position": max_pos,
            "pattern": "",
            "schedule": [],
        }

    def update_shift_cycle(
        self, cycle_id: int, name: str, size_weeks: int, unit: int | None = None
    ) -> dict:
        """Aktualisiert Name, Größe und optional die Einheit eines Zyklus.

        ``unit``: 1 = Wochen (SIZE in Wochen), 0 = Tage (SIZE in Tagen,
        Original-Modus „Einheit: Tage", Wine-belegt); None lässt die Einheit
        unverändert.
        """
        filepath = self._table("CYCLE")
        fields = get_table_fields(filepath)
        raw_idx, existing = self._find_record("CYCLE", cycle_id)
        if raw_idx is None:
            raise ValueError(f"Shift cycle {cycle_id} not found")
        data = {"NAME": name, "SIZE": size_weeks}
        if unit is not None:
            data["UNIT"] = 1 if int(unit) == 1 else 0
        update_record(filepath, fields, raw_idx, data)
        return {"ID": cycle_id, "name": name, "weeks": size_weeks}

    def delete_shift_cycle(self, cycle_id: int) -> int:
        """Löscht einen Zyklus aus 5CYCLE samt seinen Einträgen aus 5CYENT und 5CYASS."""
        # Delete cycle entries first
        self.clear_cycle_entries(cycle_id)
        # Cascade: remove employee assignments that reference this cycle
        cyass_path = self._table("CYASS")
        cyass_fields = get_table_fields(cyass_path)
        cyass_matches = find_all_records(cyass_path, cyass_fields, CYCLEID=cycle_id)
        for idx, _ in reversed(cyass_matches):
            delete_record(cyass_path, cyass_fields, idx)
        # Cascade: remove cycle exceptions referencing this cycle (via CYASS IDs no longer valid)
        # Löschen per CYCLEID, wenn das Feld existiert, sonst Best-Effort
        try:
            cyexc_path = self._table("CYEXC")
            cyexc_fields = get_table_fields(cyexc_path)
            # CycleExceptions reference cycle_assignment_id, not cycle_id directly
            # Nach dem Entfernen der CYASS-Einträge sind die Ausnahmen verwaist; mit aufräumen
            assignment_ids = {rec.get("ID") for _, rec in cyass_matches}
            if assignment_ids:
                all_exc = find_all_records(cyexc_path, cyexc_fields)
                for idx, rec in reversed(all_exc):
                    if rec.get("CYASSID") in assignment_ids:
                        delete_record(cyexc_path, cyexc_fields, idx)
        except Exception:
            pass  # CYEXC table may not exist in all deployments
        # Den Zyklus-Satz löschen
        raw_idx, _ = self._find_record("CYCLE", cycle_id)
        if raw_idx is None:
            return 0
        filepath = self._table("CYCLE")
        fields = get_table_fields(filepath)
        delete_record(filepath, fields, raw_idx)
        return 1

    def set_cycle_entry(
        self, cycle_id: int, index: int, shift_id: int | None
    ) -> None:
        """Legt einen 5CYENT-Eintrag für Zyklus und Tag-Index an oder aktualisiert ihn."""
        filepath = self._table("CYENT")
        fields = get_table_fields(filepath)
        matches = find_all_records(filepath, fields, CYCLEEID=cycle_id, INDEX=index)
        if matches:
            raw_idx, _ = matches[0]
            update_record(filepath, fields, raw_idx, {"SHIFTID": shift_id or 0})
        else:
            new_id = self._next_id("CYENT")
            record = {
                "ID": new_id,
                "CYCLEEID": cycle_id,
                "INDEX": index,
                "SHIFTID": shift_id or 0,
                "WORKPLACID": 0,
                "RESERVED": "",
            }
            append_record(filepath, fields, record, autoid_field="ID")

    def clear_cycle_entries(self, cycle_id: int) -> int:
        """Löscht alle 5CYENT-Einträge des Zyklus. Liefert die Anzahl der gelöschten."""
        filepath = self._table("CYENT")
        fields = get_table_fields(filepath)
        matches = find_all_records(filepath, fields, CYCLEEID=cycle_id)
        count = 0
        for raw_idx, _ in matches:
            delete_record(filepath, fields, raw_idx)
            count += 1
        return count

    # ── Cycle Assignments ─────────────────────────────────────
    def get_cycle_assignments(self) -> list[dict]:
        """Return all cycle assignments from CYASS."""
        rows = self._read("CYASS")
        result = []
        for r in rows:
            result.append(
                {
                    "id": r.get("ID"),
                    "employee_id": r.get("EMPLOYEEID"),
                    "cycle_id": r.get("CYCLEID"),
                    "start": r.get("START", ""),
                    "end": r.get("END", ""),
                    "entrance": r.get("ENTRANCE", ""),
                }
            )
        return result

    def get_cycle_assignment_for_employee(self, employee_id: int) -> dict | None:
        """Liefert die aktive Zyklus-Zuweisung eines Mitarbeiters, falls vorhanden."""
        for a in self.get_cycle_assignments():
            if a["employee_id"] == employee_id:
                return a
        return None

    def assign_cycle(
        self,
        employee_id: int,
        cycle_id: int,
        start_date: str,
        end_date: str | None = None,
    ) -> dict:
        """Assign a cycle to an employee (append to CYASS).

        ``end_date`` (5CYASS.END) befristet die Zuordnung: die Zyklus-Expansion
        erzeugt nur bis einschließlich dieses Datums Dienste (siehe
        ``expand_cycle_assignments``). Ohne End-Datum gilt die Zuordnung offen.
        """
        filepath = self._table("CYASS")
        fields = get_table_fields(filepath)
        existing = read_dbf(filepath)
        max_id = max((r.get("ID", 0) or 0 for r in existing), default=0)
        new_id = max_id + 1
        record = {
            "ID": new_id,
            "EMPLOYEEID": employee_id,
            "CYCLEID": cycle_id,
            "START": start_date,
            "END": end_date or "",
            "ENTRANCE": start_date,
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return {
            "id": record["ID"],
            "employee_id": employee_id,
            "cycle_id": cycle_id,
            "start": start_date,
            "end": end_date or "",
        }

    def remove_cycle_assignment(self, employee_id: int) -> int:
        """Entfernt Zyklus-Zuweisung(en) eines Mitarbeiters. Liefert die Anzahl."""
        filepath = self._table("CYASS")
        fields = get_table_fields(filepath)
        matches = find_all_records(filepath, fields, EMPLOYEEID=employee_id)
        count = 0
        for idx, _ in matches:
            delete_record(filepath, fields, idx)
            count += 1
        return count

    def generate_schedule_from_cycle(
        self,
        year: int,
        month: int,
        employee_ids: list | None = None,
        force: bool = False,
        dry_run: bool = False,
        respect_restrictions: bool = True,
    ) -> dict:
        """
        Befüllt den Dienstplan für year/month aus den Schichtmodell-Zuweisungen.

        Berücksichtigt:
        - Schichtmodell-Zyklen (Basis)
        - Mitarbeiter-Verfügbarkeit (availability.json)
        - Qualifikationen/Skills (skills.json)
        - Wochenarbeitszeit-Limits (HRSWEEK aus Mitarbeiterdaten)
        - Bereits zugewiesene Schichten in der Woche (Konfliktvermeidung)
        - RESTR-Einschränkungen

        employee_ids: None = alle MAs mit Zyklus-Zuweisung, sonst nur diese
        force: True = bestehende Einträge überschreiben
        dry_run: True = nur Vorschau (nichts wird geschrieben)
        respect_restrictions: True = Schicht-Sperren aus RESTR beachten
        Gibt zurück: {'created': N, 'skipped': N, 'skipped_restriction': N,
                      'skipped_availability': N, 'skipped_hours_limit': N,
                      'errors': [...], 'preview': [...], 'report': {...}}
        """
        import calendar as _cal
        import json
        import os
        from datetime import date as _date

        # Collect all cycle assignments
        all_assignments = self.get_cycle_assignments()
        if employee_ids is not None:
            emp_id_set = set(employee_ids)
            all_assignments = [
                a for a in all_assignments if a["employee_id"] in emp_id_set
            ]

        if not all_assignments:
            return {
                "created": 0,
                "skipped": 0,
                "skipped_restriction": 0,
                "skipped_availability": 0,
                "skipped_hours_limit": 0,
                "errors": [],
                "preview": [],
                "report": {},
            }

        # Build cycle lookup: cycle_id -> cycle record
        cycles_raw = self._read("CYCLE")
        cycle_map = {c["ID"]: c for c in cycles_raw}

        # Build cycle entries lookup: cycle_id -> {index: shift_id}
        cyent_raw = self._read("CYENT")
        cycle_entries: dict = {}
        for e in cyent_raw:
            cid = e.get("CYCLEEID")
            idx = e.get("INDEX", 0)
            sid = e.get("SHIFTID", 0)
            if cid is None:
                continue
            if cid not in cycle_entries:
                cycle_entries[cid] = {}
            if sid:  # only non-zero shift IDs mean "has a shift"
                cycle_entries[cid][idx] = sid

        # Vorhandene Planeinträge des Monats sammeln (alle drei Tabellen)
        # Zusätzlich shift_ids je MA und Datum für die Wochenstunden-Berechnung führen
        prefix = f"{year:04d}-{month:02d}"
        existing_entries: set = set()  # (employee_id, date_str)
        existing_shift_map: dict = {}  # (employee_id, date_str) -> shift_id
        for r in self._read("MASHI"):
            d = r.get("DATE", "")
            if d and d.startswith(prefix):
                eid = r.get("EMPLOYEEID")
                if eid is not None:
                    existing_entries.add((eid, d))
                    existing_shift_map[(eid, d)] = r.get("SHIFTID")
        for table in ("SPSHI", "ABSEN"):
            for r in self._read(table):
                d = r.get("DATE", "")
                if d and d.startswith(prefix):
                    eid = r.get("EMPLOYEEID")
                    if eid is not None:
                        existing_entries.add((eid, d))

        # Build restrictions lookup: (employee_id, shift_id, weekday) -> True.
        # WEEKDAY ist der Original-Tag-Index (D-34): 0=Mo..6=So, 7=Feiertag.
        restrictions: set = set()  # (employee_id, shift_id, weekday)
        restr_holidays = calc.holiday_calendar(self._read("HOLID"))
        if respect_restrictions:
            try:
                for r in self._read("RESTR"):
                    eid = r.get("EMPLOYEEID")
                    sid = r.get("SHIFTID")
                    wday = r.get("WEEKDAY", 0) or 0
                    grade = r.get("RESTRICT", 0) or 0
                    # RESTRICT=0 („keine", D-58) ist KEINE Sperre und darf die
                    # Auto-Planung nicht blockieren; nur „auf Anfrage" (1) und
                    # „nie" (2) werden übersprungen.
                    if eid is not None and sid and grade >= 1:
                        restrictions.add((eid, sid, wday))
            except Exception:
                pass

        # ── Load availability data ──────────────────────────────
        availability_data: dict = {}  # emp_id (int) -> {day(0-6): {available, time_windows}}
        try:
            avail_path = os.path.join(_paths.api_data_dir(), "availability.json")
            if os.path.exists(avail_path):
                with open(avail_path) as f:
                    raw_avail = json.load(f)
                for emp_id_str, emp_avail in raw_avail.items():
                    try:
                        eid = int(emp_id_str)
                    except (ValueError, TypeError):
                        continue
                    day_map: dict = {}
                    for day_entry in emp_avail.get("days", []):
                        day_num = day_entry.get("day")  # 0=Mon, 6=Sun
                        if day_num is not None:
                            day_map[day_num] = {
                                "available": day_entry.get("available", True),
                                "time_windows": day_entry.get("time_windows", []),
                            }
                    availability_data[eid] = day_map
        except Exception:
            pass  # best-effort

        # ── Load skills data ────────────────────────────────────
        skills_assignments: dict = {}  # emp_id -> set of skill_ids
        try:
            skills_path = os.path.join(_paths.api_data_dir(), "skills.json")
            if os.path.exists(skills_path):
                with open(skills_path) as f:
                    skills_data = json.load(f)
                for assignment in skills_data.get("assignments", []):
                    eid = assignment.get("employee_id")
                    sid = assignment.get("skill_id")
                    if eid is not None and sid:
                        if eid not in skills_assignments:
                            skills_assignments[eid] = set()
                        skills_assignments[eid].add(sid)
        except Exception:
            pass  # best-effort

        num_days = _cal.monthrange(year, month)[1]
        created = 0
        skipped = 0
        skipped_restriction = 0
        skipped_availability = 0
        skipped_hours_limit = 0
        errors: list = []
        preview: list = []
        # Je-MA-Statistik für den Optimierungsbericht
        emp_stats: dict = {}  # emp_id -> {'total': 0, 'weekend': 0, 'night': 0}

        # Build employee data lookup
        try:
            employees_raw = self._read("EMPL")
            emp_names = {
                e.get("ID"): f"{e.get('NAME', '')} {e.get('FIRSTNAME', '')}".strip()
                for e in employees_raw
            }
            emp_weekly_hours: dict = {}  # emp_id -> max weekly hours (float)
            for e in employees_raw:
                eid = e.get("ID")
                hrs = e.get("HRSWEEK", 0)
                if eid is not None and hrs:
                    try:
                        emp_weekly_hours[eid] = float(hrs)
                    except (ValueError, TypeError):
                        pass
        except Exception:
            emp_names = {}
            emp_weekly_hours = {}

        # Build shift data lookup + identify night shifts
        night_shift_ids: set = set()
        shift_durations: dict = {}  # shift_id -> {weekday_index: hours}
        try:
            shifts_raw = self._read("SHIFT")
            shift_names = {
                s.get("ID"): s.get("SHORTNAME", "") or s.get("NAME", "")
                for s in shifts_raw
            }
            for s in shifts_raw:
                sid = s.get("ID")
                if sid is None:
                    continue
                # Build per-weekday durations
                durations: dict = {}
                for wd_idx in range(8):
                    dur_val = s.get(f"DURATION{wd_idx}", 0)
                    try:
                        durations[wd_idx] = float(dur_val) if dur_val else 0.0
                    except (ValueError, TypeError):
                        durations[wd_idx] = 0.0
                shift_durations[sid] = durations

                # Identify night shifts
                t = s.get("STARTEND0", "") or ""
                if "-" in t:
                    start_t = t.split("-")[0].strip()
                    try:
                        h = int(start_t.split(":")[0])
                        if h >= 20 or h < 6:
                            night_shift_ids.add(sid)
                    except Exception:
                        pass
        except Exception:
            shift_names = {}

        def _get_shift_duration_hours(shift_id: int, weekday_index: int) -> float:
            """Schichtdauer in Stunden für einen bestimmten Wochentag.

            Tagindex nach Orakel-Daten (Spec 3.4.3 / Parity-Befund 2):
            DURATION0=Montag … DURATION6=Sonntag, DURATION7=Feiertag.
            """
            durations = shift_durations.get(shift_id, {})
            return durations.get(weekday_index, 0.0)

        def _get_iso_week_key(d: _date) -> tuple:
            """Liefert (year, week_number) für die ISO-Wochen-Gruppierung."""
            iso = d.isocalendar()
            return (iso[0], iso[1])

        def _check_availability(emp_id: int, target_date: _date, shift_id: int) -> bool:
            """Prüft, ob der MA am Datum für die Schicht verfügbar ist.

            Liefert True bei Verfügbarkeit (oder ohne Verfügbarkeitsdaten),
            False bei Nichtverfügbarkeit.
            """
            if emp_id not in availability_data:
                return True  # Keine Verfügbarkeitsdaten → als verfügbar annehmen

            weekday_idx = target_date.weekday()  # 0=Mon, 6=Sun
            day_avail = availability_data[emp_id].get(weekday_idx)
            if day_avail is None:
                return True  # Keine Daten für diesen Tag → als verfügbar annehmen

            if not day_avail.get("available", True):
                return False  # Explizit nicht verfügbar

            # Verfügbar mit Zeitfenstern: Schichtzeit gegen die Fenster prüfen
            time_windows = day_avail.get("time_windows", [])
            if not time_windows:
                return True  # Ganztägig verfügbar

            # Schicht-Zeitbereich für diesen Wochentag parsen
            shift = shift_durations.get(shift_id)
            if shift is None:
                return True  # Schichtzeit nicht bestimmbar → als OK annehmen

            # Schicht-STARTEND für diesen Wochentag aus den Rohdaten holen
            try:
                shift_rec = None
                for s in self._read("SHIFT"):
                    if s.get("ID") == shift_id:
                        shift_rec = s
                        break
                if not shift_rec:
                    return True

                # Try weekday-specific STARTEND, fall back to STARTEND0
                startend_key = f"STARTEND{weekday_idx + 1}"
                startend = (shift_rec.get(startend_key, "") or "").strip()
                if not startend or "-" not in startend:
                    startend = (shift_rec.get("STARTEND0", "") or "").strip()
                if not startend or "-" not in startend:
                    return True  # Can't parse → assume OK

                parts = startend.split("-")
                sh, sm = parts[0].strip().split(":")
                eh, em = parts[1].strip().split(":")
                shift_start = int(sh) * 60 + int(sm)
                shift_end = int(eh) * 60 + int(em)
                if shift_end <= shift_start:
                    shift_end += 24 * 60  # overnight

                # Check if shift fits within any availability window
                for window in time_windows:
                    ws = window.get("start", "")
                    we = window.get("end", "")
                    if not ws or not we:
                        continue
                    wsh, wsm = ws.split(":")
                    weh, wem = we.split(":")
                    win_start = int(wsh) * 60 + int(wsm)
                    win_end = int(weh) * 60 + int(wem)
                    if win_start <= shift_start and shift_end <= win_end:
                        return True

                return False  # Shift doesn't fit in any availability window
            except Exception:
                return True  # Parse error → assume available

        # Track weekly hours: emp_id -> {(year, week): accumulated_hours}
        weekly_hours_tracker: dict = {}

        # Pre-populate weekly hours from existing schedule entries
        for (eid, date_str), sid in existing_shift_map.items():
            try:
                d = _date.fromisoformat(date_str)
                week_key = _get_iso_week_key(d)
                wd_idx = d.weekday()
                hrs = _get_shift_duration_hours(sid, wd_idx)
                if eid not in weekly_hours_tracker:
                    weekly_hours_tracker[eid] = {}
                weekly_hours_tracker[eid][week_key] = (
                    weekly_hours_tracker[eid].get(week_key, 0.0) + hrs
                )
            except Exception:
                pass

        # Deduplicate: keep only one assignment per employee (last one in list)
        emp_assignment: dict = {}
        for a in all_assignments:
            emp_assignment[a["employee_id"]] = a

        for emp_id, assignment in emp_assignment.items():
            cycle_id = assignment.get("cycle_id")
            start_str = assignment.get("start", "") or ""

            if not start_str:
                errors.append(f"MA {emp_id}: Kein Startdatum für Zyklus-Zuweisung")
                continue

            # Parse start date (stored as 'YYYY-MM-DD' ISO format)
            try:
                start_date = _date.fromisoformat(start_str[:10])
            except ValueError:
                errors.append(f"MA {emp_id}: Ungültiges Startdatum '{start_str}'")
                continue

            cycle = cycle_map.get(cycle_id)
            if not cycle:
                errors.append(f"MA {emp_id}: Zyklus {cycle_id} nicht gefunden")
                continue

            size = int(cycle.get("SIZE", 1) or 1)
            # UNIT=1: SIZE in Wochen; UNIT=0: SIZE in TAGEN (Original „Einheit:
            # Tage", Wine-belegt 2026-07-02) — sonst rotieren Tages-Modelle 7× zu lang.
            unit_weeks = int(cycle.get("UNIT", 1) or 0) == 1
            cycle_length = size * 7 if unit_weeks else size
            entries_for_cycle = cycle_entries.get(cycle_id, {})

            for day in range(1, num_days + 1):
                target_date = _date(year, month, day)
                date_str = target_date.isoformat()

                delta = (target_date - start_date).days
                if delta < 0:
                    # Tag liegt vor dem Zyklusstart → überspringen
                    continue

                position = delta % cycle_length
                shift_id = entries_for_cycle.get(position)
                if not shift_id:
                    # Keine Schicht an dieser Position (Frei) → überspringen
                    continue

                # Einschränkungen prüfen: WEEKDAY ist der Tag-Index 0=Mo..6=So,
                # 7=Feiertag (D-34); calc.day_index bildet das Datum entsprechend ab.
                if respect_restrictions and restrictions:
                    day_idx = calc.day_index(target_date, restr_holidays)
                    restricted = (emp_id, shift_id, day_idx) in restrictions
                    if restricted:
                        skipped_restriction += 1
                        preview.append(
                            {
                                "employee_id": emp_id,
                                "employee_name": emp_names.get(emp_id, f"MA {emp_id}"),
                                "date": date_str,
                                "shift_id": shift_id,
                                "shift_name": shift_names.get(shift_id, str(shift_id)),
                                "status": "restricted",
                            }
                        )
                        continue

                # ── Check availability ──────────────────────────
                if not _check_availability(emp_id, target_date, shift_id):
                    skipped_availability += 1
                    preview.append(
                        {
                            "employee_id": emp_id,
                            "employee_name": emp_names.get(emp_id, f"MA {emp_id}"),
                            "date": date_str,
                            "shift_id": shift_id,
                            "shift_name": shift_names.get(shift_id, str(shift_id)),
                            "status": "unavailable",
                        }
                    )
                    continue

                # ── Check weekly hours limit ────────────────────
                max_weekly = emp_weekly_hours.get(emp_id, 0)
                weekday_index = target_date.weekday()  # 0=Mon, 6=Sun
                shift_hours = _get_shift_duration_hours(shift_id, weekday_index)
                if max_weekly > 0 and shift_hours > 0:
                    week_key = _get_iso_week_key(target_date)
                    current_week_hours = (
                        weekly_hours_tracker.get(emp_id, {}).get(week_key, 0.0)
                    )
                    if current_week_hours + shift_hours > max_weekly:
                        skipped_hours_limit += 1
                        preview.append(
                            {
                                "employee_id": emp_id,
                                "employee_name": emp_names.get(emp_id, f"MA {emp_id}"),
                                "date": date_str,
                                "shift_id": shift_id,
                                "shift_name": shift_names.get(shift_id, str(shift_id)),
                                "status": "hours_exceeded",
                                "detail": f"{current_week_hours:.1f}h + {shift_hours:.1f}h > {max_weekly:.1f}h/Woche",
                            }
                        )
                        continue

                key = (emp_id, date_str)
                if key in existing_entries:
                    if not force:
                        skipped += 1
                        preview.append(
                            {
                                "employee_id": emp_id,
                                "employee_name": emp_names.get(emp_id, f"MA {emp_id}"),
                                "date": date_str,
                                "shift_id": shift_id,
                                "shift_name": shift_names.get(shift_id, str(shift_id)),
                                "status": "skip",
                            }
                        )
                        continue
                    # force=True: delete existing entry first (unless dry_run)
                    if not dry_run:
                        try:
                            self.delete_schedule_entry(emp_id, date_str)
                            existing_entries.discard(key)
                        except Exception as e:
                            errors.append(
                                f"MA {emp_id}, {date_str}: Fehler beim Löschen: {e}"
                            )
                            continue
                    preview.append(
                        {
                            "employee_id": emp_id,
                            "employee_name": emp_names.get(emp_id, f"MA {emp_id}"),
                            "date": date_str,
                            "shift_id": shift_id,
                            "shift_name": shift_names.get(shift_id, str(shift_id)),
                            "status": "overwrite",
                        }
                    )
                else:
                    preview.append(
                        {
                            "employee_id": emp_id,
                            "employee_name": emp_names.get(emp_id, f"MA {emp_id}"),
                            "date": date_str,
                            "shift_id": shift_id,
                            "shift_name": shift_names.get(shift_id, str(shift_id)),
                            "status": "new",
                        }
                    )

                # Statistik für den Optimierungsbericht führen
                if emp_id not in emp_stats:
                    emp_stats[emp_id] = {
                        "employee_id": emp_id,
                        "name": emp_names.get(emp_id, f"MA {emp_id}"),
                        "total": 0,
                        "weekend": 0,
                        "night": 0,
                        "skills": list(skills_assignments.get(emp_id, set())),
                        "weekly_hours_limit": emp_weekly_hours.get(emp_id, 0),
                    }
                emp_stats[emp_id]["total"] += 1
                if target_date.weekday() >= 5:
                    emp_stats[emp_id]["weekend"] += 1
                if shift_id in night_shift_ids:
                    emp_stats[emp_id]["night"] += 1

                # Update weekly hours tracker
                if shift_hours > 0:
                    week_key = _get_iso_week_key(target_date)
                    if emp_id not in weekly_hours_tracker:
                        weekly_hours_tracker[emp_id] = {}
                    weekly_hours_tracker[emp_id][week_key] = (
                        weekly_hours_tracker[emp_id].get(week_key, 0.0) + shift_hours
                    )

                if not dry_run:
                    try:
                        self.add_schedule_entry(emp_id, date_str, shift_id)
                        existing_entries.add(key)
                        created += 1
                    except Exception as e:
                        errors.append(f"MA {emp_id}, {date_str}: {e}")
                        preview.pop()  # remove from preview if write failed
                else:
                    if key not in existing_entries:
                        created += 1

        # Build optimization report
        report_employees = sorted(emp_stats.values(), key=lambda x: -x["total"])

        # Add weekly hours summary per employee
        for emp in report_employees:
            eid = emp["employee_id"]
            weeks = weekly_hours_tracker.get(eid, {})
            if weeks:
                emp["weekly_hours_planned"] = {
                    f"KW{wk[1]}": round(hrs, 1) for wk, hrs in sorted(weeks.items())
                }
                emp["max_weekly_hours_planned"] = round(max(weeks.values()), 1)
                emp["avg_weekly_hours_planned"] = round(
                    sum(weeks.values()) / len(weeks), 1
                )

        report: dict = {
            "employees": report_employees,
            "skipped_restriction": skipped_restriction,
            "skipped_availability": skipped_availability,
            "skipped_hours_limit": skipped_hours_limit,
        }
        if len(report_employees) >= 2:
            import statistics as _stats

            totals = [e["total"] for e in report_employees]
            weekends = [e["weekend"] for e in report_employees]
            nights = [e["night"] for e in report_employees]
            report["std_total"] = (
                round(_stats.stdev(totals), 2) if len(totals) > 1 else 0
            )
            report["std_weekend"] = (
                round(_stats.stdev(weekends), 2) if len(weekends) > 1 else 0
            )
            report["std_night"] = (
                round(_stats.stdev(nights), 2) if len(nights) > 1 else 0
            )
            # Gini-Koeffizient über die Gesamt-Schichten
            n = len(totals)
            mean = sum(totals) / n
            if mean > 0:
                gini = sum(abs(a - b) for a in totals for b in totals) / (
                    2 * n * n * mean
                )
            else:
                gini = 0.0
            report["gini"] = round(gini, 4)
            report["fairness_label"] = (
                "sehr gut"
                if gini < 0.05
                else ("gut" if gini < 0.1 else ("mittel" if gini < 0.2 else "schlecht"))
            )

        return {
            "created": created,
            "skipped": skipped,
            "skipped_restriction": skipped_restriction,
            "skipped_availability": skipped_availability,
            "skipped_hours_limit": skipped_hours_limit,
            "errors": errors,
            "preview": preview,
            "report": report,
        }

    # ── Staffing requirements ─────────────────────────────────
    def get_staffing(self, year: int, month: int) -> list[dict]:
        """Return staffing requirements from 5SHDEM (min/max per weekday/shift/group)."""
        rows = self._read("SHDEM")
        return rows

    def get_utilization(
        self, year: int, month: int, group_id: int | None = None
    ) -> list[dict]:
        """Personalauslastung gegen den Bedarf (Spec 3.9.4).

        Je Tag des Monats werden alle Bedarfszellen (Gruppe × Schichtart) aus
        5SHDEM geprüft (Wochenbedarf je Tagindex 0..7, Feiertag = 7 über
        calc.day_index); ein 5SPDEM-Satz (Tagesbedarf) überschreibt den
        Wochenbedarf seiner Zelle an seinem Datum. Eingeteilt zählt je
        Mitarbeiter höchstens einmal (calc.count_assigned über 5MASHI +
        expandierte 5CYASS + 5SPSHI mit SHIFTID). Zellstatus nach
        calc.utilization_status: ist<min ⇒ "under", ist>max ⇒ "over", sonst
        "ok"; Tage ohne definierten Bedarf erhalten den Status "none".
        """
        von = date(year, month, 1)
        bis = self._last_of_month(year, month)
        holidays = self._calc_holidays()

        members_by_group = self.get_all_group_members()
        group_ids = (
            [group_id] if group_id is not None else sorted(members_by_group.keys())
        )

        shdem_by_group: dict[int, list[dict]] = {}
        shifts_by_group: dict[int, set[int]] = {}
        for r in self._read("SHDEM"):
            gid = r.get("GROUPID") or 0
            shdem_by_group.setdefault(gid, []).append(r)
            sid = int(r.get("SHIFTID") or 0)
            if sid:
                shifts_by_group.setdefault(gid, set()).add(sid)

        spdem_by_cell: dict[tuple[int, str, int], dict] = {}
        for r in self._read("SPDEM"):
            gid = r.get("GROUPID") or 0
            sid = int(r.get("SHIFTID") or 0)
            d = r.get("DATE") or ""
            if sid and d:
                spdem_by_cell[(gid, d, sid)] = r

        manual = self._movement_by_employee("MASHI", von, bis)
        special = self._movement_by_employee("SPSHI", von, bis)
        cycle = self._cycle_shifts_by_employee(von, bis)
        entries_by_emp: dict[int, list[dict]] = {}
        for src in (manual, cycle, special):
            for eid, recs in src.items():
                entries_by_emp.setdefault(eid, []).extend(recs)

        member_entries_by_group = {
            gid: {
                eid: entries_by_emp.get(eid, ())
                for eid in members_by_group.get(gid, [])
            }
            for gid in group_ids
        }
        allowed_ids = (
            set(members_by_group.get(group_id, [])) if group_id is not None else None
        )

        # Tageszählung wie bisher: eingeteilte MA (5MASHI + 5CYASS + 5SPSHI TYPE=0)
        scheduled_by_day: dict[str, set[int]] = {}
        for eid, recs in manual.items():
            for r in recs:
                scheduled_by_day.setdefault(r.get("DATE") or "", set()).add(eid)
        for eid, recs in cycle.items():
            for r in recs:
                scheduled_by_day.setdefault(r.get("DATE") or "", set()).add(eid)
        for eid, recs in special.items():
            for r in recs:
                if int(r.get("TYPE") or 0) == 0:
                    scheduled_by_day.setdefault(r.get("DATE") or "", set()).add(eid)

        # Zellreihung wie im Original nach 5SHIFT.POSITION (Ordnung aus
        # get_shifts), nicht nach der ID; unbekannte IDs ans Ende.
        shift_order = {
            s.get("ID"): pos
            for pos, s in enumerate(self.get_shifts(include_hidden=True))
        }

        result = []
        num_days = calendar.monthrange(year, month)[1]
        for day in range(1, num_days + 1):
            d = date(year, month, day)
            iso = d.isoformat()
            cells = []
            for gid in group_ids:
                demands = shdem_by_group.get(gid, [])
                member_entries = member_entries_by_group.get(gid, {})
                shift_ids = set(shifts_by_group.get(gid, set()))
                shift_ids.update(
                    sid for (g, dd, sid) in spdem_by_cell if g == gid and dd == iso
                )
                for sid in sorted(
                    shift_ids,
                    key=lambda i: (shift_order.get(i, len(shift_order)), i),
                ):
                    spdem = spdem_by_cell.get((gid, iso, sid))
                    if spdem is not None:
                        mn, mx = int(spdem.get("MIN") or 0), int(spdem.get("MAX") or 0)
                        source = "SPDEM"
                    else:
                        demand = calc.demand_for_day(
                            demands, d, holidays=holidays, shift_id=sid
                        )
                        if demand is None:
                            continue
                        mn, mx = demand
                        source = "SHDEM"
                    assigned = calc.count_assigned(member_entries, d, [sid])
                    st = calc.utilization_status(assigned, mn, mx)
                    cells.append(
                        {
                            "group_id": gid,
                            "shift_id": sid,
                            "min": mn,
                            "max": mx,
                            "assigned": assigned,
                            "status": {-1: "under", 0: "ok", 1: "over"}[st],
                            "source": source,
                        }
                    )

            if any(c["status"] == "under" for c in cells):
                status = "under"
            elif any(c["status"] == "over" for c in cells):
                status = "over"
            elif cells:
                status = "ok"
            else:
                status = "none"

            scheduled = scheduled_by_day.get(iso, set())
            if allowed_ids is not None:
                scheduled = scheduled & allowed_ids
            result.append(
                {
                    "day": day,
                    "date": iso,
                    "scheduled_count": len(scheduled),
                    "required_count": sum(c["min"] for c in cells) if cells else None,
                    "required_min": sum(c["min"] for c in cells) if cells else None,
                    "required_max": sum(c["max"] for c in cells) if cells else None,
                    "status": status,
                    "cells": cells,
                }
            )
        return result

    # ── Write: SPSHI entries (Einsatzplan) ───────────────────
    def add_spshi_entry(
        self,
        employee_id: int,
        date_str: str,
        name: str = "",
        shortname: str = "",
        shift_id: int = 0,
        workplace_id: int = 0,
        entry_type: int = 0,
        startend: str = "",
        duration: float = 0.0,
        colortext: int = 0,
        colorbar: int = 0,
        colorbk: int = 16777215,
        noextra: bool = False,
    ) -> dict:
        """Hängt einen neuen Satz an 5SPSHI.DBF an. Liefert den angelegten Satz.

        ``noextra`` setzt 5SPSHI.NOEXTRA (Spec 3.8.3 Nr. 13): „keine
        Arbeitszeitzuschläge berechnen". Wirkt für freie Sonderdienste ohne
        Schicht-Referenz; bei gesetzter SHIFTID gilt das NOEXTRA der Schicht.
        """
        filepath = self._table("SPSHI")
        fields = get_table_fields(filepath)
        new_id = self._next_id("SPSHI")
        record = {
            "ID": new_id,
            "EMPLOYEEID": employee_id,
            "DATE": date_str,
            "NAME": name,
            "SHORTNAME": shortname,
            "SHIFTID": shift_id,
            "WORKPLACID": workplace_id,
            "TYPE": entry_type,
            "COLORTEXT": colortext,
            "COLORBAR": colorbar,
            "COLORBK": colorbk,
            "BOLD": 0,
            "STARTEND": startend,
            "DURATION": duration,
            "NOEXTRA": 1 if noextra else 0,
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return {**record, "id": record["ID"]}

    def update_spshi_entry(self, entry_id: int, data: dict) -> dict:
        """Aktualisiert Felder eines bestehenden 5SPSHI-Satzes."""
        filepath = self._table("SPSHI")
        fields = get_table_fields(filepath)
        raw_idx, existing = self._find_record("SPSHI", entry_id)
        if raw_idx is None:
            raise ValueError(f"SPSHI entry {entry_id} not found")
        allowed = (
            "NAME",
            "SHORTNAME",
            "SHIFTID",
            "WORKPLACID",
            "DATE",
            "TYPE",
            "COLORTEXT",
            "COLORBAR",
            "COLORBK",
            "STARTEND",
            "DURATION",
            "NOEXTRA",
        )
        update_data = {k: v for k, v in data.items() if k in allowed}
        if "NOEXTRA" in update_data:
            update_data["NOEXTRA"] = 1 if update_data["NOEXTRA"] else 0
        update_record(filepath, fields, raw_idx, update_data)
        return {"id": entry_id, **update_data}

    def delete_spshi_entry_by_id(self, entry_id: int) -> int:
        """Delete a SPSHI entry by its ID. Returns 1 if found, 0 otherwise."""
        filepath = self._table("SPSHI")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("SPSHI", entry_id)
        if raw_idx is None:
            return 0
        delete_record(filepath, fields, raw_idx)
        return 1

    def get_spshi_entries_for_day(
        self, date_str: str, group_id: int | None = None
    ) -> list[dict]:
        """Liefert alle SPSHI-Einträge eines Datums, optional gruppengefiltert."""
        if group_id is not None:
            member_ids = set(self.get_group_members(group_id))
        else:
            member_ids = None
        shifts_map = {s["ID"]: s for s in self.get_shifts(include_hidden=True)}
        workplaces_map = {w["ID"]: w for w in self.get_workplaces(include_hidden=True)}
        result = []
        for r in self._read("SPSHI"):
            if r.get("DATE") != date_str:
                continue
            eid = r.get("EMPLOYEEID")
            if member_ids is not None and eid not in member_ids:
                continue
            shift = shifts_map.get(r.get("SHIFTID", 0)) if r.get("SHIFTID") else None
            wp = (
                workplaces_map.get(r.get("WORKPLACID", 0))
                if r.get("WORKPLACID")
                else None
            )
            result.append(
                {
                    "id": r.get("ID"),
                    "employee_id": eid,
                    "date": date_str,
                    "name": r.get("NAME", ""),
                    "shortname": r.get("SHORTNAME", ""),
                    "shift_id": r.get("SHIFTID", 0),
                    "shift_name": shift.get("NAME", "") if shift else "",
                    "shift_short": shift.get("SHORTNAME", "") if shift else "",
                    "workplace_id": r.get("WORKPLACID", 0),
                    "workplace_name": wp.get("NAME", "") if wp else "",
                    "type": r.get("TYPE", 0),
                    "startend": r.get("STARTEND", ""),
                    "duration": float(r.get("DURATION", 0) or 0),
                    "colortext": r.get("COLORTEXT", 0),
                    "colorbar": r.get("COLORBAR", 0),
                    "colorbk": r.get("COLORBK", 16777215),
                    "color_bk": bgr_to_hex(r.get("COLORBK", 16777215))
                    if r.get("COLORBK")
                    else "#FFFFFF",
                    "color_text": bgr_to_hex(r.get("COLORTEXT", 0))
                    if r.get("COLORTEXT")
                    else "#000000",
                    "noextra": bool(r.get("NOEXTRA")),
                }
            )
        return result

    # ── Day schedule ──────────────────────────────────────────
    def get_schedule_day(
        self, date_str: str, group_id: int | None = None
    ) -> list[dict]:
        """
        Return all employees with their assignment for a specific day.
        date_str: 'YYYY-MM-DD'
        """
        employees = self.get_employees(include_hidden=False)
        if group_id is not None:
            member_ids = set(self.get_group_members(group_id))
            employees = [e for e in employees if e["ID"] in member_ids]

        shifts_map = {s["ID"]: s for s in self.get_shifts(include_hidden=True)}
        lt_map = {lt["ID"]: lt for lt in self.get_leave_types(include_hidden=True)}
        workplaces_map = {w["ID"]: w for w in self.get_workplaces(include_hidden=True)}

        # Zuweisungs-Lookup für den Tag bauen
        day_entries: dict[int, dict] = {}

        # Unmaterialisierte Zyklusdienste (5CYASS, Spec 6.3/4.2) als Basis —
        # 5MASHI/5SPSHI/5ABSEN überschreiben sie in den folgenden Schleifen.
        day_date = date.fromisoformat(date_str)
        for eid, recs in self._cycle_shifts_by_employee(day_date, day_date).items():
            for r in recs:
                day_entries[eid] = {
                    "kind": "shift",
                    "source": "cycle",
                    "shift_id": r.get("SHIFTID"),
                    "workplace_id": r.get("WORKPLACID"),
                    "leave_type_id": None,
                }

        for r in self._read("MASHI"):
            if r.get("DATE") == date_str:
                eid = r.get("EMPLOYEEID")
                if eid:
                    day_entries[eid] = {
                        "kind": "shift",
                        "shift_id": r.get("SHIFTID"),
                        "workplace_id": r.get("WORKPLACID"),
                        "leave_type_id": None,
                    }

        for r in self._read("SPSHI"):
            if r.get("DATE") == date_str:
                eid = r.get("EMPLOYEEID")
                if eid:
                    day_entries[eid] = {
                        "kind": "special_shift",
                        "shift_id": r.get("SHIFTID"),
                        "workplace_id": r.get("WORKPLACID"),
                        "leave_type_id": None,
                        "custom_name": r.get("NAME", ""),
                        "custom_short": r.get("SHORTNAME", ""),
                        "color_bk": bgr_to_hex(r.get("COLORBK", 16777215))
                        if r.get("COLORBK")
                        else None,
                        "color_text": bgr_to_hex(r.get("COLORTEXT", 0))
                        if r.get("COLORTEXT")
                        else None,
                        "spshi_id": r.get("ID"),
                        "spshi_type": r.get("TYPE", 0),
                        "spshi_startend": r.get("STARTEND", ""),
                        "spshi_duration": float(r.get("DURATION", 0) or 0),
                    }

        for r in self._read("ABSEN"):
            if r.get("DATE") == date_str:
                eid = r.get("EMPLOYEEID")
                if eid:
                    day_entries[eid] = {
                        "kind": "absence",
                        "shift_id": None,
                        "workplace_id": None,
                        "leave_type_id": r.get("LEAVETYPID"),
                    }

        result = []
        for emp in employees:
            eid = emp["ID"]
            entry = day_entries.get(eid, {})
            kind = entry.get("kind")
            shift_id = entry.get("shift_id")
            leave_type_id = entry.get("leave_type_id")
            workplace_id = entry.get("workplace_id")

            shift_name = ""
            shift_short = ""
            color_bk = "#FFFFFF"
            color_text = "#000000"
            leave_name = ""
            display_name = ""

            if shift_id and shift_id in shifts_map:
                s = shifts_map[shift_id]
                shift_name = s.get("NAME", "")
                shift_short = s.get("SHORTNAME", "")
                color_bk = entry.get("color_bk") or bgr_to_hex(
                    s.get("COLORBK", 16777215)
                )
                color_text = entry.get("color_text") or bgr_to_hex(
                    s.get("COLORTEXT", 0)
                )
                display_name = shift_short or shift_name
            elif leave_type_id and leave_type_id in lt_map:
                lt = lt_map[leave_type_id]
                leave_name = lt.get("NAME", "")
                color_bk = bgr_to_hex(lt.get("COLORBK", 16777215))
                color_text = bgr_to_hex(lt.get("COLORBAR", 0))
                display_name = lt.get("SHORTNAME", lt.get("NAME", ""))
            elif kind == "special_shift":
                color_bk = entry.get("color_bk", "#FFFFFF")
                color_text = entry.get("color_text", "#000000")
                display_name = entry.get("custom_short", entry.get("custom_name", ""))

            wp_name = ""
            if workplace_id and workplace_id in workplaces_map:
                wp_name = workplaces_map[workplace_id].get("NAME", "")

            result.append(
                {
                    "employee_id": eid,
                    "employee_name": f"{emp.get('NAME', '')}, {emp.get('FIRSTNAME', '')}".strip(
                        ", "
                    ),
                    "employee_short": emp.get("SHORTNAME", ""),
                    "shift_id": shift_id,
                    "shift_name": shift_name,
                    "shift_short": shift_short,
                    "color_bk": color_bk,
                    "color_text": color_text,
                    "workplace_id": workplace_id,
                    "workplace_name": wp_name,
                    "kind": kind,
                    "source": entry.get("source"),
                    "leave_name": leave_name,
                    "display_name": display_name,
                    "spshi_id": entry.get("spshi_id"),
                    "spshi_type": entry.get("spshi_type"),
                    "spshi_startend": entry.get("spshi_startend", ""),
                    "spshi_duration": entry.get("spshi_duration", 0.0),
                }
            )

        return result

    # ── Monthly statistics ────────────────────────────────────
    def get_statistics(
        self,
        year: int | None = None,
        month: int | None = None,
        group_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict]:
        """Liefert die Je-MA-Statistik für einen Monat oder freien Zeitraum.

        Ist-/Sollstunden folgen GetActualHours/GetNominalHours der
        Berechnungsschicht (Spec 3.3/3.4): CALCBASE-Dispatcher, tagindex-
        korrekte Schichtstunden, 5SPSHI-Ersetzung, expandierte 5CYASS,
        Abwesenheits-Anrechnung (3.5) und 5BOOK-Konten (3.6).

        Auswertungszeitraum (Spec 3.9.1): entweder ``year``+``month``
        (Monats-Komfort) oder ein freier Zeitraum ``date_from``/``date_to``
        (ISO-Datum, beide erforderlich; hat Vorrang vor year/month).
        """
        if date_from is not None and date_to is not None:
            von = date.fromisoformat(str(date_from))
            bis = date.fromisoformat(str(date_to))
            if von > bis:
                raise ValueError("date_from muss <= date_to sein")
        elif year is None or month is None:
            raise ValueError(
                "Entweder year+month oder date_from+date_to angeben"
            )
        else:
            von = date(year, month, 1)
            bis = self._last_of_month(year, month)

        employees = self.get_employees(include_hidden=False)
        if group_id is not None:
            member_ids = set(self.get_group_members(group_id))
            employees = [e for e in employees if e["ID"] in member_ids]

        # Build employee→primary group mapping (use get_all_group_members to avoid N+1)
        groups_all = self.get_groups()
        emp_group: dict[int, str] = {}
        emp_group_id: dict[int, int] = {}
        all_gm = self.get_all_group_members()  # {group_id: [employee_ids]}
        for grp in groups_all:
            gid = grp["ID"]
            for mid in all_gm.get(gid, []):
                if mid not in emp_group:
                    emp_group[mid] = grp.get("NAME", "")
                    emp_group_id[mid] = gid

        inputs = self._calc_inputs(von, bis)
        lt_map = inputs["leave_types_by_id"]
        lo, hi = von.isoformat(), bis.isoformat()

        result = []
        for emp in employees:
            eid = emp["ID"]
            ctx = self._calc_context(emp)
            plan = self._plan_kwargs(inputs, eid)
            absences = inputs["absences"].get(eid, [])
            bookings = inputs["bookings"].get(eid, [])

            target = calc.get_nominal_hours(
                ctx, von, bis, holidays=inputs["holidays"], bookings=bookings
            )
            actual = calc.get_actual_hours(
                ctx,
                von,
                bis,
                absences=absences,
                leave_types_by_id=lt_map,
                bookings=bookings,
                **plan,
            )
            shifts_count = sum(
                1
                for key in ("manual_shifts", "cycle_shifts", "special_shifts")
                for r in plan[key]
                if lo <= (r.get("DATE") or "") <= hi
            )

            abs_days = len(absences)
            vac = sick = 0
            for r in absences:
                lt = lt_map.get(r.get("LEAVETYPID"))
                if lt is None:
                    continue
                # Vacation: only ENTITLED=1 types count against leave quota
                # (CHARGETYP=1 alone, e.g. Krankheit, must NOT count as vacation)
                if lt.get("ENTITLED"):
                    vac += 1
                # Sick: detect by name keyword
                lt_name = (lt.get("NAME", "") or "").lower()
                lt_short = (lt.get("SHORTNAME", "") or "").lower()
                if any(
                    kw in lt_name or kw in lt_short for kw in ["krank", "sick", "ku"]
                ):
                    sick += 1

            result.append(
                {
                    "employee_id": eid,
                    "employee_name": f"{emp.get('NAME', '')}, {emp.get('FIRSTNAME', '')}".strip(
                        ", "
                    ),
                    "employee_short": emp.get("SHORTNAME", ""),
                    "group_name": emp_group.get(eid, ""),
                    "group_id": emp_group_id.get(eid, None),
                    "target_hours": round(target, 2),
                    "actual_hours": round(actual, 2),
                    "shifts_count": shifts_count,
                    "absence_days": abs_days,
                    "overtime_hours": round(actual - target, 2),
                    "vacation_used": vac,
                    "sick_days": sick,
                }
            )

        return result

    # ── Personaltabelle (Spec 3.9.2/3.9.3) ────────────────────
    def get_personnel_table(
        self, date_from: str, date_to: str, group_id: int | None = None
    ) -> dict:
        """Personaltabelle über einen freien Auswertungszeitraum (Spec 3.9).

        Standard-Spalten je Mitarbeiter über calc.personnel_table_row
        (Iststunden/Sollstunden/Saldo/Arbeitszeit/Abwesenheit bezahlt/
        Sonntag/Feiertag/Sonderdienste, Spec 3.9.2). Dynamische Spalten:
        Einteilungen je Schichtart (3.9.3 Nr. 4, calc.shift_assignment_counts)
        und Fehltage je Abwesenheitsart (Nr. 5, calc.absence_days_by_type).
        Umfasst der Zeitraum genau ein Kalenderjahr, liefern
        anspruchsverbundene Arten zusätzlich den Doppelwert
        genommen/verbleibend (Nr. 6, calc.leave_account).
        """
        von = date.fromisoformat(str(date_from))
        bis = date.fromisoformat(str(date_to))
        if von > bis:
            raise ValueError("date_from muss <= date_to sein")

        employees = self.get_employees(include_hidden=False)
        if group_id is not None:
            member_ids = set(self.get_group_members(group_id))
            employees = [e for e in employees if e["ID"] in member_ids]

        inputs = self._calc_inputs(von, bis)
        lt_map = inputs["leave_types_by_id"]
        leave_types = list(lt_map.values())

        # Spec 3.9.3 Nr. 6: Doppelwert nur bei genau einem Kalenderjahr
        one_year = (
            von == date(von.year, 1, 1) and bis == date(von.year, 12, 31)
        )
        leaen_by_emp: dict[int, list[dict]] = {}
        if one_year:
            for r in self._read("LEAEN"):
                leaen_by_emp.setdefault(r.get("EMPLOYEEID"), []).append(r)

        rows = []
        for emp in employees:
            eid = emp["ID"]
            ctx = self._calc_context(emp)
            plan = self._plan_kwargs(inputs, eid)
            absences = inputs["absences"].get(eid, [])
            bookings = inputs["bookings"].get(eid, [])

            std = calc.personnel_table_row(
                ctx,
                von,
                bis,
                absences=absences,
                leave_types_by_id=lt_map,
                bookings=bookings,
                **plan,
            )
            shift_counts = calc.shift_assignment_counts(
                ctx,
                von,
                bis,
                manual_shifts=plan["manual_shifts"],
                cycle_shifts=plan["cycle_shifts"],
            )
            absence_days = calc.absence_days_by_type(
                ctx,
                von,
                bis,
                holidays=inputs["holidays"],
                absences=absences,
                leave_types_by_id=lt_map,
            )
            row = {
                "employee_id": eid,
                "employee_name": f"{emp.get('NAME', '')}, {emp.get('FIRSTNAME', '')}".strip(
                    ", "
                ),
                "employee_short": emp.get("SHORTNAME", ""),
                **{
                    k: round(v, 2) if isinstance(v, float) else v
                    for k, v in std.items()
                },
                "shift_counts": shift_counts,
                "absence_days_by_type": {
                    lt_id: round(days, 2) for lt_id, days in absence_days.items()
                },
            }
            if one_year:
                accounts = {}
                for lt in leave_types:
                    if not lt.get("ENTITLED"):
                        continue
                    acct = calc.leave_account(
                        ctx,
                        von.year,
                        lt,
                        holidays=inputs["holidays"],
                        entitlements=leaen_by_emp.get(eid, []),
                        absences=absences,
                    )
                    accounts[lt["ID"]] = {
                        "taken": round(acct.taken, 2),
                        "remaining": round(acct.remaining, 2),
                    }
                row["leave_accounts"] = accounts
            rows.append(row)

        return {
            "date_from": von.isoformat(),
            "date_to": bis.isoformat(),
            "group_id": group_id,
            "one_year": one_year,
            "columns": {
                "shifts": [
                    {"id": s["ID"], "name": s.get("NAME", ""), "short": s.get("SHORTNAME", "")}
                    for s in self.get_shifts(include_hidden=False)
                ],
                "leave_types": [
                    {
                        "id": lt["ID"],
                        "name": lt.get("NAME", ""),
                        "short": lt.get("SHORTNAME", ""),
                        "entitled": bool(lt.get("ENTITLED")),
                    }
                    for lt in self.get_leave_types(include_hidden=False)
                ],
            },
            "rows": rows,
        }

    # ── Year overview ─────────────────────────────────────────
    def get_schedule_year(self, year: int, employee_id: int) -> list[dict]:
        """Liefert die Plan-Zusammenfassung je Monat für einen Mitarbeiter.

        actual_hours = Arbeitszeit nach GetWorkHours (Spec 3.4.2/3.4.3:
        tagindex-korrekte DURATION, 5SPSHI-Ersetzung, expandierte 5CYASS);
        target_hours = GetNominalHours (Spec 3.3, CALCBASE-Dispatcher).
        """
        shifts_map = {s["ID"]: s for s in self.get_shifts(include_hidden=True)}
        lt_map = {lt["ID"]: lt for lt in self.get_leave_types(include_hidden=True)}

        von = date(year, 1, 1)
        bis = date(year, 12, 31)
        inputs = self._calc_inputs(von, bis, employee_id)
        plan = self._plan_kwargs(inputs, employee_id)

        # Alle Einträge dieses MA im Jahr sammeln
        year_str = f"{year:04d}"
        monthly: dict[int, dict] = {}

        for m in range(1, 13):
            monthly[m] = {
                "month": m,
                "shifts": 0,
                "absences": 0,
                "target_hours": 0.0,
                "actual_hours": 0.0,
                "label_counts": {},
            }

        for source in ("manual_shifts", "cycle_shifts"):
            for r in plan[source]:
                d = r.get("DATE", "")
                if not d or not d.startswith(year_str):
                    continue
                m = int(d[5:7])
                monthly[m]["shifts"] += 1
                sid = r.get("SHIFTID")
                if sid and sid in shifts_map:
                    label = shifts_map[sid].get("SHORTNAME", "?")
                    monthly[m]["label_counts"][label] = (
                        monthly[m]["label_counts"].get(label, 0) + 1
                    )

        for r in plan["special_shifts"]:
            d = r.get("DATE", "")
            if not d or not d.startswith(year_str):
                continue
            m = int(d[5:7])
            monthly[m]["shifts"] += 1
            label = r.get("SHORTNAME", "?")
            monthly[m]["label_counts"][label] = (
                monthly[m]["label_counts"].get(label, 0) + 1
            )

        for r in inputs["absences"].get(employee_id, []):
            d = r.get("DATE", "")
            if d and d.startswith(year_str):
                m = int(d[5:7])
                monthly[m]["absences"] += 1
                ltid = r.get("LEAVETYPID")
                if ltid and ltid in lt_map:
                    label = lt_map[ltid].get("SHORTNAME", "?")
                    monthly[m]["label_counts"][label] = (
                        monthly[m]["label_counts"].get(label, 0) + 1
                    )

        # Soll- und Iststunden je Monat über die Berechnungsschicht füllen
        emp = self.get_employee(employee_id)
        if emp:
            ctx = self._calc_context(emp)
            for m in range(1, 13):
                m_von = date(year, m, 1)
                m_bis = self._last_of_month(year, m)
                target = calc.get_nominal_hours(
                    ctx, m_von, m_bis, holidays=inputs["holidays"]
                )
                actual = calc.get_work_hours(ctx, m_von, m_bis, **plan)
                monthly[m]["target_hours"] = round(target, 2)
                monthly[m]["actual_hours"] = round(actual, 2)

        return list(monthly.values())

    # ── Week schedule ─────────────────────────────────────────
    def get_schedule_week(self, date_str: str, group_id: int | None = None) -> dict:
        """
        Return schedule for the full ISO week (Mon–Sun) that contains date_str.
        Returns { week_start, week_end, days: [{ date, entries: [...] }] }
        """
        from datetime import timedelta

        dt = datetime.strptime(date_str, "%Y-%m-%d")
        monday = dt - timedelta(days=dt.weekday())
        week_dates = [
            (monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)
        ]

        employees = self.get_employees(include_hidden=False)
        if group_id is not None:
            member_ids = set(self.get_group_members(group_id))
            employees = [e for e in employees if e["ID"] in member_ids]

        shifts_map = {s["ID"]: s for s in self.get_shifts(include_hidden=True)}
        lt_map = {lt["ID"]: lt for lt in self.get_leave_types(include_hidden=True)}
        workplaces_map = {w["ID"]: w for w in self.get_workplaces(include_hidden=True)}
        emp_map = {e["ID"]: e for e in employees}
        allowed_ids = set(emp_map.keys())

        week_set = set(week_dates)

        # Build lookup: date -> employee_id -> entry
        day_entries: dict[str, dict[Any, dict]] = {d: {} for d in week_dates}

        # Unmaterialisierte Zyklusdienste (5CYASS, Spec 6.3/4.2) als Basis —
        # 5MASHI/5SPSHI/5ABSEN überschreiben sie in den folgenden Schleifen.
        week_von = date.fromisoformat(week_dates[0])
        week_bis = date.fromisoformat(week_dates[6])
        for eid, recs in self._cycle_shifts_by_employee(week_von, week_bis).items():
            if eid not in allowed_ids:
                continue
            for r in recs:
                d = r.get("DATE", "")
                if d in week_set:
                    day_entries[d][eid] = {
                        "kind": "shift",
                        "source": "cycle",
                        "shift_id": r.get("SHIFTID"),
                        "workplace_id": r.get("WORKPLACID"),
                        "leave_type_id": None,
                    }

        for r in self._read("MASHI"):
            d = r.get("DATE", "")
            if d in week_set:
                eid = r.get("EMPLOYEEID")
                if eid is not None and eid in allowed_ids:
                    day_entries[d][eid] = {
                        "kind": "shift",
                        "shift_id": r.get("SHIFTID"),
                        "workplace_id": r.get("WORKPLACID"),
                        "leave_type_id": None,
                    }

        for r in self._read("SPSHI"):
            d = r.get("DATE", "")
            if d in week_set:
                eid = r.get("EMPLOYEEID")
                if eid is not None and eid in allowed_ids:
                    day_entries[d][eid] = {
                        "kind": "special_shift",
                        "shift_id": r.get("SHIFTID"),
                        "workplace_id": r.get("WORKPLACID"),
                        "leave_type_id": None,
                        "custom_name": r.get("NAME", ""),
                        "custom_short": r.get("SHORTNAME", ""),
                        "color_bk": bgr_to_hex(r.get("COLORBK", 16777215))
                        if r.get("COLORBK")
                        else None,
                        "color_text": bgr_to_hex(r.get("COLORTEXT", 0))
                        if r.get("COLORTEXT")
                        else None,
                        "spshi_id": r.get("ID"),
                        "spshi_type": r.get("TYPE", 0),
                        "spshi_startend": r.get("STARTEND", ""),
                        "spshi_duration": float(r.get("DURATION", 0) or 0),
                    }

        for r in self._read("ABSEN"):
            d = r.get("DATE", "")
            if d in week_set:
                eid = r.get("EMPLOYEEID")
                if eid is not None and eid in allowed_ids:
                    day_entries[d][eid] = {
                        "kind": "absence",
                        "shift_id": None,
                        "workplace_id": None,
                        "leave_type_id": r.get("LEAVETYPID"),
                    }

        def _build_entry(emp: dict, entry: dict) -> dict:
            eid = emp["ID"]
            kind = entry.get("kind")
            shift_id = entry.get("shift_id")
            leave_type_id = entry.get("leave_type_id")
            workplace_id = entry.get("workplace_id")

            shift_name = ""
            shift_short = ""
            color_bk = "#FFFFFF"
            color_text = "#000000"
            leave_name = ""
            display_name = ""

            if shift_id and shift_id in shifts_map:
                s = shifts_map[shift_id]
                shift_name = s.get("NAME", "")
                shift_short = s.get("SHORTNAME", "")
                color_bk = entry.get("color_bk") or bgr_to_hex(
                    s.get("COLORBK", 16777215)
                )
                color_text = entry.get("color_text") or bgr_to_hex(
                    s.get("COLORTEXT", 0)
                )
                display_name = shift_short or shift_name
            elif leave_type_id and leave_type_id in lt_map:
                lt = lt_map[leave_type_id]
                leave_name = lt.get("NAME", "")
                color_bk = bgr_to_hex(lt.get("COLORBK", 16777215))
                color_text = bgr_to_hex(lt.get("COLORBAR", 0))
                display_name = lt.get("SHORTNAME", lt.get("NAME", ""))
            elif kind == "special_shift":
                color_bk = entry.get("color_bk", "#FFFFFF")
                color_text = entry.get("color_text", "#000000")
                display_name = entry.get("custom_short", entry.get("custom_name", ""))

            wp_name = ""
            if workplace_id and workplace_id in workplaces_map:
                wp_name = workplaces_map[workplace_id].get("NAME", "")

            return {
                "employee_id": eid,
                "employee_name": f"{emp.get('NAME', '')}, {emp.get('FIRSTNAME', '')}".strip(
                    ", "
                ),
                "employee_short": emp.get("SHORTNAME", ""),
                "shift_id": shift_id,
                "shift_name": shift_name,
                "shift_short": shift_short,
                "color_bk": color_bk,
                "color_text": color_text,
                "workplace_id": workplace_id,
                "workplace_name": wp_name,
                "kind": kind,
                "source": entry.get("source"),
                "leave_name": leave_name,
                "display_name": display_name,
                "spshi_id": entry.get("spshi_id"),
                "spshi_type": entry.get("spshi_type"),
                "spshi_startend": entry.get("spshi_startend", ""),
                "spshi_duration": entry.get("spshi_duration", 0.0),
            }

        days = []
        for d in week_dates:
            entries = []
            for emp in employees:
                eid = emp["ID"]
                entry = day_entries[d].get(eid, {})
                if entry:
                    entries.append(_build_entry(emp, entry))
                else:
                    entries.append(
                        {
                            "employee_id": eid,
                            "employee_name": f"{emp.get('NAME', '')}, {emp.get('FIRSTNAME', '')}".strip(
                                ", "
                            ),
                            "employee_short": emp.get("SHORTNAME", ""),
                            "shift_id": None,
                            "shift_name": "",
                            "shift_short": "",
                            "color_bk": "#FFFFFF",
                            "color_text": "#000000",
                            "workplace_id": None,
                            "workplace_name": "",
                            "kind": None,
                            "leave_name": "",
                            "display_name": "",
                            "spshi_id": None,
                            "spshi_type": None,
                            "spshi_startend": "",
                            "spshi_duration": 0.0,
                        }
                    )
            days.append({"date": d, "entries": entries})

        return {
            "week_start": week_dates[0],
            "week_end": week_dates[6],
            "days": days,
        }

    # ── Write: add schedule entry ─────────────────────────────
    def add_schedule_entry(
        self,
        employee_id: int,
        date_str: str,
        shift_id: int,
        schedule_type: int = 0,
        workplace_id: int = 0,
    ) -> dict:
        """Write a new schedule entry to MASHI.

        ``schedule_type`` (5MASHI.TYPE, Spec 4.12): 0=Istplan (Vorgabe),
        1=Sollplan (Zielvorgabe). Soll- und Istplan-Eintrag dürfen am selben
        Tag koexistieren (2-Zeilen-Ansicht); zwei Einträge derselben Planart
        an einem Tag sind weiterhin ein Duplikat. ``workplace_id`` setzt den
        Arbeitsplatz (5MASHI.WORKPLACID, Spec 6.4; 0 = keiner).

        Raises ValueError if an entry of the same plan type for this
        employee+date already exists in MASHI. Callers that want upsert
        semantics should call delete_schedule_entry first.
        """
        schedule_type = 1 if int(schedule_type or 0) == 1 else 0
        filepath = self._table("MASHI")
        fields = get_table_fields(filepath)
        # Duplikat-Guard (gleicher MA + Tag + Planart) über den gecachten
        # Lesepfad statt eines Datei-Voll-Scans: find_all_records parste hier
        # die komplette 5MASHI je Schreibvorgang neu (~0,5 s bei 20k+ Sätzen,
        # P1-b2) — der Guard braucht keine Datei-Indizes, nur die Existenz.
        # Konsistenz unverändert: auch der frühere Scan lief VOR dem Append
        # außerhalb des Append-Locks; die ID-Vergabe bleibt atomar (P0-1).
        duplicate = any(
            r.get("EMPLOYEEID") == employee_id
            and r.get("DATE") == date_str
            and int(r.get("TYPE") or 0) == schedule_type
            for r in self._read("MASHI")
        )
        if duplicate:
            raise ValueError(
                f"Schedule entry for employee {employee_id} on {date_str} already exists. "
                "Delete it first or use the bulk/update endpoint."
            )
        # Get max existing ID
        record = {
            # ID wird in append_record(autoid_field="ID") atomar unter dem Append-
            # Lock vergeben (P0-1) und in record zurückgeschrieben. Der frühere
            # Vorab-Volltabellen-Read für max(ID)+1 war damit toter Aufwand und
            # parste auf großen Tabellen die kompletten 5MASHI neu (~65 ms/Schreib-
            # vorgang) — entfernt, beschleunigt das Eintragen/Umplanen (Punkt 6).
            "ID": 0,
            "EMPLOYEEID": employee_id,
            "DATE": date_str,
            "SHIFTID": shift_id,
            "WORKPLACID": int(workplace_id or 0),
            "TYPE": schedule_type,
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        self._invalidate_cache("MASHI")
        return record

    def set_schedule_workplace(
        self,
        employee_id: int,
        date_str: str,
        workplace_id: int,
        schedule_type: int = 0,
    ) -> int:
        """Setze den Arbeitsplatz (5MASHI.WORKPLACID) eines bestehenden
        Dienstplan-Eintrags (Spec 6.4). ``workplace_id=0`` entfernt die Zuordnung.
        Wählt den Eintrag der angegebenen Planart (TYPE). Returns Anzahl
        aktualisierter Sätze (0 wenn kein passender Eintrag existiert)."""
        schedule_type = 1 if int(schedule_type or 0) == 1 else 0
        filepath = self._table("MASHI")
        fields = get_table_fields(filepath)
        matches = find_all_records(
            filepath, fields,
            EMPLOYEEID=employee_id, DATE=date_str, TYPE=schedule_type,
        )
        for idx, _ in matches:
            update_record(filepath, fields, idx, {"WORKPLACID": int(workplace_id or 0)})
        if matches:
            self._invalidate_cache("MASHI")
        return len(matches)

    # ── Write: delete schedule entry ──────────────────────────
    def delete_schedule_entry(self, employee_id: int, date_str: str) -> int:
        """Löscht Planeinträge zu MA+Datum aus MASHI, SPSHI, ABSEN. Liefert die Anzahl."""
        count = 0
        for table in ["MASHI", "SPSHI", "ABSEN"]:
            filepath = self._table(table)
            fields = get_table_fields(filepath)
            matches = find_all_records(
                filepath, fields, EMPLOYEEID=employee_id, DATE=date_str
            )
            for idx, _ in matches:
                delete_record(filepath, fields, idx)
                count += 1
        for table in ["MASHI", "SPSHI", "ABSEN"]:
            self._invalidate_cache(table)
        return count

    def delete_shift_only(self, employee_id: int, date_str: str) -> int:
        """Löscht nur Schichteinträge (MASHI, SPSHI) zu MA+Datum; Abwesenheiten bleiben."""
        count = 0
        for table in ["MASHI", "SPSHI"]:
            filepath = self._table(table)
            fields = get_table_fields(filepath)
            matches = find_all_records(
                filepath, fields, EMPLOYEEID=employee_id, DATE=date_str
            )
            for idx, _ in matches:
                delete_record(filepath, fields, idx)
                count += 1
        return count

    def delete_absence_only(self, employee_id: int, date_str: str) -> int:
        """Löscht nur Abwesenheiten (ABSEN) zu MA+Datum; Schichten bleiben."""
        count = 0
        filepath = self._table("ABSEN")
        fields = get_table_fields(filepath)
        matches = find_all_records(
            filepath, fields, EMPLOYEEID=employee_id, DATE=date_str
        )
        for idx, _ in matches:
            delete_record(filepath, fields, idx)
            count += 1
        return count

    # ── Write: add absence ────────────────────────────────────
    @staticmethod
    def _validate_absence_interval(interval: int, start: int, end: int) -> tuple[int, int]:
        """Validiert 5ABSEN.INTERVAL/START/END (Spec 3.5.2, D-54).

        INTERVAL: 0 = ganzer Tag, 1 = Vormittag, 2 = Nachmittag,
        3 = stundenweise (START/END in Minuten ab Mitternacht; END <= START
        bedeutet rechnerischen Tageswechsel, Spec 3.5.2 Nr. 3).
        Liefert die zu speichernden (START, END) — bei INTERVAL != 3 immer (0, 0).
        """
        if interval not in (0, 1, 2, 3):
            raise ValueError(f"INTERVAL muss 0..3 sein (war {interval})")
        if interval != 3:
            return 0, 0
        if not (0 <= start <= 1440 and 0 <= end <= 1440):
            raise ValueError("START/END müssen Minuten in 0..1440 sein")
        if start == end:
            # weder start < end noch Tageswechsel — 0/24h-Fenster ist ungültig
            raise ValueError(
                "Bei INTERVAL=3 muss START < END sein (oder END < START für "
                "einen Tageswechsel); START == END ist ungültig"
            )
        return start, end

    def add_absence(
        self,
        employee_id: int,
        date_str: str,
        leave_type_id: int,
        interval: int = 0,
        start: int = 0,
        end: int = 0,
    ) -> dict:
        """Write a new absence entry to ABSEN.

        ``interval`` ist der Teiltagsmodus (Spec 3.5.2/D-54): 0 = ganzer Tag,
        1 = Vormittag, 2 = Nachmittag, 3 = stundenweise mit ``start``/``end``
        in Minuten ab Mitternacht (END <= START = Tageswechsel).

        Raises ValueError if an absence for this employee+date already exists
        (regardless of leave type — one absence per day per employee).
        """
        start, end = self._validate_absence_interval(interval, start, end)
        filepath = self._table("ABSEN")
        fields = get_table_fields(filepath)
        # Doppelte Abwesenheiten desselben MA am selben Tag verhindern
        duplicates = find_all_records(
            filepath, fields, EMPLOYEEID=employee_id, DATE=date_str
        )
        if duplicates:
            raise ValueError(
                f"Absence for employee {employee_id} on {date_str} already exists."
            )
        record = {
            # ID atomar in append_record(autoid_field="ID") vergeben (P0-1); der
            # frühere max(ID)+1-Vorab-Read parste die ganze 5ABSEN neu — entfernt.
            "ID": 0,
            "EMPLOYEEID": employee_id,
            "DATE": date_str,
            "LEAVETYPID": leave_type_id,
            "TYPE": 0,
            "INTERVAL": interval,
            "START": start,
            "END": end,
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return record

    def update_absence(
        self,
        employee_id: int,
        date_str: str,
        leave_type_id: int | None = None,
        interval: int | None = None,
        start: int = 0,
        end: int = 0,
    ) -> dict:
        """Aktualisiert die Abwesenheit zu MA+Datum (5ABSEN).

        ``leave_type_id`` und ``interval`` werden nur geändert, wenn angegeben;
        bei ``interval`` gelten dieselben Regeln wie in :meth:`add_absence`.
        Wirft ValueError, wenn zu MA+Datum keine Abwesenheit existiert.
        """
        filepath = self._table("ABSEN")
        fields = get_table_fields(filepath)
        matches = find_all_records(
            filepath, fields, EMPLOYEEID=employee_id, DATE=date_str
        )
        if not matches:
            raise ValueError(
                f"Absence for employee {employee_id} on {date_str} not found."
            )
        raw_idx, existing = matches[0]
        update_data: dict[str, Any] = {}
        if leave_type_id is not None:
            update_data["LEAVETYPID"] = leave_type_id
        if interval is not None:
            start, end = self._validate_absence_interval(interval, start, end)
            update_data["INTERVAL"] = interval
            update_data["START"] = start
            update_data["END"] = end
        if update_data:
            update_record(filepath, fields, raw_idx, update_data)
        return {**existing, **update_data}

    # ── Staffing requirements (SHDEM + DADEM) ────────────────
    def get_staffing_requirements(
        self, year: int | None = None, month: int | None = None
    ) -> dict:
        """Return staffing requirements: SHDEM (shift min/max per weekday) + DADEM (daily totals)."""
        shdem_rows = self._read("SHDEM")
        dadem_rows = self._read("DADEM")
        shifts_map = {s["ID"]: s for s in self.get_shifts(include_hidden=True)}

        # SHDEM-Zeilen mit Schichtinfo anreichern
        enriched_shdem = []
        for r in shdem_rows:
            sid = r.get("SHIFTID")
            shift = shifts_map.get(sid) if sid else None
            enriched_shdem.append(
                {
                    "id": r.get("ID"),
                    "group_id": r.get("GROUPID"),
                    "weekday": r.get("WEEKDAY"),  # 0=Mon..6=Sun (assumed)
                    "shift_id": sid,
                    "shift_name": shift.get("NAME", "") if shift else "",
                    "shift_short": shift.get("SHORTNAME", "") if shift else "",
                    "color_bk": bgr_to_hex(shift.get("COLORBK", 16777215))
                    if shift
                    else "#FFFFFF",
                    "color_text": bgr_to_hex(shift.get("COLORTEXT", 0))
                    if shift
                    else "#000000",
                    "workplace_id": r.get("WORKPLACID"),
                    "min": r.get("MIN", 0),
                    "max": r.get("MAX", 0),
                }
            )

        return {
            "shift_requirements": enriched_shdem,
            "daily_requirements": dadem_rows,
        }

    # ── Notes ─────────────────────────────────────────────────
    def get_notes(
        self, date: str | None = None, employee_id: int | None = None
    ) -> list[dict]:
        """Liefert Notizen, optional nach Datum und/oder Mitarbeiter gefiltert."""
        rows = self._read("NOTE")
        result = []
        for r in rows:
            if date and r.get("DATE") != date:
                continue
            if employee_id is not None and r.get("EMPLOYEEID") != employee_id:
                continue
            result.append(
                {
                    "id": r.get("ID"),
                    "employee_id": r.get("EMPLOYEEID"),
                    "date": r.get("DATE", ""),
                    "text1": r.get("TEXT1", ""),
                    "text2": r.get("TEXT2", ""),
                    "category": (r.get("RESERVED") or "").strip(),
                }
            )
        return result

    def add_note(
        self,
        date: str,
        text: str,
        employee_id: int = 0,
        text2: str = "",
        category: str = "",
    ) -> dict:
        """Append a note to 5NOTE."""
        filepath = self._table("NOTE")
        fields = get_table_fields(filepath)
        existing = read_dbf(filepath)
        max_id = max((r.get("ID", 0) or 0 for r in existing), default=0)
        new_id = max_id + 1
        # TEXT1/TEXT2: C len=252 bytes, UTF-16-LE → 125 chars max
        # RESERVED (category): C len=20 bytes → 9 chars max
        _NOTE_TEXT_MAX = 125
        _NOTE_CAT_MAX = 9
        if text and len(text) > _NOTE_TEXT_MAX:
            raise ValueError(
                f"text zu lang: {len(text)} Zeichen (max {_NOTE_TEXT_MAX})"
            )
        if text2 and len(text2) > _NOTE_TEXT_MAX:
            raise ValueError(
                f"text2 zu lang: {len(text2)} Zeichen (max {_NOTE_TEXT_MAX})"
            )
        if category and len(category) > _NOTE_CAT_MAX:
            raise ValueError(
                f"category zu lang: {len(category)} Zeichen (max {_NOTE_CAT_MAX})"
            )
        record = {
            "ID": new_id,
            "EMPLOYEEID": employee_id,
            "DATE": date,
            "TEXT1": text or "",
            "TEXT2": text2 or "",
            "RESERVED": category or "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return {
            "id": record["ID"],
            "employee_id": employee_id,
            "date": date,
            "text1": text,
            "text2": text2,
            "category": category,
        }

    def delete_note(self, note_id: int) -> int:
        """Delete a note by ID from 5NOTE."""
        filepath = self._table("NOTE")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("NOTE", note_id)
        if raw_idx is None:
            return 0
        delete_record(filepath, fields, raw_idx)
        return 1

    def update_note(
        self,
        note_id: int,
        text1: str | None = None,
        text2: str | None = None,
        employee_id: int | None = None,
        date: str | None = None,
        category: str | None = None,
    ) -> dict | None:
        """Aktualisiert Felder einer bestehenden Notiz."""
        filepath = self._table("NOTE")
        fields = get_table_fields(filepath)
        raw_idx, record = self._find_record("NOTE", note_id)
        if raw_idx is None:
            return None
        _NOTE_TEXT_MAX = 125
        _NOTE_CAT_MAX = 9
        update_data: dict[str, Any] = {}
        if text1 is not None:
            if len(text1) > _NOTE_TEXT_MAX:
                raise ValueError(
                    f"text1 zu lang: {len(text1)} Zeichen (max {_NOTE_TEXT_MAX})"
                )
            update_data["TEXT1"] = text1
        if text2 is not None:
            if len(text2) > _NOTE_TEXT_MAX:
                raise ValueError(
                    f"text2 zu lang: {len(text2)} Zeichen (max {_NOTE_TEXT_MAX})"
                )
            update_data["TEXT2"] = text2
        if employee_id is not None:
            update_data["EMPLOYEEID"] = employee_id  # type: ignore[assignment]
        if date is not None:
            update_data["DATE"] = date
        if category is not None:
            if len(category) > _NOTE_CAT_MAX:
                raise ValueError(
                    f"category zu lang: {len(category)} Zeichen (max {_NOTE_CAT_MAX})"
                )
            update_data["RESERVED"] = category
        update_record(filepath, fields, raw_idx, update_data)
        # Return updated record
        _, updated = self._find_record("NOTE", note_id)
        if updated:
            return {
                "id": updated.get("ID"),
                "employee_id": updated.get("EMPLOYEEID"),
                "date": updated.get("DATE", ""),
                "text1": updated.get("TEXT1", ""),
                "text2": updated.get("TEXT2", ""),
                "category": (updated.get("RESERVED") or "").strip(),
            }
        return None

    # ── Periods ───────────────────────────────────────────────
    def get_periods(self, group_id: int | None = None) -> list[dict]:
        """Return accounting periods from PERIO."""
        rows = self._read("PERIO")
        result = []
        for r in rows:
            if group_id is not None and r.get("GROUPID") != group_id:
                continue
            result.append(
                {
                    "id": r.get("ID"),
                    "group_id": r.get("GROUPID"),
                    "start": r.get("START", ""),
                    "end": r.get("END", ""),
                    "color": bgr_to_hex(r.get("COLOR", 16777215))
                    if r.get("COLOR")
                    else None,
                    "description": r.get("DESCRIPT", ""),
                }
            )
        result.sort(key=lambda x: x.get("start", ""))
        return result

    # ── Write: Employees ──────────────────────────────────────
    def _next_id(self, table_name: str) -> int:
        """Liefert max(ID)+1 einer Tabelle."""
        rows = self._read(table_name)
        return max((r.get("ID", 0) or 0 for r in rows), default=0) + 1

    def _find_record(
        self, table_name: str, record_id: int
    ) -> tuple[int | None, dict | None]:
        """Liefert (raw_index, record) zum Satz mit der ID, sonst (None, None)."""
        filepath = self._table(table_name)
        fields = get_table_fields(filepath)
        matches = find_all_records(filepath, fields, ID=record_id)
        if matches:
            return matches[0]
        return None, None

    def create_employee(self, data: dict) -> dict:
        """Legt einen neuen Mitarbeiter-Satz an und liefert das neue MA-dict."""
        filepath = self._table("EMPL")
        fields = get_table_fields(filepath)
        new_id = self._next_id("EMPL")
        rows = self._read("EMPL")
        max_pos = max((r.get("POSITION", 0) or 0 for r in rows), default=0) + 1
        # Uniqueness check: SHORTNAME must be unique among active employees (if provided)
        shortname = (data.get("SHORTNAME") or "").strip().upper()
        if shortname:
            for row in rows:
                if row.get("HIDE"):
                    continue
                if (row.get("SHORTNAME") or "").strip().upper() == shortname:
                    raise ValueError(f"DUPLICATE:SHORTNAME:{shortname}")
        field_names = {f["name"] for f in fields}
        record = {
            "ID": new_id,
            "POSITION": data.get("POSITION", max_pos),
            "NUMBER": data.get("NUMBER", ""),
            "NAME": data.get("NAME", ""),
            "FIRSTNAME": data.get("FIRSTNAME", ""),
            "SHORTNAME": data.get("SHORTNAME", ""),
            "SEX": data.get("SEX", 0),
            "HRSDAY": data.get("HRSDAY", 0.0),
            "HRSWEEK": data.get("HRSWEEK", 0.0),
            "HRSMONTH": data.get("HRSMONTH", 0.0),
            "WORKDAYS": data.get("WORKDAYS", "1 1 1 1 1 0 0 0"),
            "HIDE": 1 if data.get("HIDE") else 0,
            "RESERVED": "",
        }
        # Add extended fields if present in DBF schema
        for key in (
            "HRSTOTAL",
            "BOLD",
            "CALCBASE",
            "DEDUCTHOL",
            "SALUTATION",
            "STREET",
            "ZIP",
            "TOWN",
            "PHONE",
            "EMAIL",
            "FUNCTION",
            "BIRTHDAY",
            "EMPSTART",
            "EMPEND",
            "NOTE1",
            "NOTE2",
            "NOTE3",
            "NOTE4",
            "ARBITR1",
            "ARBITR2",
            "ARBITR3",
            "CFGLABEL",
            "CBKLABEL",
            "CBKSCHED",
        ):
            if key in field_names and key in data and data[key] is not None:
                record[key] = data[key]
        append_record(filepath, fields, record, autoid_field="ID")
        self._invalidate_cache("EMPL")
        return {**record, "id": record["ID"]}

    def update_employee(self, emp_id: int, data: dict) -> dict:
        """Update an existing employee record by ID."""
        filepath = self._table("EMPL")
        fields = get_table_fields(filepath)
        field_names = {f["name"] for f in fields}
        raw_idx, existing = self._find_record("EMPL", emp_id)
        if raw_idx is None:
            raise ValueError(f"Employee {emp_id} not found")
        update_data = {}
        all_updatable = (
            "NAME",
            "FIRSTNAME",
            "SHORTNAME",
            "NUMBER",
            "SEX",
            "HRSDAY",
            "HRSWEEK",
            "HRSMONTH",
            "HRSTOTAL",
            "WORKDAYS",
            "HIDE",
            "BOLD",
            "POSITION",
            "SALUTATION",
            "STREET",
            "ZIP",
            "TOWN",
            "PHONE",
            "EMAIL",
            "FUNCTION",
            "BIRTHDAY",
            "EMPSTART",
            "EMPEND",
            "CALCBASE",
            "DEDUCTHOL",
            "NOTE1",
            "NOTE2",
            "NOTE3",
            "NOTE4",
            "ARBITR1",
            "ARBITR2",
            "ARBITR3",
            "CFGLABEL",
            "CBKLABEL",
            "CBKSCHED",
            "PHOTO",
        )
        for key in all_updatable:
            if key in data and data[key] is not None and key in field_names:
                update_data[key] = data[key]
        update_record(filepath, fields, raw_idx, update_data)
        return {"id": emp_id, **update_data}

    def change_password(self, user_id: int, new_password_plain: str) -> bool:
        """Change a user's password. Stores bcrypt hash in sidecar + legacy MD5 in DBF.

        Returns True if successful, False if user not found.
        """
        filepath = self._table("USER")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("USER", user_id)
        if raw_idx is None:
            return False
        # Legacy-MD5 in der DBF ablegen (Rückwärtskompatibilität zum Desktop-Client)
        digest = self._hash_password_md5(new_password_plain)
        update_record(filepath, fields, raw_idx, {"DIGEST": digest})
        # bcrypt-Hash im Sidecar ablegen (primär für die Web-Auth)
        bcrypt_hash = self._hash_password_bcrypt(new_password_plain)
        self._save_bcrypt_hash(user_id, bcrypt_hash)
        return True

    def delete_employee(self, emp_id: int) -> int:
        """Soft-delete (deactivate) an employee by setting HIDE=1.

        Historical data (shifts, absences, bookings) is preserved so that
        past schedules and reports remain accurate.  Only the employee
        record itself is marked as hidden/inactive.
        """
        filepath = self._table("EMPL")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("EMPL", emp_id)
        if raw_idx is None:
            return 0
        update_record(filepath, fields, raw_idx, {"HIDE": 1})
        self._invalidate_cache("EMPL")
        return 1

    def activate_employee(self, emp_id: int) -> int:
        """Reactivate a soft-deleted (hidden) employee by setting HIDE=0.

        Returns 1 if the employee was found and reactivated, 0 otherwise.
        """
        filepath = self._table("EMPL")
        fields = get_table_fields(filepath)
        raw_idx, existing = self._find_record("EMPL", emp_id)
        if raw_idx is None:
            return 0
        update_record(filepath, fields, raw_idx, {"HIDE": 0})
        self._invalidate_cache("EMPL")
        return 1

    # ── Write: Groups ─────────────────────────────────────────
    def create_group(self, data: dict) -> dict:
        """Legt einen neuen Gruppen-Satz an und liefert das neue Gruppen-dict."""
        filepath = self._table("GROUP")
        fields = get_table_fields(filepath)
        field_names = {f["name"] for f in fields}
        new_id = self._next_id("GROUP")
        rows = self._read("GROUP")
        max_pos = max((r.get("POSITION", 0) or 0 for r in rows), default=0) + 1
        record = {
            "ID": new_id,
            "NAME": data.get("NAME", ""),
            "SHORTNAME": data.get("SHORTNAME", ""),
            "SUPERID": data.get("SUPERID", 0),
            "POSITION": data.get("POSITION", max_pos),
            "HIDE": 1 if data.get("HIDE") else 0,
            "RESERVED": "",
        }
        for key in ("BOLD", "DAILYDEM", "ARBITR", "CFGLABEL", "CBKLABEL", "CBKSCHED"):
            if key in field_names and key in data and data[key] is not None:
                record[key] = data[key]
        append_record(filepath, fields, record, autoid_field="ID")
        self._invalidate_cache("GROUP")
        return {**record, "id": record["ID"]}

    def update_group(self, group_id: int, data: dict) -> dict:
        """Update an existing group record by ID."""
        filepath = self._table("GROUP")
        fields = get_table_fields(filepath)
        field_names = {f["name"] for f in fields}
        raw_idx, _ = self._find_record("GROUP", group_id)
        if raw_idx is None:
            raise ValueError(f"Group {group_id} not found")
        update_data = {}
        for key in (
            "NAME",
            "SHORTNAME",
            "SUPERID",
            "POSITION",
            "HIDE",
            "BOLD",
            "DAILYDEM",
            "ARBITR",
            "CFGLABEL",
            "CBKLABEL",
            "CBKSCHED",
        ):
            if key in data and data[key] is not None and key in field_names:
                update_data[key] = data[key]
        update_record(filepath, fields, raw_idx, update_data)
        self._invalidate_cache("GROUP")
        return {"id": group_id, **update_data}

    def delete_group(self, group_id: int) -> int:
        """Soft-Delete einer Gruppe samt Kaskaden-Entfernung ihrer Mitgliedschaften (GRASG)."""
        filepath = self._table("GROUP")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("GROUP", group_id)
        if raw_idx is None:
            return 0
        update_record(filepath, fields, raw_idx, {"HIDE": 1})
        # Cascade: remove group membership entries
        grasg_path = self._table("GRASG")
        grasg_fields = get_table_fields(grasg_path)
        grasg_matches = find_all_records(grasg_path, grasg_fields, GROUPID=group_id)
        for idx, _ in reversed(grasg_matches):
            delete_record(grasg_path, grasg_fields, idx)
        return 1

    def add_group_member(self, group_id: int, employee_id: int) -> dict:
        """Add an employee to a group."""
        filepath = self._table("GRASG")
        fields = get_table_fields(filepath)
        # Check not already a member
        matches = find_all_records(
            filepath, fields, GROUPID=group_id, EMPLOYEEID=employee_id
        )
        if matches:
            return {
                "id": matches[0][1].get("ID"),
                "group_id": group_id,
                "employee_id": employee_id,
            }
        new_id = self._next_id("GRASG")
        record = {
            "ID": new_id,
            "EMPLOYEEID": employee_id,
            "GROUPID": group_id,
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return record

    def remove_group_member(self, group_id: int, employee_id: int) -> int:
        """Remove an employee from a group."""
        filepath = self._table("GRASG")
        fields = get_table_fields(filepath)
        matches = find_all_records(
            filepath, fields, GROUPID=group_id, EMPLOYEEID=employee_id
        )
        count = 0
        for raw_idx, _ in matches:
            delete_record(filepath, fields, raw_idx)
            count += 1
        return count

    # ── Write: Shifts ─────────────────────────────────────────
    def create_shift(self, data: dict) -> dict:
        """Create a new shift definition."""
        filepath = self._table("SHIFT")
        fields = get_table_fields(filepath)
        field_names = {f["name"] for f in fields}
        new_id = self._next_id("SHIFT")
        rows = self._read("SHIFT")
        max_pos = max((r.get("POSITION", 0) or 0 for r in rows), default=0) + 1
        # Uniqueness check: NAME must be unique among active shifts
        name_lower = (data.get("NAME") or "").strip().lower()
        for row in rows:
            if row.get("HIDE"):
                continue
            if (row.get("NAME") or "").strip().lower() == name_lower:
                raise ValueError(f"DUPLICATE:SHIFTNAME:{data.get('NAME')}")
        record = {
            "ID": new_id,
            "NAME": data.get("NAME", ""),
            "SHORTNAME": data.get("SHORTNAME", ""),
            "POSITION": data.get("POSITION", max_pos),
            "COLORTEXT": data.get("COLORTEXT", 0),
            "COLORBAR": data.get("COLORBAR", 0),
            "COLORBK": data.get("COLORBK", 16777215),
            "BOLD": 1 if data.get("BOLD") else 0,
            "DURATION0": data.get("DURATION0", 0.0),
            "HIDE": 1 if data.get("HIDE") else 0,
            "RESERVED": "",
        }
        # Add per-weekday duration/startend fields if present in schema
        for i in range(1, 8):
            dk = f"DURATION{i}"
            sk = f"STARTEND{i}"
            if dk in field_names and dk in data and data[dk] is not None:
                record[dk] = data[dk]
            if sk in field_names and sk in data and data[sk] is not None:
                record[sk] = data[sk]
        if (
            "STARTEND0" in field_names
            and "STARTEND0" in data
            and data["STARTEND0"] is not None
        ):
            record["STARTEND0"] = data["STARTEND0"]
        # R5.5-15: keine Arbeitszeitzuschläge berechnen
        if "NOEXTRA" in field_names and "NOEXTRA" in data and data["NOEXTRA"] is not None:
            record["NOEXTRA"] = 1 if data["NOEXTRA"] else 0
        append_record(filepath, fields, record, autoid_field="ID")
        self._invalidate_cache("SHIFT")
        return {**record, "id": record["ID"]}

    def update_shift(self, shift_id: int, data: dict) -> dict:
        """Update an existing shift definition by ID."""
        filepath = self._table("SHIFT")
        fields = get_table_fields(filepath)
        field_names = {f["name"] for f in fields}
        raw_idx, _ = self._find_record("SHIFT", shift_id)
        if raw_idx is None:
            raise ValueError(f"Shift {shift_id} not found")
        update_data = {}
        base_keys = (
            "NAME",
            "SHORTNAME",
            "POSITION",
            "COLORTEXT",
            "COLORBAR",
            "COLORBK",
            "DURATION0",
            "HIDE",
        )
        for key in base_keys:
            if key in data:
                update_data[key] = data[key]
        # Per-weekday duration/startend fields
        for i in range(8):
            dk = f"DURATION{i}"
            sk = f"STARTEND{i}"
            if dk in data and dk in field_names:
                update_data[dk] = data[dk]
            if sk in data and sk in field_names:
                update_data[sk] = data[sk]
        # R5.5-15: keine Arbeitszeitzuschläge berechnen
        if "NOEXTRA" in data and "NOEXTRA" in field_names:
            update_data["NOEXTRA"] = 1 if data["NOEXTRA"] else 0
        # Fette Schrift (5SHIFT.BOLD)
        if "BOLD" in data and "BOLD" in field_names:
            update_data["BOLD"] = 1 if data["BOLD"] else 0
        update_record(filepath, fields, raw_idx, update_data)
        self._invalidate_cache("SHIFT")
        return {"id": shift_id, **update_data}

    def shift_active_usage_count(self, shift_id: int) -> int:
        """Liefert die Zahl aktiver MASHI-Sätze, die diese shift_id referenzieren."""
        count = 0
        for table in ("MASHI", "SPSHI"):
            try:
                filepath = self._table(table)
                fields = get_table_fields(filepath)
                matches = find_all_records(filepath, fields, SHIFTID=shift_id)
                count += len(matches)
            except Exception:
                pass
        return count

    def hide_shift(self, shift_id: int) -> int:
        filepath = self._table("SHIFT")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("SHIFT", shift_id)
        if raw_idx is None:
            return 0
        update_record(filepath, fields, raw_idx, {"HIDE": 1})
        self._invalidate_cache("SHIFT")
        return 1

    # ── Write: Leave Types ────────────────────────────────────
    def create_leave_type(self, data: dict) -> dict:
        filepath = self._table("LEAVT")
        fields = get_table_fields(filepath)
        new_id = self._next_id("LEAVT")
        rows = self._read("LEAVT")
        max_pos = max((r.get("POSITION", 0) or 0 for r in rows), default=0) + 1
        record = {
            "ID": new_id,
            "NAME": data.get("NAME", ""),
            "SHORTNAME": data.get("SHORTNAME", ""),
            "POSITION": data.get("POSITION", max_pos),
            "COLORTEXT": data.get("COLORTEXT", 0),
            "COLORBAR": data.get("COLORBAR", 0),
            "COLORBK": data.get("COLORBK", 16777215),
            "BOLD": 1 if data.get("BOLD") else 0,
            "ENTITLED": 1 if data.get("ENTITLED") else 0,
            "STDENTIT": data.get("STDENTIT", 0.0),
            "HIDE": 1 if data.get("HIDE") else 0,
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return {**record, "id": record["ID"]}

    def update_leave_type(self, lt_id: int, data: dict) -> dict:
        filepath = self._table("LEAVT")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("LEAVT", lt_id)
        if raw_idx is None:
            raise ValueError(f"LeaveType {lt_id} not found")
        update_data = {}
        for key in (
            "NAME",
            "SHORTNAME",
            "POSITION",
            "COLORTEXT",
            "COLORBAR",
            "COLORBK",
            "ENTITLED",
            "STDENTIT",
            "HIDE",
        ):
            if key in data:
                update_data[key] = data[key]
        # Fette Schrift (5LEAVT.BOLD)
        if "BOLD" in data:
            update_data["BOLD"] = 1 if data["BOLD"] else 0
        update_record(filepath, fields, raw_idx, update_data)
        return {"id": lt_id, **update_data}

    def leave_type_active_usage_count(self, lt_id: int) -> int:
        """Liefert die Zahl der ABSEN-Sätze, die diese Abwesenheitsart referenzieren."""
        try:
            filepath = self._table("ABSEN")
            fields = get_table_fields(filepath)
            matches = find_all_records(filepath, fields, LEAVETYPID=lt_id)
            return len(matches)
        except Exception:
            return 0

    def hide_leave_type(self, lt_id: int) -> int:
        filepath = self._table("LEAVT")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("LEAVT", lt_id)
        if raw_idx is None:
            return 0
        update_record(filepath, fields, raw_idx, {"HIDE": 1})
        return 1

    # ── Write: Holidays ───────────────────────────────────────
    @staticmethod
    def _validate_holiday_interval(interval: int) -> int:
        """Validiert 5HOLID.INTERVAL (Spec 3.2.1 Nr. 3).

        0 = ganztägiger Feiertag; ≠ 0 = halber Feiertag mit Tageshälfte
        1 → 0–720 Min. bzw. 2 → 720–1440 Min. (UNSICHER: Zuordnung
        1=Vormittag/2=Nachmittag aus der Zuschlags-Disassembly plausibel,
        datenseitig unbestätigt — Beispiel-DB enthält nur INTERVAL=0).
        """
        interval = int(interval or 0)
        if interval not in (0, 1, 2):
            raise ValueError(f"INTERVAL muss 0, 1 oder 2 sein (war {interval})")
        return interval

    def create_holiday(self, data: dict, repeat_years: int = 0) -> dict:
        """Feiertag anlegen (5HOLID).

        ``repeat_years`` = Option "auch in den Folgejahren" (Spec 3.2.1 Nr. 4 /
        R5.16-5): legt zusätzlich Datumssätze mit gleichem Termin für die
        nächsten ``repeat_years`` Jahre an (Original: 9).
        """
        interval = self._validate_holiday_interval(data.get("INTERVAL", 0))
        filepath = self._table("HOLID")
        fields = get_table_fields(filepath)
        new_id = self._next_id("HOLID")
        record = {
            "ID": new_id,
            "DATE": data.get("DATE", ""),
            "NAME": data.get("NAME", ""),
            "INTERVAL": interval,
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        repeated_ids: list[int] = []
        if repeat_years > 0:
            base = datetime.strptime(record["DATE"], "%Y-%m-%d").date()
            for offset in range(1, repeat_years + 1):
                year = base.year + offset
                try:
                    d = base.replace(year=year)
                except ValueError:  # 29.02. in Nicht-Schaltjahr
                    continue
                # Each follow-up year gets its own atomically allocated ID.
                child = {**record, "DATE": d.isoformat()}
                append_record(filepath, fields, child, autoid_field="ID")
                repeated_ids.append(child["ID"])
        self._invalidate_cache("HOLID")
        result = {**record, "id": record["ID"]}
        if repeated_ids:
            result["repeated_ids"] = repeated_ids
        return result

    def update_holiday(self, holiday_id: int, data: dict) -> dict:
        filepath = self._table("HOLID")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("HOLID", holiday_id)
        if raw_idx is None:
            raise ValueError(f"Holiday {holiday_id} not found")
        update_data = {}
        for key in ("DATE", "NAME", "INTERVAL"):
            if key in data:
                update_data[key] = data[key]
        if "INTERVAL" in update_data:
            update_data["INTERVAL"] = self._validate_holiday_interval(
                update_data["INTERVAL"]
            )
        update_record(filepath, fields, raw_idx, update_data)
        self._invalidate_cache("HOLID")
        return {"id": holiday_id, **update_data}

    def delete_holiday(self, holiday_id: int) -> int:
        filepath = self._table("HOLID")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("HOLID", holiday_id)
        if raw_idx is None:
            return 0
        delete_record(filepath, fields, raw_idx)
        self._invalidate_cache("HOLID")
        return 1

    # ── Write: Workplaces ─────────────────────────────────────
    def create_workplace(self, data: dict) -> dict:
        filepath = self._table("WOPL")
        fields = get_table_fields(filepath)
        new_id = self._next_id("WOPL")
        rows = self._read("WOPL")
        max_pos = max((r.get("POSITION", 0) or 0 for r in rows), default=0) + 1
        record = {
            "ID": new_id,
            "NAME": data.get("NAME", ""),
            "SHORTNAME": data.get("SHORTNAME", ""),
            "POSITION": data.get("POSITION", max_pos),
            "COLORTEXT": data.get("COLORTEXT", 0),
            "COLORBAR": data.get("COLORBAR", 0),
            "COLORBK": data.get("COLORBK", 16777215),
            "HIDE": 1 if data.get("HIDE") else 0,
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return {**record, "id": record["ID"]}

    def update_workplace(self, wp_id: int, data: dict) -> dict:
        filepath = self._table("WOPL")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("WOPL", wp_id)
        if raw_idx is None:
            raise ValueError(f"Workplace {wp_id} not found")
        update_data = {}
        for key in (
            "NAME",
            "SHORTNAME",
            "POSITION",
            "COLORTEXT",
            "COLORBAR",
            "COLORBK",
            "HIDE",
        ):
            if key in data:
                update_data[key] = data[key]
        update_record(filepath, fields, raw_idx, update_data)
        return {"id": wp_id, **update_data}

    def hide_workplace(self, wp_id: int) -> int:
        filepath = self._table("WOPL")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("WOPL", wp_id)
        if raw_idx is None:
            return 0
        update_record(filepath, fields, raw_idx, {"HIDE": 1})
        return 1

    # ── Manuelle Stammdaten-Sortierung (POSITION, Spec 5.1 Nr. 4) ──
    #: Tabellen mit POSITION-Feld, die programmweit manuell sortierbar sind.
    _REORDERABLE = {
        "employees": "EMPL",
        "shifts": "SHIFT",
        "groups": "GROUP",
        "leave_types": "LEAVT",
        "workplaces": "WOPL",
    }

    def reorder(self, entity: str, ordered_ids: list[int]) -> int:
        """Weise den Datensätzen einer Stammdaten-Tabelle POSITION 1..N in der
        übergebenen Reihenfolge zu (manuelle, programmweite Sortierung, Spec 5.1
        Nr. 4). ``entity`` ist einer von ``employees/shifts/groups/leave_types/
        workplaces``. Returns Anzahl aktualisierter Sätze."""
        table = self._REORDERABLE.get(entity)
        if table is None:
            raise ValueError(f"Unbekannte oder nicht sortierbare Entität: {entity}")
        filepath = self._table(table)
        fields = get_table_fields(filepath)
        n = 0
        for pos, rec_id in enumerate(ordered_ids, start=1):
            for raw_idx, _ in find_all_records(filepath, fields, ID=int(rec_id)):
                update_record(filepath, fields, raw_idx, {"POSITION": pos})
                n += 1
        if n:
            self._invalidate_cache(table)
        return n

    # ── Workplace ↔ Employee Assignments (sidecar JSON) ────────
    def _assignments_path(self) -> str:
        return os.path.join(self.db_path, "workplace_assignments.json")

    def _load_assignments(self) -> dict[str, list[int]]:
        """Load {workplace_id_str: [employee_id, ...]} from sidecar JSON."""
        import json

        path = self._assignments_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_assignments(self, data: dict[str, list[int]]) -> None:
        path = self._assignments_path()
        _atomic_write_json(path, data, indent=2)

    def get_workplace_employees(self, wp_id: int) -> list[dict]:
        """Return employees explicitly assigned to a workplace (sidecar JSON)."""
        assignments = self._load_assignments()
        emp_ids = assignments.get(str(wp_id), [])
        if not emp_ids:
            return []
        emps = {e["ID"]: e for e in self.get_employees(include_hidden=True)}
        result = []
        for eid in emp_ids:
            e = emps.get(eid)
            if e:
                result.append(
                    {
                        "ID": e.get("ID"),
                        "NAME": e.get("NAME", ""),
                        "FIRSTNAME": e.get("FIRSTNAME", ""),
                        "SHORTNAME": e.get("SHORTNAME", ""),
                        "NUMBER": e.get("NUMBER", ""),
                    }
                )
        return result

    def assign_employee_to_workplace(self, employee_id: int, workplace_id: int) -> bool:
        """Assign an employee to a workplace (idempotent)."""
        assignments = self._load_assignments()
        key = str(workplace_id)
        if key not in assignments:
            assignments[key] = []
        if employee_id not in assignments[key]:
            assignments[key].append(employee_id)
            self._save_assignments(assignments)
            return True
        return False  # already assigned

    def remove_employee_from_workplace(
        self, employee_id: int, workplace_id: int
    ) -> bool:
        """Remove an employee from a workplace."""
        assignments = self._load_assignments()
        key = str(workplace_id)
        lst = assignments.get(key, [])
        if employee_id in lst:
            lst.remove(employee_id)
            assignments[key] = lst
            self._save_assignments(assignments)
            return True
        return False  # not assigned

    # ── Extra Charges (XCHAR) ─────────────────────────────────
    def get_extracharges(self, include_hidden: bool = False) -> list[dict]:
        rows = self._read("XCHAR")
        if not include_hidden:
            rows = [r for r in rows if not r.get("HIDE")]
        result = []
        for r in rows:
            result.append(
                {
                    "ID": r.get("ID"),
                    "NAME": r.get("NAME", ""),
                    "POSITION": r.get("POSITION", 0),
                    "START": r.get("START", 0),  # minutes from midnight
                    "END": r.get("END", 0),
                    "VALIDITY": r.get("VALIDITY", 0),
                    "VALIDDAYS": r.get("VALIDDAYS", ""),
                    "DATE": r.get("DATE") or "",
                    "HOLRULE": r.get("HOLRULE", 0),
                    "HIDE": r.get("HIDE", 0),
                }
            )
        result.sort(key=lambda x: x.get("POSITION", 0))
        return result

    def create_extracharge(self, data: dict) -> dict:
        filepath = self._table("XCHAR")
        fields = get_table_fields(filepath)
        new_id = self._next_id("XCHAR")
        rows = self._read("XCHAR")
        max_pos = max((r.get("POSITION", 0) or 0 for r in rows), default=0) + 1
        record = {
            "ID": new_id,
            "NAME": data.get("NAME", ""),
            "POSITION": data.get("POSITION", max_pos),
            "START": data.get("START", 0),
            "END": data.get("END", 0),
            "VALIDITY": data.get("VALIDITY", 0),
            # Kanonische leerzeichengetrennte Maske (Original-Layout); kompakte Eingabe erlaubt.
            "VALIDDAYS": calc.normalize_day_mask(data.get("VALIDDAYS", ""), 7),
            # Festes Datum für VALIDITY=1 (Spec 3.8.2 Nr. 5) — leer bei Wochentagsmodus.
            "DATE": data.get("DATE", ""),
            "HOLRULE": data.get("HOLRULE", 0),
            "HIDE": 1 if data.get("HIDE") else 0,
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return {**record, "id": record["ID"]}

    def update_extracharge(self, xc_id: int, data: dict) -> dict:
        filepath = self._table("XCHAR")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("XCHAR", xc_id)
        if raw_idx is None:
            raise ValueError(f"ExtraCharge {xc_id} not found")
        update_data = {}
        for key in (
            "NAME",
            "POSITION",
            "START",
            "END",
            "VALIDITY",
            "VALIDDAYS",
            "DATE",
            "HOLRULE",
            "HIDE",
        ):
            if key in data:
                update_data[key] = data[key]
        # Wochentagsmaske aufs kanonische leerzeichengetrennte Layout normalisieren.
        if "VALIDDAYS" in update_data:
            update_data["VALIDDAYS"] = calc.normalize_day_mask(update_data["VALIDDAYS"], 7)
        update_record(filepath, fields, raw_idx, update_data)
        return {"id": xc_id, **update_data}

    def delete_extracharge(self, xc_id: int) -> int:
        filepath = self._table("XCHAR")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("XCHAR", xc_id)
        if raw_idx is None:
            return 0
        update_record(filepath, fields, raw_idx, {"HIDE": 1})
        return 1

    # ── Extra Charge Calculation helpers ─────────────────────
    @staticmethod
    def _decode_startend(raw: str) -> str:
        """Decode a UTF-16LE-encoded STARTEND string back to ASCII (e.g. '06:00-14:00')."""
        if not raw:
            return ""
        try:
            decoded = raw.encode("utf-16-le").decode("latin1", errors="replace").strip()
            return decoded.replace("\x00", "").replace("\t", "").strip()
        except Exception:
            return ""

    @staticmethod
    def _time_str_to_minutes(time_str: str) -> int | None:
        """Convert 'HH:MM' to minutes from midnight, or None if invalid."""
        parts = time_str.strip().split(":")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]) * 60 + int(parts[1])
        return None

    @staticmethod
    def _interval_overlap_minutes(
        a_start: int, a_end: int, b_start: int, b_end: int
    ) -> int:
        """Calculate overlap in minutes between two simple (non-wrapping) time intervals."""
        return max(0, min(a_end, b_end) - max(a_start, b_start))

    def get_shift_time_range(
        self, shift: dict, weekday: int
    ) -> tuple[int | None, int | None, float]:
        """Öffentlich: (start_min, end_min, duration_h) einer Schicht am Wochentag.

        Für Konsumenten (z. B. den CalDAV-Export), die Dienstzeiten brauchen,
        ohne das STARTEND-Format (D-31) selbst zu parsen.
        """
        return self._get_shift_time_range(shift, weekday)

    def _get_shift_time_range(
        self, shift: dict, weekday: int
    ) -> tuple[int | None, int | None, float]:
        """Liefert (start_min, end_min, duration_h) einer Schicht am Wochentag."""
        raw = shift.get(f"STARTEND{weekday}", "")
        decoded = self._decode_startend(raw)
        if decoded and "-" in decoded:
            parts = decoded.split("-", 1)
            start_m = self._time_str_to_minutes(parts[0].strip())
            end_m = self._time_str_to_minutes(parts[1].strip())
            if start_m is not None and end_m is not None:
                duration = float(shift.get(f"DURATION{weekday}", 0) or 0)
                return start_m, end_m, duration
        duration = float(shift.get(f"DURATION{weekday}", 0) or 0)
        return None, None, duration

    def calculate_extracharge_hours(
        self,
        year: int | None = None,
        month: int | None = None,
        employee_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict]:
        """
        Calculate surcharge hours per ExtraCharge rule for a month or period.
        Returns a list of dicts: { charge_id, charge_name, hours, shift_count, ... }

        Spec 3.8 über die Berechnungsschicht: VALIDDAYS als Maske geparst,
        Fensterschnitt statt DURATION-Sonderfall (END=0 → 1440), Tagindex 7,
        bis zu 3 Teilfenster, Tageswechsel-Split, VALIDITY=1, halbe Feiertage,
        NOEXTRA (bei 5SPSHI via SHIFTID-Weiche), expandierte 5CYASS.
        ``shift_count`` zählt Mitarbeiter-Tage mit Zuschlagsstunden > 0.

        Auswertungszeitraum (Spec 3.9.1): entweder ``year``+``month`` oder ein
        freier Zeitraum ``date_from``/``date_to`` (ISO-Datum, beide
        erforderlich; hat Vorrang).
        """
        charges = self.get_extracharges(include_hidden=False)
        if not charges:
            return []

        if date_from is not None and date_to is not None:
            von = date.fromisoformat(str(date_from))
            bis = date.fromisoformat(str(date_to))
            if von > bis:
                raise ValueError("date_from muss <= date_to sein")
        elif year is None or month is None:
            raise ValueError(
                "Entweder year+month oder date_from+date_to angeben"
            )
        else:
            von = date(year, month, 1)
            bis = self._last_of_month(year, month)
        inputs = self._calc_inputs(von, bis, employee_id)

        employees = self.get_employees(include_hidden=True)
        if employee_id is not None:
            employees = [e for e in employees if e["ID"] == employee_id]

        charge_acc: dict[int, dict] = {
            c["ID"]: {"hours": 0.0, "count": 0, "charge": c} for c in charges
        }

        for emp in employees:
            eid = emp["ID"]
            plan = self._plan_kwargs(inputs, eid)
            if not (
                plan["manual_shifts"] or plan["cycle_shifts"] or plan["special_shifts"]
            ):
                continue
            ctx = self._calc_context(emp)
            intervals = calc.daily_work_intervals(ctx, von, bis, **plan)
            for c in charges:
                acc = charge_acc[c["ID"]]
                for d, work in intervals.items():
                    hours = calc.extracharge_hours_on_day(
                        c, d, work, holidays=inputs["holidays"]
                    )
                    if hours > calc.EPSILON:
                        acc["hours"] += hours
                        acc["count"] += 1

        return [
            {
                "charge_id": cid,
                "charge_name": acc["charge"]["NAME"],
                "hours": round(acc["hours"], 2),
                "shift_count": acc["count"],
                "start_time": acc["charge"].get("START", 0),
                "end_time": acc["charge"].get("END", 0),
                "validdays": acc["charge"].get("VALIDDAYS", ""),
                "holrule": acc["charge"].get("HOLRULE", 0),
            }
            for cid, acc in charge_acc.items()
        ]

    def extracharge_hours_by_day(
        self,
        year: int | None = None,
        month: int | None = None,
        employee_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict]:
        """Zeitzuschläge je Tag (Spec 3.8): je (Mitarbeiter, Tag, Zuschlag) eine Zeile
        mit Stunden > 0. Nutzt dieselbe Berechnungsschicht wie
        :meth:`calculate_extracharge_hours` (Fensterschnitt, Tageswechsel-Split,
        Tagindex 7, NOEXTRA, expandierte 5CYASS); nur die Aggregation ist je Tag
        statt je Regel. Zeitraum wie dort: ``year``+``month`` oder ``date_from``/
        ``date_to`` (Vorrang)."""
        charges = self.get_extracharges(include_hidden=False)
        if not charges:
            return []

        if date_from is not None and date_to is not None:
            von = date.fromisoformat(str(date_from))
            bis = date.fromisoformat(str(date_to))
            if von > bis:
                raise ValueError("date_from muss <= date_to sein")
        elif year is None or month is None:
            raise ValueError("Entweder year+month oder date_from+date_to angeben")
        else:
            von = date(year, month, 1)
            bis = self._last_of_month(year, month)
        inputs = self._calc_inputs(von, bis, employee_id)

        employees = self.get_employees(include_hidden=True)
        if employee_id is not None:
            employees = [e for e in employees if e["ID"] == employee_id]

        rows: list[dict] = []
        for emp in employees:
            eid = emp["ID"]
            plan = self._plan_kwargs(inputs, eid)
            if not (
                plan["manual_shifts"] or plan["cycle_shifts"] or plan["special_shifts"]
            ):
                continue
            ctx = self._calc_context(emp)
            intervals = calc.daily_work_intervals(ctx, von, bis, **plan)
            for c in charges:
                for d, work in intervals.items():
                    hours = calc.extracharge_hours_on_day(
                        c, d, work, holidays=inputs["holidays"]
                    )
                    if hours > calc.EPSILON:
                        rows.append(
                            {
                                "employee_id": eid,
                                "employee_name": emp.get("NAME", ""),
                                "date": d.isoformat(),
                                "charge_id": c["ID"],
                                "charge_name": c["NAME"],
                                "hours": round(hours, 2),
                            }
                        )
        rows.sort(key=lambda r: (r["employee_id"], r["date"], r["charge_id"]))
        return rows

    # ── Leave Entitlements ────────────────────────────────────
    def get_absences_list(
        self,
        year: int | None = None,
        employee_id: int | None = None,
        leave_type_id: int | None = None,
    ) -> list[dict]:
        """Return all absences, optionally filtered by year/employee/leave type."""
        rows = self._read("ABSEN")
        lt_map = {lt["ID"]: lt for lt in self.get_leave_types(include_hidden=True)}
        result = []
        for r in rows:
            date = r.get("DATE", "")
            if year is not None and not date.startswith(str(year)):
                continue
            if employee_id is not None and r.get("EMPLOYEEID") != employee_id:
                continue
            lt_id = r.get("LEAVETYPID", 0)
            if leave_type_id is not None and lt_id != leave_type_id:
                continue
            lt = lt_map.get(lt_id)
            result.append(
                {
                    "id": r.get("ID"),
                    "employee_id": r.get("EMPLOYEEID"),
                    "date": date,
                    "leave_type_id": lt_id,
                    "leave_type_name": lt.get("NAME", "") if lt else "",
                    "leave_type_short": lt.get("SHORTNAME", "") if lt else "",
                    # Teiltagsmodus (Spec 3.5.2/D-54): 0=ganz, 1=vorm., 2=nachm.,
                    # 3=stundenweise (START/END Minuten ab Mitternacht)
                    "interval": r.get("INTERVAL", 0) or 0,
                    "start_time": r.get("START", 0) or 0,
                    "end_time": r.get("END", 0) or 0,
                }
            )
        result.sort(key=lambda x: x.get("date", ""))
        return result

    def get_all_group_assignments(self) -> list[dict]:
        """Return all group assignments (employee_id, group_id pairs)."""
        rows = self._read("GRASG")
        return [
            {"employee_id": r.get("EMPLOYEEID"), "group_id": r.get("GROUPID")}
            for r in rows
        ]

    def get_leave_entitlements(
        self, year: int | None = None, employee_id: int | None = None
    ) -> list[dict]:
        """Read leave entitlements from 5LEAEN.DBF."""
        rows = self._read("LEAEN")
        lt_map = {lt["ID"]: lt for lt in self.get_leave_types(include_hidden=True)}

        result = []
        for r in rows:
            if year is not None and r.get("YEAR") != year:
                continue
            if employee_id is not None and r.get("EMPLOYEEID") != employee_id:
                continue
            lt_id = r.get("LEAVETYPID", 0)
            lt = lt_map.get(lt_id)
            result.append(
                {
                    "id": r.get("ID"),
                    "employee_id": r.get("EMPLOYEEID"),
                    "year": r.get("YEAR"),
                    "leave_type_id": lt_id,
                    "leave_type_name": lt.get("NAME", "") if lt else "",
                    "entitlement": float(r.get("ENTITLEMNT", 0) or 0),
                    "carry_forward": float(r.get("REST", 0) or 0),
                    "in_days": bool(r.get("INDAYS", 1)),
                }
            )
        return result

    def set_leave_entitlement(
        self,
        employee_id: int,
        year: int,
        days: float,
        carry_forward: float = 0,
        leave_type_id: int = 0,
        in_days: bool = True,
    ) -> dict:
        """Create or update a leave entitlement record.

        ENTITLEMNT/REST sind Gleitkommawerte (Spec 3.7, F-Felder) —
        Halbtage wie 12,5 bleiben erhalten.
        """
        filepath = self._table("LEAEN")
        fields = get_table_fields(filepath)

        # Read max ID before deletion
        all_records = read_dbf(filepath)
        max_id = max((r.get("ID", 0) or 0 for r in all_records), default=0)

        # Bestehende Einträge zu MA+Jahr(+Abwesenheitsart) entfernen
        existing = find_all_records(filepath, fields, EMPLOYEEID=employee_id, YEAR=year)
        for idx, rec in existing:
            if leave_type_id == 0 or rec.get("LEAVETYPID", 0) == leave_type_id:
                delete_record(filepath, fields, idx)

        new_id = max_id + 1
        record = {
            "ID": new_id,
            "EMPLOYEEID": employee_id,
            "YEAR": year,
            "LEAVETYPID": leave_type_id,
            "ENTITLEMNT": float(days),
            "REST": float(carry_forward),
            "INDAYS": 1 if in_days else 0,
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return {
            "id": record["ID"],
            "employee_id": employee_id,
            "year": year,
            "leave_type_id": leave_type_id,
            "entitlement": days,
            "carry_forward": carry_forward,
            "in_days": in_days,
        }

    def get_leave_balance(self, employee_id: int, year: int) -> dict:
        """Calculate leave balance: entitlement + carry_forward - used = remaining.

        Je anspruchsverbundener Art (ENTITLED=1) über calculations.leave_account
        berechnet (Spec 3.7.1: Verbrauch nach 3.5.2/3.5.3 mit CHARGETYP/
        CHARGEHRS/COUNTALL und INTERVAL-Halbtagen) und über die Arten summiert.
        Ohne 5LEAEN-Satz gibt es keinen Default-Anspruch (das Original zeigt
        dann keinen Anspruch).
        """
        emp = self.get_employee(employee_id)
        ctx = (
            self._calc_context(emp)
            if emp
            else calc.EmployeeContext(workdays=(False,) * 8)
        )
        holidays = self._calc_holidays()
        leaen_rows = [
            r for r in self._read("LEAEN") if r.get("EMPLOYEEID") == employee_id
        ]
        absences = [
            r for r in self._read("ABSEN") if r.get("EMPLOYEEID") == employee_id
        ]

        total_entitlement = total_carry = used = 0.0
        by_type = []
        for lt in self.get_leave_types(include_hidden=True):
            if not lt.get("ENTITLED"):
                continue
            acct = calc.leave_account(
                ctx,
                year,
                lt,
                holidays=holidays,
                entitlements=leaen_rows,
                absences=absences,
            )
            total_entitlement += acct.entitlement
            total_carry += acct.rest
            used += acct.taken
            # Spec 3.9.3 Nr. 6: Doppelwert genommen/verbleibend je Art
            by_type.append(
                {
                    "leave_type_id": lt["ID"],
                    "leave_type_name": lt.get("NAME", ""),
                    "leave_type_short": lt.get("SHORTNAME", ""),
                    "entitlement": acct.entitlement,
                    "carry_forward": acct.rest,
                    "total": acct.total,
                    "used": acct.taken,
                    "remaining": acct.remaining,
                }
            )

        remaining = total_entitlement + total_carry - used
        forfeiture_date = f"{year}-12-31"

        return {
            "employee_id": employee_id,
            "year": year,
            "entitlement": total_entitlement,
            "carry_forward": total_carry,
            "total": total_entitlement + total_carry,
            "used": used,
            "remaining": remaining,
            "by_type": by_type,
            "forfeiture_date": forfeiture_date,
            "has_custom_entitlement": any(
                r.get("YEAR") == year for r in leaen_rows
            ),
        }

    def get_leave_balance_group(self, year: int, group_id: int) -> list[dict]:
        """Liefert den Urlaubssaldo aller Mitarbeiter einer Gruppe."""
        member_ids = self.get_group_members(group_id)
        emp_map = {e["ID"]: e for e in self.get_employees(include_hidden=True)}
        result = []
        for eid in member_ids:
            emp = emp_map.get(eid)
            if not emp:
                continue
            balance = self.get_leave_balance(eid, year)
            balance["employee_name"] = (
                f"{emp.get('NAME', '')}, {emp.get('FIRSTNAME', '')}".strip(", ")
            )
            balance["employee_number"] = emp.get("NUMBER", "")
            result.append(balance)
        result.sort(key=lambda x: x.get("employee_name", ""))
        return result

    # ── Holiday Bans ──────────────────────────────────────────
    def get_holiday_bans(self, group_id: int | None = None) -> list[dict]:
        """Read holiday bans from 5HOBAN.DBF."""
        rows = self._read("HOBAN")
        groups_map = {g["ID"]: g for g in self.get_groups(include_hidden=True)}
        result = []
        for r in rows:
            if group_id is not None and r.get("GROUPID") != group_id:
                continue
            gid = r.get("GROUPID", 0)
            grp = groups_map.get(gid)
            result.append(
                {
                    "id": r.get("ID"),
                    "group_id": gid,
                    "group_name": grp.get("NAME", "") if grp else "",
                    "start_date": r.get("START", ""),
                    "end_date": r.get("END", ""),
                    "restrict": r.get("RESTRICT", 1),
                    "reason": r.get("DESCRIPT", ""),
                }
            )
        result.sort(key=lambda x: x.get("start_date", ""))
        return result

    def create_holiday_ban(
        self, group_id: int, start_date: str, end_date: str, reason: str = ""
    ) -> dict:
        """Create a holiday ban entry."""
        filepath = self._table("HOBAN")
        fields = get_table_fields(filepath)
        existing = read_dbf(filepath)
        max_id = max((r.get("ID", 0) or 0 for r in existing), default=0)
        new_id = max_id + 1
        record = {
            "ID": new_id,
            "GROUPID": group_id,
            "START": start_date,
            "END": end_date,
            "RESTRICT": 1,
            "DESCRIPT": reason[:200] if reason else "",
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return {
            "id": record["ID"],
            "group_id": group_id,
            "start_date": start_date,
            "end_date": end_date,
            "restrict": 1,
            "reason": reason,
        }

    def delete_holiday_ban(self, ban_id: int) -> int:
        """Löscht eine Urlaubssperre. Liefert die Zahl der gelöschten Sätze."""
        filepath = self._table("HOBAN")
        fields = get_table_fields(filepath)
        matches = find_all_records(filepath, fields, ID=ban_id)
        count = 0
        for idx, _ in matches:
            delete_record(filepath, fields, idx)
            count += 1
        return count

    def update_holiday_ban(self, ban_id: int, data: dict) -> dict | None:
        """Update a holiday ban (5HOBAN) in place. Only provided keys change
        (``group_id``, ``start_date``, ``end_date``, ``reason``). Returns the
        updated record dict, or None if no ban has that ID."""
        filepath = self._table("HOBAN")
        fields = get_table_fields(filepath)
        raw_idx, record = self._find_record("HOBAN", ban_id)
        if raw_idx is None:
            return None
        update_data: dict = {}
        if "group_id" in data:
            update_data["GROUPID"] = data["group_id"]
        if "start_date" in data:
            update_data["START"] = data["start_date"]
        if "end_date" in data:
            update_data["END"] = data["end_date"]
        if "reason" in data:
            update_data["DESCRIPT"] = (data["reason"] or "")[:200]
        if update_data:
            update_record(filepath, fields, raw_idx, update_data)
        return {
            "id": ban_id,
            "group_id": update_data.get("GROUPID", record.get("GROUPID")),
            "start_date": update_data.get("START", record.get("START", "")),
            "end_date": update_data.get("END", record.get("END", "")),
            "restrict": record.get("RESTRICT", 1),
            "reason": update_data.get("DESCRIPT", record.get("DESCRIPT", "")),
        }

    # ── Annual Close ──────────────────────────────────────────
    def _annual_close_employee(
        self, emp: dict, year: int, keep_entitlements: bool = False
    ) -> tuple[dict, list[dict]]:
        """Jahresabschluss-Kennzahlen und 5LEAEN-Zeilen (year+1) eines MA.

        Spec 3.7.2 über calculations.annual_close: je Abwesenheitsart mit
        ENTITLED=1 (Übertrag nur bei CARRYFWD=1), restNeu = gesamt − verbraucht
        ohne Cap, ENTITLEMNT-Vorbelegung aus vorhandenem Folgejahres-Wert bzw.
        STDENTIT. Verbrauch nach 3.5.2/3.5.3 (Halbtage zählen 0,5).
        ``keep_entitlements`` = Dialog-Option "Urlaubsansprüche bleiben im
        Folgejahr gleich" (R6.8-4).
        """
        eid = emp["ID"]
        ctx = self._calc_context(emp)
        holidays = self._calc_holidays()
        leave_types = self.get_leave_types(include_hidden=True)
        leaen_rows = [r for r in self._read("LEAEN") if r.get("EMPLOYEEID") == eid]
        absences = [r for r in self._read("ABSEN") if r.get("EMPLOYEEID") == eid]

        new_rows = calc.annual_close(
            ctx,
            year,
            holidays=holidays,
            leave_types=leave_types,
            entitlements=leaen_rows,
            absences=absences,
            keep_entitlements=keep_entitlements,
        )
        carried_by_type = {int(r["LEAVETYPID"]): float(r["REST"]) for r in new_rows}

        entitlement = carry_in = used = remaining = 0.0
        carried = forfeited = 0.0
        for lt in leave_types:
            if not lt.get("ENTITLED"):
                continue
            acct = calc.leave_account(
                ctx,
                year,
                lt,
                holidays=holidays,
                entitlements=leaen_rows,
                absences=absences,
            )
            entitlement += acct.entitlement
            carry_in += acct.rest
            used += acct.taken
            remaining += acct.remaining
            lt_id = int(lt.get("ID") or 0)
            if lt_id in carried_by_type and lt.get("CARRYFWD"):
                carried += carried_by_type[lt_id]
            else:  # ENTITLED ohne CARRYFWD: Rest verfällt (Spec 3.7.2) —
                # auch mit keep_entitlements (dann steht die Art mit REST=0
                # in den neuen Zeilen)
                forfeited += max(0.0, acct.remaining)

        summary = {
            "employee_id": eid,
            "employee_name": f"{emp.get('NAME', '')}, {emp.get('FIRSTNAME', '')}".strip(
                ", "
            ),
            "entitlement": entitlement,
            "carry_forward_in": carry_in,
            "total": entitlement + carry_in,
            "used": used,
            "remaining": remaining,
            "proposed_carry_forward": carried,
            "forfeited": forfeited,
        }
        return summary, new_rows

    def get_annual_close_preview(
        self,
        year: int,
        group_id: int | None = None,
        carry_forward_days: float = 10,
        keep_entitlements: bool = False,
    ) -> dict:
        """Preview annual close without saving changes.

        ``carry_forward_days`` bleibt aus API-Kompatibilität erhalten, wird
        aber ignoriert: das Original überträgt ungedeckelt gesamt − verbraucht
        je Art (Spec 3.7.2, kein Cap). ``keep_entitlements`` = Dialog-Option
        "Urlaubsansprüche bleiben im Folgejahr gleich" (R6.8-4).
        """
        employees = self.get_employees(include_hidden=False)
        if group_id is not None:
            member_ids = set(self.get_group_members(group_id))
            employees = [e for e in employees if e["ID"] in member_ids]

        details = []
        total_carry = 0.0
        total_forfeited = 0.0

        for emp in employees:
            summary, _rows = self._annual_close_employee(
                emp, year, keep_entitlements=keep_entitlements
            )
            total_carry += summary["proposed_carry_forward"]
            total_forfeited += summary["forfeited"]
            details.append(summary)

        return {
            "year": year,
            "next_year": year + 1,
            "carry_forward_limit": carry_forward_days,
            "employee_count": len(employees),
            "total_carry_forward": total_carry,
            "total_forfeited": total_forfeited,
            "details": details,
        }

    def check_annual_close_exists(
        self, year: int, group_id: int | None = None
    ) -> bool:
        """Prüft, ob der Jahresabschluss für Jahr+1 schon Daten hat (Idempotenz-Guard)."""
        employees = self.get_employees(include_hidden=False)
        if group_id is not None:
            member_ids = set(self.get_group_members(group_id))
            employees = [e for e in employees if e["ID"] in member_ids]
        next_year = year + 1
        for emp in employees[:3]:  # check first few employees only
            rows = self._read("LEAEN")
            for r in rows:
                if r.get("EMPLOYEEID") == emp["ID"] and r.get("YEAR") == next_year:
                    return True
        return False

    def run_annual_close(
        self,
        year: int,
        group_id: int | None = None,
        carry_forward_days: float = 10,
        keep_entitlements: bool = False,
    ) -> dict:
        """
        Run annual close: write the 5LEAEN rows for year+1 per leave type.

        Spec 3.7.2 über calculations.annual_close: je Art fortgeschrieben
        (kein Kollabieren auf LEAVETYPID=0), Übertrag nur bei CARRYFWD=1 und
        ungedeckelt (``carry_forward_days`` wird aus API-Kompatibilität
        akzeptiert, aber ignoriert), Float-Ansprüche bleiben erhalten.
        ``keep_entitlements`` = Dialog-Option "Urlaubsansprüche bleiben im
        Folgejahr gleich" (R6.8-4): ENTITLEMNT des abgeschlossenen Jahres wird
        ins Folgejahr kopiert, und auch Arten ohne CARRYFWD werden (mit REST=0)
        fortgeschrieben.
        Returns summary with { processed, total_carry_forward, total_forfeited, already_existed }.
        NOTE: Not fully atomic — partial failures may leave some employees processed and others not.
        """
        already_existed = self.check_annual_close_exists(year, group_id)
        employees = self.get_employees(include_hidden=False)
        if group_id is not None:
            member_ids = set(self.get_group_members(group_id))
            employees = [e for e in employees if e["ID"] in member_ids]

        processed = 0
        total_carry = 0.0
        total_forfeited = 0.0
        details = []

        for emp in employees:
            eid = emp["ID"]
            summary, new_rows = self._annual_close_employee(
                emp, year, keep_entitlements=keep_entitlements
            )

            for row in new_rows:
                self.set_leave_entitlement(
                    employee_id=eid,
                    year=year + 1,
                    days=float(row["ENTITLEMNT"]),
                    carry_forward=float(row["REST"]),
                    leave_type_id=int(row["LEAVETYPID"]),
                    in_days=bool(row["INDAYS"]),
                )

            total_carry += summary["proposed_carry_forward"]
            total_forfeited += summary["forfeited"]
            processed += 1
            details.append(
                {
                    "employee_id": eid,
                    "employee_name": summary["employee_name"],
                    "remaining": summary["remaining"],
                    "carry_forward": summary["proposed_carry_forward"],
                    "forfeited": summary["forfeited"],
                }
            )

        return {
            "year": year,
            "next_year": year + 1,
            "processed": processed,
            "total_carry_forward": total_carry,
            "total_forfeited": total_forfeited,
            "already_existed": already_existed,
            "details": details,
        }

    def forfeit_rest(
        self,
        cutoff_date: str,
        group_id: int | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Resturlaub zum Stichtag verfallen lassen (Spec 3.7.3 / Dialog 5.17).

        Je Mitarbeiter und anspruchsverbundener Art (ENTITLED=1) wird REST des
        Stichtagsjahres auf den Verbrauch bis einschließlich Stichtag gekürzt
        (calculations.forfeit_rest: nur kürzen, nie erhöhen; ENTITLEMNT bleibt
        unberührt). ``dry_run=True`` liefert die Kürzungen als Vorschau, ohne
        5LEAEN zu schreiben; sonst werden die betroffenen 5LEAEN-Sätze über
        den dbf_writer aktualisiert (Journal/CDX automatisch).
        """
        cutoff = date.fromisoformat(str(cutoff_date))
        year = cutoff.year

        employees = self.get_employees(include_hidden=False)
        if group_id is not None:
            member_ids = set(self.get_group_members(group_id))
            employees = [e for e in employees if e["ID"] in member_ids]

        holidays = self._calc_holidays()
        leave_types = self.get_leave_types(include_hidden=True)
        lt_map = {lt["ID"]: lt for lt in leave_types}
        leaen_by_emp: dict[int, list[dict]] = {}
        for r in self._read("LEAEN"):
            leaen_by_emp.setdefault(r.get("EMPLOYEEID"), []).append(r)
        absen_by_emp: dict[int, list[dict]] = {}
        for r in self._read("ABSEN"):
            absen_by_emp.setdefault(r.get("EMPLOYEEID"), []).append(r)

        filepath = self._table("LEAEN")
        fields = get_table_fields(filepath) if not dry_run else None

        cuts = []
        total_forfeited = 0.0
        for emp in employees:
            eid = emp["ID"]
            ctx = self._calc_context(emp)
            leaen_rows = leaen_by_emp.get(eid, [])
            rows = calc.forfeit_rest(
                ctx,
                cutoff,
                holidays=holidays,
                leave_types=leave_types,
                entitlements=leaen_rows,
                absences=absen_by_emp.get(eid, []),
            )
            for row in rows:
                lt_id = int(row["LEAVETYPID"])
                old_rest = next(
                    (
                        float(r.get("REST") or 0.0)
                        for r in leaen_rows
                        if r.get("YEAR") == year and r.get("LEAVETYPID") == lt_id
                    ),
                    0.0,
                )
                new_rest = float(row["REST"])
                if not dry_run:
                    matches = find_all_records(
                        filepath, fields, EMPLOYEEID=eid, YEAR=year, LEAVETYPID=lt_id
                    )
                    for raw_idx, _rec in matches:
                        update_record(filepath, fields, raw_idx, {"REST": new_rest})
                lt = lt_map.get(lt_id)
                total_forfeited += old_rest - new_rest
                cuts.append(
                    {
                        "employee_id": eid,
                        "employee_name": f"{emp.get('NAME', '')}, {emp.get('FIRSTNAME', '')}".strip(
                            ", "
                        ),
                        "leave_type_id": lt_id,
                        "leave_type_name": lt.get("NAME", "") if lt else "",
                        "year": year,
                        "old_rest": old_rest,
                        "new_rest": new_rest,
                        "forfeited": round(old_rest - new_rest, 4),
                    }
                )

        if not dry_run and cuts:
            self._invalidate_cache("LEAEN")

        return {
            "cutoff_date": cutoff.isoformat(),
            "year": year,
            "group_id": group_id,
            "dry_run": dry_run,
            "employees_processed": len(employees),
            "cuts": cuts,
            "total_forfeited": round(total_forfeited, 4),
        }

    # ── Zeitkonto / Überstunden ───────────────────────────────

    def get_overtime_records(
        self, year: int | None = None, employee_id: int | None = None
    ) -> list[dict]:
        """Return records from 5OVER.DBF (manual overtime adjustments)."""
        rows = self._read("OVER")
        result = []
        year_str = str(year) if year else None
        for r in rows:
            if employee_id is not None and r.get("EMPLOYEEID") != employee_id:
                continue
            d = r.get("DATE", "")
            if year_str and (not d or not d.startswith(year_str)):
                continue
            result.append(
                {
                    "id": r.get("ID"),
                    "employee_id": r.get("EMPLOYEEID"),
                    "date": d,
                    "hours": float(r.get("HOURS", 0) or 0),
                }
            )
        return result

    def get_bookings(
        self,
        year: int | None = None,
        month: int | None = None,
        employee_id: int | None = None,
    ) -> list[dict]:
        """Return manual time bookings from 5BOOK.DBF."""
        rows = self._read("BOOK")
        result = []
        year_str = str(year) if year else None
        prefix = f"{year:04d}-{month:02d}" if (year and month) else None
        for r in rows:
            if employee_id is not None and r.get("EMPLOYEEID") != employee_id:
                continue
            d = r.get("DATE", "")
            if prefix and (not d or not d.startswith(prefix)):
                continue
            elif year_str and not prefix and (not d or not d.startswith(year_str)):
                continue
            result.append(
                {
                    "id": r.get("ID"),
                    "employee_id": r.get("EMPLOYEEID"),
                    "date": d,
                    "type": r.get("TYPE", 0),
                    "value": float(r.get("VALUE", 0) or 0),
                    "note": r.get("NOTE", ""),
                }
            )
        result.sort(key=lambda x: x.get("date", ""))
        return result

    def create_booking(
        self,
        employee_id: int,
        date_str: str,
        booking_type: int,
        value: float,
        note: str = "",
    ) -> dict:
        """Append a new manual booking to 5BOOK.DBF."""
        filepath = self._table("BOOK")
        fields = get_table_fields(filepath)
        existing = read_dbf(filepath)
        max_id = max((r.get("ID", 0) or 0 for r in existing), default=0)
        new_id = max_id + 1
        record = {
            "ID": new_id,
            "EMPLOYEEID": employee_id,
            "DATE": date_str,
            "TYPE": booking_type,
            "VALUE": value,
            "NOTE": (note or "")[:200],
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return {
            "id": record["ID"],
            "employee_id": employee_id,
            "date": date_str,
            "type": booking_type,
            "value": value,
            "note": note,
        }

    def update_booking(
        self,
        booking_id: int,
        date_str: str | None = None,
        booking_type: int | None = None,
        value: float | None = None,
        note: str | None = None,
    ) -> dict | None:
        """Update a manual booking in 5BOOK.DBF. Only provided fields change.

        Returns the updated record dict, or None if no booking has that ID.
        """
        filepath = self._table("BOOK")
        fields = get_table_fields(filepath)
        raw_idx, record = self._find_record("BOOK", booking_id)
        if raw_idx is None:
            return None
        update_data: dict = {}
        if date_str is not None:
            update_data["DATE"] = date_str
        if booking_type is not None:
            update_data["TYPE"] = booking_type
        if value is not None:
            update_data["VALUE"] = value
        if note is not None:
            update_data["NOTE"] = (note or "")[:200]
        if update_data:
            update_record(filepath, fields, raw_idx, update_data)
        return {
            "id": booking_id,
            "employee_id": record.get("EMPLOYEEID"),
            "date": update_data.get("DATE", record.get("DATE")),
            "type": update_data.get("TYPE", record.get("TYPE")),
            "value": update_data.get("VALUE", record.get("VALUE")),
            "note": update_data.get("NOTE", record.get("NOTE")),
        }

    def delete_booking(self, booking_id: int) -> int:
        """Delete a booking from 5BOOK.DBF by ID. Returns 1 if deleted, 0 if not found."""
        filepath = self._table("BOOK")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("BOOK", booking_id)
        if raw_idx is None:
            return 0
        delete_record(filepath, fields, raw_idx)
        return 1

    # ── Carry Forward (Saldo-Übertrag) ────────────────────────
    # Entscheidung (Spec 3.6): Das Original kennt KEINEN Stundenkonten-
    # Jahresabschluss (3.6.2 Nr. 6); ein Saldo-Übertrag ins Folgejahr ist
    # manuell als 5BOOK-Buchung TYPE 0/1 mit Datum im neuen Jahr abzubilden.
    # 5BOOK TYPE=2 ist ausschließlich das Überstundenkonto des Schwester-
    # produkts Win-Fehlzeiten (3.6.1/3.6.3) und fließt nie in Ist/Soll/Saldo.
    # Die lib bildet den Übertrag daher als TYPE=0-Buchung (Iststundenkonto)
    # am 1.1. des Jahres ab, erkennbar am NOTE-Präfix "Jahresübertrag".
    _CARRY_FORWARD_NOTE = "Jahresübertrag"

    def get_carry_forward(self, employee_id: int, year: int) -> dict:
        """Liest die Übertrags-Buchung zu MA+Jahr aus 5BOOK.DBF.

        Spec-konform ist der Übertrag eine TYPE=0-Buchung am 1.1. des Jahres
        mit NOTE-Präfix "Jahresübertrag" (siehe Blockkommentar oben); sie
        wirkt dadurch regulär über GetActualHours auf den Saldo.
        """
        target_date = f"{year}-01-01"
        for r in self._read("BOOK"):
            if (
                r.get("EMPLOYEEID") == employee_id
                and int(r.get("TYPE") or 0) == calc.BOOKING_ACTUAL
                and r.get("DATE") == target_date
                and str(r.get("NOTE") or "").startswith(self._CARRY_FORWARD_NOTE)
            ):
                return {
                    "employee_id": employee_id,
                    "year": year,
                    "hours": float(r.get("VALUE", 0) or 0),
                    "booking_id": r.get("ID"),
                }
        return {
            "employee_id": employee_id,
            "year": year,
            "hours": 0.0,
            "booking_id": None,
        }

    def set_carry_forward(self, employee_id: int, year: int, hours: float) -> dict:
        """Setzt den Übertrag zu MA+Jahr als TYPE=0-Buchung am 1.1.

        Ersetzt eine vorhandene Übertrags-Buchung des Jahres; räumt dabei auch
        Altbestände auf, die eine frühere lib-Version als TYPE=2 geschrieben
        hat (erkennbar am NOTE-Präfix — fremde TYPE=2-Buchungen, d. h. echte
        Überstundenkonto-Buchungen von Win-Fehlzeiten, bleiben unangetastet).
        """
        filepath = self._table("BOOK")
        fields = get_table_fields(filepath)
        year_str = str(year)
        # Bestehende Übertrags-Buchungen des Jahres löschen (TYPE=0 neu und
        # TYPE=2 alt, beide am NOTE-Marker erkennbar).
        existing = read_dbf(filepath)
        for r in existing:
            if r.get("EMPLOYEEID") != employee_id:
                continue
            if not str(r.get("NOTE") or "").startswith(self._CARRY_FORWARD_NOTE):
                continue
            d = r.get("DATE", "")
            if d and d.startswith(year_str):
                rec_id = r.get("ID")
                if rec_id is not None:
                    raw_idx, _ = self._find_record("BOOK", rec_id)
                    if raw_idx is not None:
                        delete_record(filepath, fields, raw_idx)
        # Append new carry-forward (Iststundenkonto, Spec 3.6.2 Nr. 6)
        existing2 = read_dbf(filepath)
        max_id = max((r.get("ID", 0) or 0 for r in existing2), default=0)
        new_id = max_id + 1
        date_str = f"{year}-01-01"
        record = {
            "ID": new_id,
            "EMPLOYEEID": employee_id,
            "DATE": date_str,
            "TYPE": calc.BOOKING_ACTUAL,
            "VALUE": hours,
            "NOTE": f"{self._CARRY_FORWARD_NOTE} {year}",
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return {
            "employee_id": employee_id,
            "year": year,
            "hours": hours,
            "booking_id": record["ID"],
        }

    def calculate_annual_statement(self, employee_id: int, year: int) -> dict:
        """
        Calculate annual saldo and return carry-forward for next year.
        saldo = total_saldo (Ist − Soll, Spec 3.6.2) ohne den bereits als
        TYPE=0-Buchung enthaltenen Jahresübertrag (carry_in).
        """
        balance = self.calculate_time_balance(employee_id, year)
        if not balance:
            return {
                "saldo": 0.0,
                "should_carry": False,
                "employee_id": employee_id,
                "year": year,
            }
        # Die bestehende Übertrags-Buchung aus der Berechnung ausnehmen
        cf = self.get_carry_forward(employee_id, year)
        carry_in = cf["hours"]
        total_saldo = balance.get("total_saldo", 0.0)
        # carry_in aus dem Saldo herausrechnen (steckt in den TYPE=0-Ist-Buchungen)
        net_saldo = round(total_saldo - carry_in, 2)
        return {
            "employee_id": employee_id,
            "year": year,
            "saldo": net_saldo,
            "carry_in": carry_in,
            "total_saldo": total_saldo,
            "should_carry": net_saldo != 0.0,
            "next_year": year + 1,
        }

    def calculate_time_balance(self, employee_id: int, year: int) -> dict:
        """
        Calculate Soll vs Ist hours and saldo for one employee for a full year.

        Spec 3.3/3.4/3.6 über die Berechnungsschicht:
        - actual_hours = GetActualHours (Arbeitszeit inkl. expandierter 5CYASS,
          Abwesenheits-Anrechnung nach 3.5, Ist-Buchungen 5BOOK TYPE=0).
        - target_hours = GetNominalHours (CALCBASE-Dispatcher, Soll-Buchungen
          5BOOK TYPE=1).
        - saldo = Ist − Soll (Spec 3.6.2); die Buchungstypen wirken bereits in
          den Operanden, nicht als separater Zuschlag.
        - 5OVER und 5BOOK TYPE=2 sind das Überstundenkonto (Spec 3.6.3) und
          fließen NICHT in den Saldo; sie werden nur informativ als
          overtime_adjustment (5OVER) / booking_adjustment (TYPE=2) und im
          Jahreswert total_overtime_account (GetOvertimeHours) ausgewiesen.
        Returns monthly breakdown + totals.
        """
        emp = self.get_employee(employee_id)
        if not emp:
            return {}
        von = date(year, 1, 1)
        bis = date(year, 12, 31)
        inputs = self._calc_inputs(von, bis, employee_id)
        return self._time_balance_from_inputs(emp, year, inputs)

    def _time_balance_from_inputs(self, emp: dict, year: int, inputs: dict) -> dict:
        """Monats-Bilanz eines Mitarbeiters aus bereits gebauten _calc_inputs.

        Trennt die Berechnung von der Eingaben-Beschaffung, damit get_zeitkonto
        die Eingaben EINMAL für alle Mitarbeiter bauen kann (ein Scan je
        Bewegungstabelle statt einer pro Mitarbeiter) und sie hier nur je
        Mitarbeiter zerschneidet. Ergebnis identisch zur Einzelberechnung.
        """
        employee_id = emp["ID"]
        ctx = self._calc_context(emp)
        von = date(year, 1, 1)
        bis = date(year, 12, 31)
        plan = self._plan_kwargs(inputs, employee_id)
        absences = inputs["absences"].get(employee_id, [])
        bookings = inputs["bookings"].get(employee_id, [])
        overtimes = inputs["overtimes"].get(employee_id, [])
        lt_map = inputs["leave_types_by_id"]

        total_target = 0.0
        total_actual = 0.0
        total_overtime_adj = 0.0
        total_booking_adj = 0.0
        months_list = []
        running_saldo = 0.0

        for m in range(1, 13):
            m_von = date(year, m, 1)
            m_bis = self._last_of_month(year, m)
            lo, hi = m_von.isoformat(), m_bis.isoformat()
            target = round(
                calc.get_nominal_hours(
                    ctx, m_von, m_bis, holidays=inputs["holidays"], bookings=bookings
                ),
                2,
            )
            actual = round(
                calc.get_actual_hours(
                    ctx,
                    m_von,
                    m_bis,
                    absences=absences,
                    leave_types_by_id=lt_map,
                    bookings=bookings,
                    **plan,
                ),
                2,
            )
            overtime_adj = round(
                sum(
                    float(r.get("HOURS") or 0.0)
                    for r in overtimes
                    if lo <= (r.get("DATE") or "") <= hi
                ),
                2,
            )
            booking_adj = round(
                calc.booking_sum(bookings, calc.BOOKING_OVERTIME, m_von, m_bis), 2
            )
            absence_days = sum(
                1 for r in absences if lo <= (r.get("DATE") or "") <= hi
            )

            diff = round(actual - target, 2)
            running_saldo += diff
            months_list.append(
                {
                    "month": m,
                    "target_hours": target,
                    "actual_hours": actual,
                    "overtime_adjustment": overtime_adj,
                    "booking_adjustment": booking_adj,
                    "absence_days": absence_days,
                    "difference": diff,
                    # Überstundenkonto-Bewegung des Monats (nicht im Saldo)
                    "adjustment": round(overtime_adj + booking_adj, 2),
                    "saldo": diff,
                    "running_saldo": round(running_saldo, 2),
                }
            )
            total_target += target
            total_actual += actual
            total_overtime_adj += overtime_adj
            total_booking_adj += booking_adj

        total_diff = round(total_actual - total_target, 2)
        overtime_account = calc.get_overtime_hours(
            ctx,
            von,
            bis,
            holidays=inputs["holidays"],
            bookings=bookings,
            overtimes=overtimes,
            absences=absences,
            leave_types_by_id=lt_map,
        )

        return {
            "employee_id": employee_id,
            "employee_name": f"{emp.get('NAME', '')}, {emp.get('FIRSTNAME', '')}".strip(
                ", "
            ),
            "employee_short": emp.get("SHORTNAME", ""),
            "year": year,
            "total_target_hours": round(total_target, 2),
            "total_actual_hours": round(total_actual, 2),
            "total_difference": total_diff,
            "total_adjustment": round(total_overtime_adj + total_booking_adj, 2),
            "total_saldo": total_diff,
            "total_overtime_account": round(overtime_account, 2),
            "months": months_list,
        }

    def get_zeitkonto(
        self,
        year: int,
        employee_id: int | None = None,
        group_id: int | None = None,
    ) -> list[dict]:
        """Liefert die Zeitkonto-Übersicht aller MA (oder gruppen-/MA-gefiltert)."""
        employees = self.get_employees(include_hidden=False)
        if group_id is not None:
            member_ids = set(self.get_group_members(group_id))
            employees = [e for e in employees if e["ID"] in member_ids]
        if employee_id is not None:
            employees = [e for e in employees if e["ID"] == employee_id]

        von = date(year, 1, 1)
        bis = date(year, 12, 31)
        inputs = self._calc_inputs(von, bis, None)

        result = []
        for emp in employees:
            balance = self._time_balance_from_inputs(emp, year, inputs)
            if balance:
                result.append(
                    {
                        "employee_id": emp["ID"],
                        "employee_name": balance["employee_name"],
                        "employee_short": balance["employee_short"],
                        "total_target_hours": balance["total_target_hours"],
                        "total_actual_hours": balance["total_actual_hours"],
                        "total_difference": balance["total_difference"],
                        "total_adjustment": balance["total_adjustment"],
                        "total_saldo": balance["total_saldo"],
                    }
                )
        result.sort(key=lambda x: x.get("employee_name", ""))
        return result

    # ── Schedule Conflicts ────────────────────────────────────

    def get_schedule_conflicts(
        self, year: int, month: int, group_id: int | None = None
    ) -> list:
        """
        Detect conflicts for a given month:
          - shift_and_absence: employee has both a MASHI/SPSHI entry AND an ABSEN entry on the same day
          - holiday_ban: employee has an ABSEN entry on a day within a HOBAN period for their group(s)
          - holiday_shift: employee has a shift on a public holiday (severity: warning)

        (No „long_shift" check: a rigid hours threshold is wrong — long shifts such as
        12-hour rosters are normal in many settings and not a conflict; the original
        has no such check either.)

        Konflikte werden ausschließlich auf der **Ist-Ebene** ermittelt: Sollplan-
        Einträge (5MASHI.TYPE=1, Spec 4.12/D-58) sind geplante Zielvorgaben und werden
        ausgeschlossen. Eine Soll-Schicht, die mit einer Ist-Abwesenheit (Krankenstand)
        oder einer Ist-Schicht am selben Tag zusammentrifft, ist die normale Soll-/Ist-
        Zwei-Ebenen-Ansicht und KEIN Konflikt — das Original-Schichtplaner5 kennt zwischen
        den beiden Ebenen gar keine Konfliktprüfung (eigenständige, unabhängige Ebenen).

        Note: Hidden employees (HIDE=True) are intentionally excluded from conflict detection.
        This matches the Schichtplaner5 convention where hidden employees are archived/inactive
        and should not generate operational warnings.

        Returns list of dicts with keys: employee_id, date, type, severity, message
        """
        prefix = f"{year:04d}-{month:02d}"
        conflicts = []

        # Get relevant employees (hidden employees intentionally excluded — see docstring)
        employees_list = self.get_employees(include_hidden=False)
        emp_map = {e["ID"]: e for e in employees_list}

        if group_id is not None:
            member_ids = set(self.get_group_members(group_id))
        else:
            member_ids = set(emp_map.keys())

        # Lookup-Maps für Schichtnamen und Abwesenheitsarten bauen
        shift_name_map: dict = {}  # {shift_id: shift_name}
        for r in self._read("SHIFT"):
            sid = r.get("ID")
            if sid:
                shift_name_map[sid] = r.get("NAME", r.get("SHORTNAME", str(sid)))
        leave_name_map: dict = {}  # {leave_type_id: leave_type_name}
        for r in self._read("LEAVT"):
            lid = r.get("ID")
            if lid:
                leave_name_map[lid] = r.get("NAME", r.get("SHORTNAME", str(lid)))

        # Bauen: employee_id -> Datums-Set mit Schichten, Datum -> Schichtname
        shift_dates: dict = {}
        shift_detail: dict = {}  # {(eid, date): shift_name}
        for r in self._read("MASHI"):
            # Konflikte werden NUR auf der Ist-Ebene berechnet: ein Sollplan-Ziel
            # (5MASHI.TYPE=1, Spec 4.12/D-58) ist eine geplante Vorgabe, kein
            # tatsächlicher Dienst. Eine Soll-Schicht neben einer Ist-Abwesenheit
            # (oder Ist-Schicht) ist die normale Soll-/Ist-Zwei-Ebenen-Ansicht,
            # kein Konflikt — das Original kennt hier gar keine Konfliktprüfung.
            if int(r.get("TYPE") or 0) == 1:
                continue
            d = r.get("DATE", "")
            if d and d.startswith(prefix):
                eid = r.get("EMPLOYEEID")
                if eid in member_ids:
                    shift_dates.setdefault(eid, set()).add(d)
                    sid = r.get("SHIFTID")
                    sname = shift_name_map.get(sid, "") if sid else ""
                    if (eid, d) not in shift_detail:
                        shift_detail[(eid, d)] = sname
        for r in self._read("SPSHI"):
            d = r.get("DATE", "")
            if d and d.startswith(prefix):
                eid = r.get("EMPLOYEEID")
                if eid in member_ids:
                    shift_dates.setdefault(eid, set()).add(d)
                    if (eid, d) not in shift_detail:
                        shift_detail[(eid, d)] = r.get("NAME", "Sonderschicht")

        # Expandierte 5CYASS-Zyklusdienste (Spec 6.3/4.2): zyklusgeplante
        # Mitarbeiter erzeugen dieselben Konflikte; 5MASHI-Tage gewinnen
        # (bereits in _cycle_shifts_by_employee ausgefiltert).
        von = date(year, month, 1)
        bis = self._last_of_month(year, month)
        for eid, recs in self._cycle_shifts_by_employee(von, bis).items():
            if eid not in member_ids:
                continue
            for r in recs:
                d = r.get("DATE", "")
                if not d.startswith(prefix):
                    continue
                shift_dates.setdefault(eid, set()).add(d)
                sid = r.get("SHIFTID")
                if (eid, d) not in shift_detail:
                    shift_detail[(eid, d)] = shift_name_map.get(sid, "") if sid else ""

        # Bauen: employee_id -> Abwesenheits-Datumsliste, Datum -> Abwesenheitsart
        absence_dates: dict = {}
        absence_detail: dict = {}  # {(eid, date): leave_type_name}
        for r in self._read("ABSEN"):
            d = r.get("DATE", "")
            if d and d.startswith(prefix):
                eid = r.get("EMPLOYEEID")
                if eid in member_ids:
                    absence_dates.setdefault(eid, set()).add(d)
                    lid = r.get("LEAVETYPID")
                    lname = leave_name_map.get(lid, "") if lid else ""
                    absence_detail[(eid, d)] = lname

        # Conflict type 1: shift_and_absence
        for eid in member_ids:
            shifts = shift_dates.get(eid, set())
            absences = absence_dates.get(eid, set())
            overlap = shifts & absences
            emp = emp_map.get(eid)
            emp_name = (
                f"{emp.get('NAME', '')}, {emp.get('FIRSTNAME', '')}".strip(", ")
                if emp
                else f"MA#{eid}"
            )
            for date_str in sorted(overlap):
                day_display = date_str[8:10] + "." + date_str[5:7] + "." + date_str[0:4]
                conflicts.append(
                    {
                        "employee_id": eid,
                        "employee_name": emp_name,
                        "date": date_str,
                        "type": "shift_and_absence",
                        "shift_name": shift_detail.get((eid, date_str), ""),
                        "absence_name": absence_detail.get((eid, date_str), ""),
                        "message": f"{emp_name}: Schicht + Abwesenheit am {day_display}",
                    }
                )

        # Conflict type 2: holiday_ban
        # Alle Urlaubssperren (aller Gruppen) holen und nach MA-Gruppen filtern
        all_bans = self.get_holiday_bans()

        # Build employee -> groups map
        emp_groups: dict = {}
        for r in self._read("GRASG"):
            eid = r.get("EMPLOYEEID")
            gid = r.get("GROUPID")
            if eid in member_ids:
                emp_groups.setdefault(eid, set()).add(gid)

        # Je MA prüfen, ob eine Abwesenheit in einen Sperrzeitraum seiner Gruppe fällt
        for eid, abs_dates in absence_dates.items():
            emp = emp_map.get(eid)
            emp_name = (
                f"{emp.get('NAME', '')}, {emp.get('FIRSTNAME', '')}".strip(", ")
                if emp
                else f"MA#{eid}"
            )
            emp_group_ids = emp_groups.get(eid, set())
            for date_str in sorted(abs_dates):
                for ban in all_bans:
                    ban_gid = ban.get("group_id")
                    # Sperre greift ohne Gruppe (global) oder wenn der MA in der gesperrten Gruppe ist
                    if ban_gid and ban_gid not in emp_group_ids:
                        continue
                    start = ban.get("start_date", "")
                    end = ban.get("end_date", "")
                    if start and end and start <= date_str <= end:
                        day_display = (
                            date_str[8:10] + "." + date_str[5:7] + "." + date_str[0:4]
                        )
                        reason = ban.get("reason", "")
                        msg = f"{emp_name}: Abwesenheit in gesperrtem Zeitraum am {day_display}"
                        if reason:
                            msg += f" ({reason})"
                        conflicts.append(
                            {
                                "employee_id": eid,
                                "date": date_str,
                                "type": "holiday_ban",
                                "message": msg,
                            }
                        )
                        break  # one conflict per employee per date

        # Conflict type 3: holiday_shift — shift on a public holiday
        holiday_dates = self.get_holiday_dates(year)
        month_holidays = {d for d in holiday_dates if d.startswith(prefix)}
        for eid in member_ids:
            emp = emp_map.get(eid)
            emp_name = (
                f"{emp.get('NAME', '')}, {emp.get('FIRSTNAME', '')}".strip(", ")
                if emp
                else f"MA#{eid}"
            )
            for date_str in sorted(shift_dates.get(eid, set())):
                if date_str in month_holidays:
                    day_display = (
                        date_str[8:10] + "." + date_str[5:7] + "." + date_str[0:4]
                    )
                    sname = shift_detail.get((eid, date_str), "")
                    shift_info = f" ({sname})" if sname else ""
                    conflicts.append(
                        {
                            "employee_id": eid,
                            "employee_name": emp_name,
                            "date": date_str,
                            "type": "holiday_shift",
                            "severity": "warning",
                            "shift_name": sname,
                            "message": f"{emp_name}: Schicht{shift_info} am Feiertag {day_display}",
                        }
                    )

        # (Früher: Konflikttyp „long_shift" bei Schichtdauer > 10 h. Entfernt — ein
        #  starrer Stunden-Schwellwert ist falsch: lange Dienste, z. B. 12-Stunden-
        #  Schichten, sind in vielen Betrieben völlig normal und kein Konflikt. Das
        #  Original kennt ohnehin keine solche Prüfung. Eine Schichtdauer ist allein
        #  kein Konflikt; eine Dienstdefinition mit fragwürdiger Dauer ist in der
        #  Schichtart-Verwaltung sichtbar, nicht als Plan-Konflikt.)

        # Sort by date then employee_id
        conflicts.sort(key=lambda c: (c["date"], c["employee_id"]))
        return conflicts

    # ── Restrictions ───────────────────────────────────────────

    def get_restrictions(self, employee_id: int | None = None) -> list[dict]:
        """Return all shift restrictions, optionally filtered by employee."""
        rows = self._read("RESTR")
        if employee_id is not None:
            rows = [r for r in rows if r.get("EMPLOYEEID") == employee_id]
        shifts = {s["ID"]: s for s in self.get_shifts(include_hidden=True)}
        result = []
        for r in rows:
            shift = shifts.get(r.get("SHIFTID", 0))
            entry = {
                "id": r.get("ID"),
                "employee_id": r.get("EMPLOYEEID"),
                "shift_id": r.get("SHIFTID"),
                "weekday": r.get("WEEKDAY", 0),
                "restrict": r.get("RESTRICT", 1),
                "reason": (r.get("RESERVED") or "").strip(),
                "shift_name": shift.get("NAME", "") if shift else "",
                "shift_short": shift.get("SHORTNAME", "") if shift else "",
            }
            result.append(entry)
        return result

    def set_restriction(
        self,
        employee_id: int,
        shift_id: int,
        reason: str = "",
        weekday: int = 0,
        grade: int = 2,
    ) -> dict:
        """Create a restriction (or update its grade if one exists).

        ``grade`` (5RESTR.RESTRICT, Spec 4.11/D-58, Dekompilat-belegt SP5R):
        0 = keine (ohne Einschränkung), 1 = „auf Anfrage"/„?" (nur auf
        Nachfrage), 2 = „nie" (harte Sperre, Vorgabe). 0/1/2 wird geklemmt.
        """
        grade = grade if grade in (0, 1, 2) else 2
        filepath = self._table("RESTR")
        fields = get_table_fields(filepath)
        existing = find_all_records(
            filepath, fields, EMPLOYEEID=employee_id, SHIFTID=shift_id, WEEKDAY=weekday
        )
        if existing:
            raw_idx, rec = existing[0]
            update_record(filepath, fields, raw_idx, {
                "RESTRICT": grade, "RESERVED": (reason or "")[:20],
            })
            return {
                "id": rec.get("ID"),
                "employee_id": employee_id,
                "shift_id": shift_id,
                "weekday": weekday,
                "reason": reason,
                "grade": grade,
                "exists": True,
            }
        new_id = self._next_id("RESTR")
        record = {
            "ID": new_id,
            "EMPLOYEEID": employee_id,
            "WEEKDAY": weekday,
            "SHIFTID": shift_id,
            "RESTRICT": grade,
            "RESERVED": (reason or "")[:20],
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return {**record, "grade": grade, "exists": False}

    def remove_restriction(
        self, employee_id: int, shift_id: int, weekday: int = 0
    ) -> int:
        """Delete matching restriction records; returns count deleted."""
        filepath = self._table("RESTR")
        fields = get_table_fields(filepath)
        matches = find_all_records(
            filepath, fields, EMPLOYEEID=employee_id, SHIFTID=shift_id, WEEKDAY=weekday
        )
        count = 0
        for raw_idx, _ in matches:
            delete_record(filepath, fields, raw_idx)
            count += 1
        return count

    # ── Ersatzsuche / Notfallplan ─────────────────────────────
    def eligible_replacements(
        self,
        date_str: str,
        shift_id: int,
        absent_employee_id: int,
        group_id: int | None = None,
    ) -> list[dict]:
        """Geeignete Vertretungen für *shift_id* am *date_str* (Spec Kap. 5/6).

        Filtert hart nach den Eignungskriterien des Originals
        (calc.is_eligible_replacement): Bereich/Gruppe (5GRASG),
        Beschäftigungszeitraum (5EMPL EMPSTART/EMPEND), Verfügbarkeit (an dem
        Tag weder in 5MASHI/5SPSHI eingeteilt noch in 5ABSEN abwesend) und
        Schichtkompatibilität (keine 5RESTR-Sperre für Wochentag/Schicht).

        ``group_id`` schränkt den Bereich explizit und HART auf diese Gruppe
        ein. Ohne Angabe sind alle sonst geeigneten Mitarbeiter Kandidaten;
        Mitglieder einer gemeinsamen Gruppe des ausgefallenen Mitarbeiters
        kommen ZUERST (``same_group`` = True), erst danach die übrigen —
        innerhalb beider Blöcke alphabetisch nach Name.
        """
        d = calc.to_date(date_str)
        if d is None:
            raise ValueError(f"Ungültiges Datum: {date_str!r}")
        holidays = self._calc_holidays()

        # Bereich bestimmen: explizite Gruppe (hart) ODER Gruppen des
        # Ausfall-MA (weich: priorisiert, schließt aber nicht aus).
        members_by_group = self.get_all_group_members()
        if group_id is not None:
            allowed_ids: set[int] | None = set(members_by_group.get(group_id, []))
            same_group_ids = allowed_ids
        else:
            allowed_ids = None
            absent_groups = [
                gid for gid, members in members_by_group.items()
                if absent_employee_id in members
            ]
            same_group_ids = {
                eid
                for gid in absent_groups
                for eid in members_by_group.get(gid, [])
            }

        # Belegung des Tages: 5MASHI + 5SPSHI (eingeteilt), 5ABSEN (abwesend).
        busy_by_emp: dict[int, set[date]] = {}
        for table in ("MASHI", "SPSHI"):
            for r in self._read(table):
                rd = calc.to_date(r.get("DATE"))
                if rd is not None:
                    busy_by_emp.setdefault(r.get("EMPLOYEEID"), set()).add(rd)
        absent_by_emp: dict[int, set[date]] = {}
        for r in self._read("ABSEN"):
            rd = calc.to_date(r.get("DATE"))
            if rd is not None:
                absent_by_emp.setdefault(r.get("EMPLOYEEID"), set()).add(rd)

        restr_by_emp: dict[int, list[dict]] = {}
        for r in self._read("RESTR"):
            restr_by_emp.setdefault(r.get("EMPLOYEEID"), []).append(r)

        candidates: list[dict] = []
        for emp in self.get_employees(include_hidden=True):
            eid = emp["ID"]
            if eid == absent_employee_id:
                continue
            ctx = self._calc_context(emp)
            ok, _reason = calc.is_eligible_replacement(
                ctx,
                d,
                shift_id,
                holidays,
                is_hidden=bool(emp.get("HIDE")),
                in_group=(allowed_ids is None) or (eid in allowed_ids),
                busy_dates=busy_by_emp.get(eid, set()),
                absent_dates=absent_by_emp.get(eid, set()),
                restrictions=restr_by_emp.get(eid, []),
            )
            if not ok:
                continue
            candidates.append(
                {
                    "id": eid,
                    "name": emp.get("NAME", ""),
                    "firstname": emp.get("FIRSTNAME", ""),
                    "shortname": emp.get("SHORTNAME", ""),
                    "function": emp.get("FUNCTION", "") or "",
                    "hrs_week": emp.get("HRSWEEK", 0.0) or 0.0,
                    "same_group": eid in same_group_ids,
                }
            )
        candidates.sort(
            key=lambda c: (not c["same_group"], c["name"].lower(), c["firstname"].lower())
        )
        return candidates

    # ── Write: Periods ────────────────────────────────────────
    def create_period(self, data: dict) -> dict:
        """Append a new accounting period to 5PERIO."""
        filepath = self._table("PERIO")
        fields = get_table_fields(filepath)
        new_id = self._next_id("PERIO")
        record = {
            "ID": new_id,
            "GROUPID": data.get("group_id", 0),
            "START": data.get("start", ""),
            "END": data.get("end", ""),
            "COLOR": data.get("color", 16777215),
            "DESCRIPT": (data.get("description", "") or "")[:200],
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return {
            "id": record["ID"],
            "group_id": record["GROUPID"],
            "start": record["START"],
            "end": record["END"],
            "color": None,
            "description": record["DESCRIPT"],
        }

    def delete_period(self, period_id: int) -> int:
        """Delete an accounting period from 5PERIO by ID."""
        filepath = self._table("PERIO")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("PERIO", period_id)
        if raw_idx is None:
            return 0
        delete_record(filepath, fields, raw_idx)
        return 1

    def update_period(self, period_id: int, data: dict) -> dict | None:
        """Update a marked period (5PERIO) in place. Only provided keys change
        (``group_id``, ``start``, ``end``, ``color`` as BGR int, ``description``).
        Returns the updated record dict, or None if no period has that ID."""
        filepath = self._table("PERIO")
        fields = get_table_fields(filepath)
        raw_idx, record = self._find_record("PERIO", period_id)
        if raw_idx is None:
            return None
        update_data: dict = {}
        if "group_id" in data:
            update_data["GROUPID"] = data["group_id"]
        if "start" in data:
            update_data["START"] = data["start"]
        if "end" in data:
            update_data["END"] = data["end"]
        if "color" in data:
            update_data["COLOR"] = data["color"]
        if "description" in data:
            update_data["DESCRIPT"] = (data["description"] or "")[:200]
        if update_data:
            update_record(filepath, fields, raw_idx, update_data)
        color_val = update_data.get("COLOR", record.get("COLOR", 16777215))
        return {
            "id": period_id,
            "group_id": update_data.get("GROUPID", record.get("GROUPID")),
            "start": update_data.get("START", record.get("START", "")),
            "end": update_data.get("END", record.get("END", "")),
            "color": bgr_to_hex(color_val) if color_val else None,
            "description": update_data.get("DESCRIPT", record.get("DESCRIPT", "")),
        }

    # ── Write: Staffing Requirements (SHDEM) ──────────────────
    def set_staffing_requirement(
        self,
        shift_id: int,
        weekday: int,
        min_staff: int,
        max_staff: int,
        group_id: int,
    ) -> dict:
        """Create or update a SHDEM staffing requirement."""
        filepath = self._table("SHDEM")
        fields = get_table_fields(filepath)
        # Try to find existing record
        matches = find_all_records(
            filepath, fields, SHIFTID=shift_id, WEEKDAY=weekday, GROUPID=group_id
        )
        if matches:
            raw_idx, existing = matches[0]
            update_record(
                filepath, fields, raw_idx, {"MIN": min_staff, "MAX": max_staff}
            )
            return {
                "id": existing.get("ID"),
                "group_id": group_id,
                "weekday": weekday,
                "shift_id": shift_id,
                "min": min_staff,
                "max": max_staff,
            }
        # Create new
        new_id = self._next_id("SHDEM")
        record = {
            "ID": new_id,
            "GROUPID": group_id,
            "WEEKDAY": weekday,
            "SHIFTID": shift_id,
            "WORKPLACID": 0,
            "MIN": min_staff,
            "MAX": max_staff,
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return {
            "id": record["ID"],
            "group_id": group_id,
            "weekday": weekday,
            "shift_id": shift_id,
            "min": min_staff,
            "max": max_staff,
        }

    # ── Settings (USETT) ──────────────────────────────────────
    def get_usett(self) -> dict:
        """Read global settings from 5USETT.DBF (record 0)."""
        rows = self._read("USETT")
        if not rows:
            return {}
        r = rows[0]
        return {
            "ID": r.get("ID", 0),
            "LOGIN": r.get("LOGIN", 0),
            "SPSHCAT": r.get("SPSHCAT", 0),
            "OVERTCAT": r.get("OVERTCAT", 0),
            "ANOANAME": r.get("ANOANAME", "Abwesend"),
            "ANOASHORT": r.get("ANOASHORT", "X"),
            "ANOACRTXT": r.get("ANOACRTXT", 0),
            "ANOACRBAR": r.get("ANOACRBAR", 16711680),
            "ANOACRBK": r.get("ANOACRBK", 16777215),
            "ANOABOLD": r.get("ANOABOLD", 0),
            "BACKUPFR": r.get("BACKUPFR", 0),
        }

    def update_usett(self, data: dict) -> dict:
        """Update global settings in 5USETT.DBF (record 0)."""
        filepath = self._table("USETT")
        fields = get_table_fields(filepath)
        rows = read_dbf(filepath)
        if not rows:
            raise ValueError("5USETT.DBF is empty — cannot update settings")
        # Roh-Index des Satzes mit ID=0 finden (globale Einstellungszeile)
        matches = find_all_records(filepath, fields, ID=0)
        if matches:
            raw_idx = matches[0][0]
        else:
            # Fallback: ersten Satz nehmen, unabhängig von der ID
            raw_idx = 0
        update_data: dict[str, Any] = {}
        allowed = (
            "ANOANAME",
            "ANOASHORT",
            "ANOACRTXT",
            "ANOACRBAR",
            "ANOACRBK",
            "ANOABOLD",
            "BACKUPFR",
            "LOGIN",
            "SPSHCAT",
            "OVERTCAT",
        )
        for key in allowed:
            if key in data:
                update_data[key] = data[key]
        update_record(filepath, fields, raw_idx, update_data)
        self._invalidate_cache("USETT")
        return self.get_usett()

    # ── Special Staffing Requirements (SPDEM) ─────────────────
    def get_special_staffing(
        self, date: str | None = None, group_id: int | None = None
    ) -> list[dict]:
        """Return date-specific staffing requirements from 5SPDEM.DBF."""
        rows = self._read("SPDEM")
        shifts_map = {s["ID"]: s for s in self.get_shifts(include_hidden=True)}
        workplaces_map = {w["ID"]: w for w in self.get_workplaces(include_hidden=True)}
        result = []
        for r in rows:
            if date is not None and r.get("DATE", "") != date:
                continue
            if group_id is not None and r.get("GROUPID") != group_id:
                continue
            sid = r.get("SHIFTID")
            shift = shifts_map.get(sid) if sid else None
            wpid = r.get("WORKPLACID")
            wp = workplaces_map.get(wpid) if wpid else None
            result.append(
                {
                    "id": r.get("ID"),
                    "group_id": r.get("GROUPID"),
                    "date": r.get("DATE", ""),
                    "shift_id": sid,
                    "shift_name": shift.get("NAME", "") if shift else "",
                    "shift_short": shift.get("SHORTNAME", "") if shift else "",
                    "color_bk": bgr_to_hex(shift.get("COLORBK", 16777215))
                    if shift
                    else "#FFFFFF",
                    "color_text": bgr_to_hex(shift.get("COLORTEXT", 0))
                    if shift
                    else "#000000",
                    "workplace_id": wpid,
                    "workplace_name": wp.get("NAME", "") if wp else "",
                    "min": r.get("MIN", 0),
                    "max": r.get("MAX", 0),
                }
            )
        result.sort(key=lambda x: (x.get("date", ""), x.get("group_id") or 0))
        return result

    def create_special_staffing(
        self,
        groupid: int,
        date: str,
        shiftid: int,
        workplacid: int,
        min_staff: int,
        max_staff: int,
    ) -> dict:
        """Create a new date-specific staffing requirement in 5SPDEM.DBF."""
        filepath = self._table("SPDEM")
        fields = get_table_fields(filepath)
        new_id = self._next_id("SPDEM")
        record = {
            "ID": new_id,
            "GROUPID": groupid,
            "DATE": date,
            "SHIFTID": shiftid,
            "WORKPLACID": workplacid,
            "MIN": min_staff,
            "MAX": max_staff,
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return {**record, "id": record["ID"]}

    def update_special_staffing(self, record_id: int, data: dict) -> dict:
        """Update a date-specific staffing requirement."""
        filepath = self._table("SPDEM")
        fields = get_table_fields(filepath)
        raw_idx, existing = self._find_record("SPDEM", record_id)
        if raw_idx is None:
            raise ValueError(f"SpecialStaffing {record_id} not found")
        update_data: dict[str, Any] = {}
        for key in ("GROUPID", "DATE", "SHIFTID", "WORKPLACID", "MIN", "MAX"):
            if key in data:
                update_data[key] = data[key]
        update_record(filepath, fields, raw_idx, update_data)
        return {"id": record_id, **update_data}

    def delete_special_staffing(self, record_id: int) -> int:
        """Delete a date-specific staffing requirement."""
        filepath = self._table("SPDEM")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("SPDEM", record_id)
        if raw_idx is None:
            return 0
        delete_record(filepath, fields, raw_idx)
        return 1

    # ── Employee Detailed Statistics ──────────────────────────

    def _is_night_shift(self, shift: dict, weekday: int) -> bool:
        """Liefert True, wenn die Schicht am Wochentag als Nachtschicht zählt.
        Nacht = Beginn >= 22:00 ODER Ende <= 06:00 (und die Schicht hat ein Zeitfenster).
        """
        raw = shift.get(f"STARTEND{weekday}", "") or shift.get("STARTEND0", "")
        decoded = self._decode_startend(raw)
        if decoded and "-" in decoded:
            parts = decoded.split("-", 1)
            start_m = self._time_str_to_minutes(parts[0].strip())
            end_m = self._time_str_to_minutes(parts[1].strip())
            if start_m is not None and end_m is not None:
                # Night: starts at 22:00 (1320 min) or later, OR ends at 06:00 (360) or earlier
                if start_m >= 1320 or end_m <= 360:
                    return True
        # Fallback: check STARTEND0
        if weekday != 0:
            raw0 = shift.get("STARTEND0", "")
            decoded0 = self._decode_startend(raw0)
            if decoded0 and "-" in decoded0:
                parts0 = decoded0.split("-", 1)
                s0 = self._time_str_to_minutes(parts0[0].strip())
                e0 = self._time_str_to_minutes(parts0[1].strip())
                if s0 is not None and e0 is not None:
                    if s0 >= 1320 or e0 <= 360:
                        return True
        return False

    def get_employee_stats_year(self, employee_id: int, year: int) -> dict:
        """
        Return detailed per-employee statistics for all 12 months of a year.
        Includes: soll_stunden, ist_stunden, weekend_shifts, night_shifts, vacation_days, absence_days.

        Stundenwerte über die Berechnungsschicht (Spec 3.3/3.4/3.5): Ist =
        GetActualHours (tagindex-korrekte DURATION, 5SPSHI-Ersetzung,
        expandierte 5CYASS, Abwesenheits-Anrechnung, Ist-Buchungen), Soll =
        GetNominalHours (CALCBASE-Dispatcher).
        """
        from datetime import date as _date

        emp = self.get_employee(employee_id)
        if not emp:
            return {}

        shifts_map = {s["ID"]: s for s in self.get_shifts(include_hidden=True)}
        lt_map = {lt["ID"]: lt for lt in self.get_leave_types(include_hidden=True)}
        entitled_ids = {lt_id for lt_id, lt in lt_map.items() if lt.get("ENTITLED")}
        year_str = f"{year:04d}"

        ctx = self._calc_context(emp)
        inputs = self._calc_inputs(date(year, 1, 1), date(year, 12, 31), employee_id)
        plan = self._plan_kwargs(inputs, employee_id)
        absences = inputs["absences"].get(employee_id, [])
        bookings = inputs["bookings"].get(employee_id, [])

        # Monats-Buckets über die Berechnungsschicht initialisieren
        monthly: dict[int, dict] = {}
        for m in range(1, 13):
            m_von = date(year, m, 1)
            m_bis = self._last_of_month(year, m)
            target = calc.get_nominal_hours(
                ctx, m_von, m_bis, holidays=inputs["holidays"], bookings=bookings
            )
            actual = calc.get_actual_hours(
                ctx,
                m_von,
                m_bis,
                absences=absences,
                leave_types_by_id=lt_map,
                bookings=bookings,
                **plan,
            )
            monthly[m] = {
                "month": m,
                "target_hours": round(target, 2),
                "actual_hours": round(actual, 2),
                "weekend_shifts": 0,
                "night_shifts": 0,
                "vacation_days": 0,
                "absence_days": 0,
                "shifts_count": 0,
            }

        # Dienste (5MASHI + expandierte 5CYASS) für die Zähler scannen
        for source in ("manual_shifts", "cycle_shifts"):
            for r in plan[source]:
                d = r.get("DATE", "")
                if not d or not d.startswith(year_str):
                    continue
                m = int(d[5:7])
                try:
                    weekday = _date.fromisoformat(d).weekday()
                except ValueError:
                    continue
                sid = r.get("SHIFTID")
                shift = shifts_map.get(sid) if sid else None
                if shift:
                    monthly[m]["shifts_count"] += 1
                    if weekday >= 5:  # Saturday=5, Sunday=6
                        monthly[m]["weekend_shifts"] += 1
                    if self._is_night_shift(shift, weekday):
                        monthly[m]["night_shifts"] += 1

        # Scan SPSHI (special shifts)
        for r in plan["special_shifts"]:
            d = r.get("DATE", "")
            if not d or not d.startswith(year_str):
                continue
            m = int(d[5:7])
            try:
                weekday = _date.fromisoformat(d).weekday()
            except ValueError:
                continue
            monthly[m]["shifts_count"] += 1
            if weekday >= 5:
                monthly[m]["weekend_shifts"] += 1
            # For special shifts, check referenced shift if available
            sid = r.get("SHIFTID")
            if sid and sid in shifts_map:
                if self._is_night_shift(shifts_map[sid], weekday):
                    monthly[m]["night_shifts"] += 1

        # Scan ABSEN (absences)
        for r in absences:
            d = r.get("DATE", "")
            if not d or not d.startswith(year_str):
                continue
            m = int(d[5:7])
            monthly[m]["absence_days"] += 1
            lt_id = r.get("LEAVETYPID", 0)
            if lt_id in entitled_ids:
                monthly[m]["vacation_days"] += 1

        # Ergebnisliste mit berechneter Differenz bauen
        months_list = []
        total_target = 0.0
        total_actual = 0.0
        total_weekend = 0
        total_night = 0
        total_vacation = 0
        total_absence = 0
        total_shifts = 0

        for m in range(1, 13):
            mo = monthly[m]
            mo["actual_hours"] = round(mo["actual_hours"], 2)
            mo["difference"] = round(mo["actual_hours"] - mo["target_hours"], 2)
            total_target += mo["target_hours"]
            total_actual += mo["actual_hours"]
            total_weekend += mo["weekend_shifts"]
            total_night += mo["night_shifts"]
            total_vacation += mo["vacation_days"]
            total_absence += mo["absence_days"]
            total_shifts += mo["shifts_count"]
            months_list.append(mo)

        return {
            "employee_id": employee_id,
            "employee_name": f"{emp.get('NAME', '')}, {emp.get('FIRSTNAME', '')}".strip(
                ", "
            ),
            "employee_short": emp.get("SHORTNAME", ""),
            "employee_number": emp.get("NUMBER", ""),
            "year": year,
            "months": months_list,
            "totals": {
                "target_hours": round(total_target, 2),
                "actual_hours": round(total_actual, 2),
                "difference": round(total_actual - total_target, 2),
                "weekend_shifts": total_weekend,
                "night_shifts": total_night,
                "vacation_days": total_vacation,
                "absence_days": total_absence,
                "shifts_count": total_shifts,
            },
        }

    def get_employee_stats_month(self, employee_id: int, year: int, month: int) -> dict:
        """
        Return detailed stats for a single employee for a specific month.
        Subset of get_employee_stats_year for one month.
        """
        result = self.get_employee_stats_year(employee_id, year)
        if not result:
            return {}
        mo = result["months"][month - 1]
        return {
            "employee_id": employee_id,
            "employee_name": result["employee_name"],
            "employee_short": result["employee_short"],
            "year": year,
            "month": month,
            "target_hours": mo["target_hours"],
            "actual_hours": mo["actual_hours"],
            "difference": mo["difference"],
            "weekend_shifts": mo["weekend_shifts"],
            "night_shifts": mo["night_shifts"],
            "vacation_days": mo["vacation_days"],
            "absence_days": mo["absence_days"],
            "shifts_count": mo["shifts_count"],
        }

    # ── Datenbank komprimieren (Spec 1.14) ─────────────────────
    def compact_database(self) -> dict:
        """PACK aller Tabellen: gelöschte Records physisch entfernen.

        Delegiert je .DBF an dbf_writer.pack_table (Header-Count/EOF,
        Journal-Zap nach Spec D-74, CDX-Invalidierung nach D-14) und
        invalidiert anschließend den Lese-Cache dieser Datenbank.
        """
        results: list[dict] = []
        total_removed = 0
        for fname in sorted(os.listdir(self.db_path)):
            if not fname.upper().endswith(".DBF"):
                continue
            fpath = os.path.join(self.db_path, fname)
            try:
                removed = pack_table(fpath)
                active = _dbf_header_info(fpath)[0]
            except Exception as exc:  # korrupte Datei: melden, weitermachen
                results.append({"file": fname, "error": str(exc)})
                continue
            total_removed += removed
            results.append({"file": fname, "removed": removed, "active": active})

        with _CACHE_LOCK:
            for key in [k for k in _GLOBAL_DBF_CACHE if k[0] == self.db_path]:
                _GLOBAL_DBF_CACHE.pop(key, None)

        return {
            "files_processed": len(results),
            "total_records_removed": total_removed,
            "details": results,
        }

    # ── Stats ──────────────────────────────────────────────────
    def get_stats(self) -> dict:
        """Liefert Übersichtsstatistiken der verbundenen Datenbank."""
        employees = self.get_employees()
        groups = self.get_groups()
        shifts = self.get_shifts()
        leave_types = self.get_leave_types()
        workplaces = self.get_workplaces()
        holidays = self.get_holidays()
        users = self.get_users()

        return {
            "employees": len(employees),
            "groups": len(groups),
            "shifts": len(shifts),
            "leave_types": len(leave_types),
            "workplaces": len(workplaces),
            "holidays": len(holidays),
            "users": len(users),
        }

    # ── Cycle Exceptions (5CYEXC) ─────────────────────────────
    def get_cycle_exceptions(
        self,
        employee_id: int | None = None,
        cycle_assignment_id: int | None = None,
    ) -> list[dict]:
        """Liefert Zyklus-Ausnahmen (Datums-Overrides in zugewiesenen Zyklen)."""
        rows = self._read("CYEXC")
        result = []
        for r in rows:
            if employee_id is not None and r.get("EMPLOYEEID") != employee_id:
                continue
            if (
                cycle_assignment_id is not None
                and r.get("CYCLEASSID") != cycle_assignment_id
            ):
                continue
            result.append(
                {
                    "id": r.get("ID"),
                    "employee_id": r.get("EMPLOYEEID"),
                    "cycle_assignment_id": r.get("CYCLEASSID"),
                    "date": r.get("DATE", ""),
                    "type": r.get("TYPE", 0),
                }
            )
        return result

    def set_cycle_exception(
        self,
        employee_id: int,
        cycle_assignment_id: int,
        date_str: str,
        exc_type: int = 1,
    ) -> dict:
        """Setzt eine Zyklus-Ausnahme für ein Datum (type 1=auslassen, 0=normal)."""
        filepath = self._table("CYEXC")
        fields = get_table_fields(filepath)
        # Prüfen, ob zu Datum/MA schon eine Ausnahme existiert
        existing = find_all_records(
            filepath, fields, EMPLOYEEID=employee_id, CYCLEASSID=cycle_assignment_id
        )
        for raw_idx, r in existing:
            if r.get("DATE", "") == date_str:
                update_record(filepath, fields, raw_idx, {"TYPE": exc_type})
                return {
                    "id": r.get("ID"),
                    "employee_id": employee_id,
                    "cycle_assignment_id": cycle_assignment_id,
                    "date": date_str,
                    "type": exc_type,
                }
        new_id = self._next_id("CYEXC")
        record = {
            "ID": new_id,
            "EMPLOYEEID": employee_id,
            "CYCLEASSID": cycle_assignment_id,
            "DATE": date_str,
            "TYPE": exc_type,
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return {
            "id": record["ID"],
            "employee_id": employee_id,
            "cycle_assignment_id": cycle_assignment_id,
            "date": date_str,
            "type": exc_type,
        }

    def delete_cycle_exception(self, exception_id: int) -> int:
        """Delete a cycle exception by ID."""
        filepath = self._table("CYEXC")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("CYEXC", exception_id)
        if raw_idx is None:
            return 0
        delete_record(filepath, fields, raw_idx)
        return 1

    # ── Differenzierte Sichtbarkeit (Spec 9.5.3, 5GRACC/5EMACC) ──
    def _descendant_group_ids(self, group_ids: set[int]) -> set[int]:
        """Erweitere eine Gruppenmenge um alle Untergruppen (SUPERID-Vererbung,
        Spec 9.5.3 Nr. 5c)."""
        children: dict[int, list[int]] = {}
        for g in self._read("GROUP"):
            children.setdefault(g.get("SUPERID") or 0, []).append(g.get("ID"))
        out = set(group_ids)
        stack = list(group_ids)
        while stack:
            gid = stack.pop()
            for child in children.get(gid, []):
                if child is not None and child not in out:
                    out.add(child)
                    stack.append(child)
        return out

    def get_user_visible_employee_ids(self, user_id: int) -> set[int] | None:
        """Sichtbare Mitarbeiter eines Benutzers (Spec 9.5.3 Nr. 6).

        ``None`` = keine Einschränkung (Admin/volle bzw. nur-lesende Rechte ohne
        differenzierte Festlegung). Sonst die Menge der MA-IDs, die über eine
        Gruppen- (5GRACC, inkl. Untergruppen) oder Mitarbeiter-Festlegung (5EMACC)
        zugänglich sind. Mitarbeiter ohne jede Festlegung bleiben verborgen.

        Hinweis (Regel 6): Der subtraktive 5EMACC-„kein Zugriff"-Override
        (Verbotsschild) wird NICHT erzwungen — der RIGHTS-Wert dafür ist in der
        Referenz-DB nicht beobachtbar (5EMACC leer) und im Datenmodell unsicher;
        5EMACC-Einträge wirken hier additiv (erweiternd). 5GRACC-Sichtbarkeit ist
        vollständig umgesetzt.
        """
        gracc = self.get_group_access(user_id)
        emacc = self.get_employee_access(user_id)
        if not gracc and not emacc:
            return None  # keine differenzierten Rechte ⇒ alles sichtbar
        visible: set[int] = set()
        group_ids = {g["group_id"] for g in gracc if g.get("group_id")}
        if group_ids:
            members = self.get_all_group_members()
            for gid in self._descendant_group_ids(group_ids):
                visible.update(members.get(gid, []))
        for e in emacc:
            eid = e.get("employee_id")
            if eid:
                visible.add(eid)
        return visible

    def get_user_visible_group_ids(self, user_id: int) -> set[int] | None:
        """Sichtbare Gruppen eines Benutzers (5GRACC inkl. Untergruppen).
        ``None`` = keine Einschränkung. Siehe get_user_visible_employee_ids."""
        gracc = self.get_group_access(user_id)
        emacc = self.get_employee_access(user_id)
        if not gracc and not emacc:
            return None
        group_ids = {g["group_id"] for g in gracc if g.get("group_id")}
        return self._descendant_group_ids(group_ids) if group_ids else set()

    # ── Employee Access (5EMACC) ──────────────────────────────
    def get_employee_access(self, user_id: int | None = None) -> list[dict]:
        """Liefert MA-bezogene Zugriffsbeschränkungen der Benutzer."""
        rows = self._read("EMACC")
        result = []
        for r in rows:
            if user_id is not None and r.get("USERID") != user_id:
                continue
            result.append(
                {
                    "id": r.get("ID"),
                    "user_id": r.get("USERID"),
                    "employee_id": r.get("EMPLOYEEID"),
                    "rights": r.get("RIGHTS", 0),
                }
            )
        return result

    def set_employee_access(self, user_id: int, employee_id: int, rights: int) -> dict:
        """Setzt die Zugriffsrechte eines Benutzers auf einen Mitarbeiter."""
        filepath = self._table("EMACC")
        fields = get_table_fields(filepath)
        existing = find_all_records(
            filepath, fields, USERID=user_id, EMPLOYEEID=employee_id
        )
        if existing:
            raw_idx, r = existing[0]
            update_record(filepath, fields, raw_idx, {"RIGHTS": rights})
            return {
                "id": r.get("ID"),
                "user_id": user_id,
                "employee_id": employee_id,
                "rights": rights,
            }
        new_id = self._next_id("EMACC")
        record = {
            "ID": new_id,
            "USERID": user_id,
            "EMPLOYEEID": employee_id,
            "RIGHTS": rights,
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return {
            "id": record["ID"],
            "user_id": user_id,
            "employee_id": employee_id,
            "rights": rights,
        }

    def delete_employee_access(self, access_id: int) -> int:
        """Delete employee access record."""
        filepath = self._table("EMACC")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("EMACC", access_id)
        if raw_idx is None:
            return 0
        delete_record(filepath, fields, raw_idx)
        return 1

    # ── Group Access (5GRACC) ─────────────────────────────────
    def get_group_access(self, user_id: int | None = None) -> list[dict]:
        """Liefert gruppenbezogene Zugriffsbeschränkungen der Benutzer."""
        rows = self._read("GRACC")
        result = []
        for r in rows:
            if user_id is not None and r.get("USERID") != user_id:
                continue
            result.append(
                {
                    "id": r.get("ID"),
                    "user_id": r.get("USERID"),
                    "group_id": r.get("GROUPID"),
                    "rights": r.get("RIGHTS", 0),
                }
            )
        return result

    def set_group_access(self, user_id: int, group_id: int, rights: int) -> dict:
        """Setzt die Zugriffsrechte eines Benutzers auf eine Gruppe."""
        filepath = self._table("GRACC")
        fields = get_table_fields(filepath)
        existing = find_all_records(filepath, fields, USERID=user_id, GROUPID=group_id)
        if existing:
            raw_idx, r = existing[0]
            update_record(filepath, fields, raw_idx, {"RIGHTS": rights})
            return {
                "id": r.get("ID"),
                "user_id": user_id,
                "group_id": group_id,
                "rights": rights,
            }
        new_id = self._next_id("GRACC")
        record = {
            "ID": new_id,
            "USERID": user_id,
            "GROUPID": group_id,
            "RIGHTS": rights,
            "RESERVED": "",
        }
        append_record(filepath, fields, record, autoid_field="ID")
        return {
            "id": record["ID"],
            "user_id": user_id,
            "group_id": group_id,
            "rights": rights,
        }

    def delete_group_access(self, access_id: int) -> int:
        """Delete group access record."""
        filepath = self._table("GRACC")
        fields = get_table_fields(filepath)
        raw_idx, _ = self._find_record("GRACC", access_id)
        if raw_idx is None:
            return 0
        delete_record(filepath, fields, raw_idx)
        return 1

    # ── Changelog ─────────────────────────────────────────────

    def _changelog_path(self) -> str:
        """Pfad zur changelog.json neben dem Datenbank-Verzeichnis."""
        return os.path.join(_paths.data_dir(), "changelog.json")

    def get_changelog(
        self,
        limit: int = 100,
        user: str | None = None,
        entity_type: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict]:
        """Read changelog entries from backend/data/changelog.json."""
        path = self._changelog_path()
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                entries: list[dict] = json.load(f)
        except Exception:
            return []
        # Filter
        if user:
            entries = [e for e in entries if e.get("user", "").lower() == user.lower()]
        if entity_type:
            entries = [
                e
                for e in entries
                if e.get("entity", "").lower() == entity_type.lower()
            ]
        if date_from:
            entries = [e for e in entries if e.get("timestamp", "") >= date_from]
        if date_to:
            entries = [
                e for e in entries if e.get("timestamp", "") <= date_to + "T23:59:59"
            ]
        # Sort newest first, apply limit
        entries = sorted(entries, key=lambda e: e.get("timestamp", ""), reverse=True)
        return entries[:limit]

    def log_action(
        self,
        user: str,
        action: str,
        entity: str,
        entity_id: int,
        details: str = "",
        old_value: dict | str | None = None,
        new_value: dict | str | None = None,
        user_id: int | None = None,
    ) -> dict:
        """Append a log entry to backend/data/changelog.json. Keeps max 5000 entries."""
        import datetime as _dt

        path = self._changelog_path()
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    entries: list[dict] = json.load(f)
            except Exception:
                entries = []
        else:
            entries = []
        entry: dict = {
            "timestamp": _dt.datetime.now().isoformat(timespec="seconds"),
            "user": user,
            "action": action,  # CREATE / UPDATE / DELETE
            "entity": entity,  # employee / shift / group / schedule / absence / ...
            "entity_id": entity_id,
            "details": details,
        }
        if user_id is not None:
            entry["user_id"] = user_id
        if old_value is not None:
            entry["old_value"] = old_value
        if new_value is not None:
            entry["new_value"] = new_value
        entries.append(entry)
        # Keep newest 5000
        if len(entries) > 5000:
            entries = entries[-5000:]
        _atomic_write_json(path, entries, ensure_ascii=False, indent=2)
        return entry

    # ── Schicht-Wünsche & Sperrtage ──────────────────────────

    def _wishes_path(self) -> str:
        """Pfad zur wishes.json neben den DB-Dateien."""
        os.makedirs(self.db_path, exist_ok=True)
        return os.path.join(self.db_path, "wishes.json")

    def get_wishes(
        self,
        employee_id: int | None = None,
        year: int | None = None,
        month: int | None = None,
    ) -> list[dict]:
        """Return shift wishes/blocked days, optionally filtered."""
        path = self._wishes_path()
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                entries: list[dict] = json.load(f)
        except Exception:
            return []
        if employee_id is not None:
            entries = [e for e in entries if e.get("employee_id") == employee_id]
        if year is not None:
            entries = [e for e in entries if str(e.get("date", ""))[:4] == str(year)]
        if month is not None:
            entries = [
                e for e in entries if str(e.get("date", ""))[5:7] == f"{month:02d}"
            ]
        return entries

    def add_wish(
        self,
        employee_id: int,
        date: str,
        wish_type: str,
        shift_id: int | None = None,
        note: str = "",
    ) -> dict:
        """Add a wish (WUNSCH) or blocked day (SPERRUNG).

        wish_type: 'WUNSCH' | 'SPERRUNG'
        """
        import datetime as _dt

        path = self._wishes_path()
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    entries: list[dict] = json.load(f)
            except Exception:
                entries = []
        else:
            entries = []
        # Prevent duplicate: same employee + date + wish_type already exists
        existing = next(
            (
                e
                for e in entries
                if e.get("employee_id") == employee_id
                and e.get("date") == date
                and e.get("wish_type") == wish_type
            ),
            None,
        )
        if existing is not None:
            raise ValueError(
                f"Wunsch vom Typ '{wish_type}' für MA {employee_id} am {date} existiert bereits (ID {existing['id']})"
            )
        max_id = max((e.get("id", 0) for e in entries), default=0)
        entry = {
            "id": max_id + 1,
            "employee_id": employee_id,
            "date": date,
            "wish_type": wish_type,  # WUNSCH | SPERRUNG
            "shift_id": shift_id,
            "note": note,
            "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        }
        entries.append(entry)
        _atomic_write_json(path, entries, ensure_ascii=False, indent=2)
        return entry

    def update_wish_status(self, wish_id: int, status: str) -> dict | None:
        """Aktualisiert den Status eines Wunsches. Liefert den Eintrag oder None."""
        import datetime as _dt

        path = self._wishes_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                entries: list[dict] = json.load(f)
        except Exception:
            return None
        entry = next((e for e in entries if e.get("id") == wish_id), None)
        if entry is None:
            return None
        entry["status"] = status
        entry["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        _atomic_write_json(path, entries, ensure_ascii=False, indent=2)
        return entry

    def delete_wish(self, wish_id: int) -> int:
        """Delete a wish by ID. Returns 1 if deleted, 0 if not found."""
        path = self._wishes_path()
        if not os.path.exists(path):
            return 0
        try:
            with open(path, encoding="utf-8") as f:
                entries: list[dict] = json.load(f)
        except Exception:
            return 0
        new_entries = [e for e in entries if e.get("id") != wish_id]
        if len(new_entries) == len(entries):
            return 0
        _atomic_write_json(path, new_entries, ensure_ascii=False, indent=2)
        return 1

    # ── Overtime Summary ──────────────────────────────────────

    def get_overtime_summary(
        self, year: int, group_id: int | None = None
    ) -> list[dict]:
        """Berechnet die Überstunden je Mitarbeiter für ein Jahr.

        Liefert:
            Liste von dicts mit den Schlüsseln
              employee_id, name, shortname, soll, ist, delta
            mit delta = ist − soll (positiv = Plusstunden, negativ = Minusstunden).
        """
        employees = self.get_employees(include_hidden=False)
        if group_id is not None:
            member_ids = set(self.get_group_members(group_id))
            employees = [e for e in employees if e["ID"] in member_ids]

        result = []
        for emp in employees:
            balance = self.calculate_time_balance(emp["ID"], year)
            if not balance:
                continue
            soll = balance.get("total_target_hours", 0.0)
            ist = balance.get("total_actual_hours", 0.0)
            delta = round(ist - soll, 2)
            result.append(
                {
                    "employee_id": emp["ID"],
                    "name": f"{emp.get('NAME', '')} {emp.get('FIRSTNAME', '')}".strip(),
                    "shortname": emp.get("SHORTNAME", ""),
                    "number": emp.get("NUMBER", ""),
                    "soll": round(soll, 2),
                    "ist": round(ist, 2),
                    "delta": delta,
                    "saldo": round(balance.get("total_saldo", delta), 2),
                }
            )
        result.sort(key=lambda x: x.get("name", ""))
        return result

    # ── Sickness Statistics ───────────────────────────────────

    def get_sickness_statistics(self, year: int) -> dict:
        """Liefert die Krankenstand-Statistik für ein Jahr.

        Liefert:
            {
                year, sick_type_ids,
                total_sick_days, affected_employees,
                per_employee: [{employee_id, employee_name, employee_short, group_name,
                                sick_days, sick_episodes, bradford_factor}],
                per_month: [{month, sick_days}],
                per_weekday: [{weekday, weekday_name, sick_days}],
            }
        """
        # Identify sick leave type IDs
        leavt = self.get_leave_types(include_hidden=True)
        sick_ids: set = set()
        for lt in leavt:
            name = (lt.get("NAME", "") or "").lower()
            short = (lt.get("SHORTNAME", "") or "").lower()
            if any(kw in name or kw in short for kw in ["krank", "sick", "ku"]):
                sick_ids.add(lt["ID"])

        year_str = str(year)

        # Krank-Abwesenheiten des Jahres sammeln
        sick_abs = []
        for r in self._read("ABSEN"):
            date_val = r.get("DATE", "") or ""
            if date_val.startswith(year_str) and r.get("LEAVETYPID") in sick_ids:
                sick_abs.append(r)

        # Build employee + group info
        employees = self.get_employees(include_hidden=False)
        {e["ID"]: e for e in employees}

        # Group membership per employee
        emp_group: dict[int, str] = {}
        emp_group_id: dict[int, int] = {}
        for grp in self.get_groups(include_hidden=False):
            gid = grp.get("ID")
            gname = grp.get("NAME", "")
            for mid in self.get_group_members(int(gid) if gid is not None else 0):
                emp_group[mid] = gname
                emp_group_id[mid] = int(gid) if gid is not None else 0

        # Per-employee: collect sick day dates
        emp_dates: dict[int, list] = {}
        for ab in sick_abs:
            eid = ab.get("EMPLOYEEID")
            date_val = ab.get("DATE", "")
            if eid and date_val:
                if eid not in emp_dates:
                    emp_dates[eid] = []
                emp_dates[eid].append(date_val)

        # Zähler je Monat und je Wochentag
        weekday_names = [
            "Montag",
            "Dienstag",
            "Mittwoch",
            "Donnerstag",
            "Freitag",
            "Samstag",
            "Sonntag",
        ]
        per_month = [0] * 13  # index 1-12
        per_weekday = [0] * 7  # index 0=Mon … 6=Sun

        for ab in sick_abs:
            date_str = ab.get("DATE", "") or ""
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d")
                per_month[d.month] += 1
                per_weekday[d.weekday()] += 1
            except ValueError:
                pass

        # Je-MA-Statistik mit Bradford-Faktor bauen
        per_employee = []
        for emp in employees:
            eid = emp["ID"]
            dates_sorted = sorted(emp_dates.get(eid, []))
            sick_days = len(dates_sorted)

            # Count distinct episodes (consecutive days → same episode)
            episodes = 0
            if dates_sorted:
                episodes = 1
                prev_d = datetime.strptime(dates_sorted[0], "%Y-%m-%d")
                for d_str in dates_sorted[1:]:
                    d = datetime.strptime(d_str, "%Y-%m-%d")
                    if (d - prev_d).days > 3:  # gap > 3 calendar days = new episode
                        episodes += 1
                    prev_d = d

            bradford = (episodes**2) * sick_days  # Bradford Factor = S² × D

            per_employee.append(
                {
                    "employee_id": eid,
                    "employee_name": f"{emp.get('NAME', '')}, {emp.get('FIRSTNAME', '')}".strip(
                        ", "
                    ),
                    "employee_short": emp.get("SHORTNAME", ""),
                    "group_name": emp_group.get(eid, ""),
                    "group_id": emp_group_id.get(eid, None),
                    "sick_days": sick_days,
                    "sick_episodes": episodes,
                    "bradford_factor": bradford,
                }
            )

        # Sort by sick_days descending, then name
        per_employee.sort(key=lambda x: (-x["sick_days"], x["employee_name"]))

        return {
            "year": year,
            "sick_type_ids": sorted(sick_ids),
            "total_sick_days": sum(per_month[1:]),
            "affected_employees": len(emp_dates),
            "total_employees": len(employees),
            "per_employee": per_employee,
            "per_month": [
                {"month": i, "sick_days": per_month[i]} for i in range(1, 13)
            ],
            "per_weekday": [
                {
                    "weekday": i,
                    "weekday_name": weekday_names[i],
                    "sick_days": per_weekday[i],
                }
                for i in range(7)
            ],
        }

    # ── Schedule Templates (Schicht-Vorlagen) ─────────────────

    def _templates_path(self) -> str:
        """Pfad zur schedule_templates.json im DB-Datenverzeichnis."""
        return os.path.join(self.db_path, "schedule_templates.json")

    def _load_templates(self) -> list[dict]:
        path = self._templates_path()
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_templates(self, templates: list[dict]) -> None:
        path = self._templates_path()
        _atomic_write_json(path, templates, ensure_ascii=False, indent=2)

    def get_schedule_templates(self) -> list[dict]:
        """Return all saved schedule templates."""
        return self._load_templates()

    def create_schedule_template(
        self, name: str, description: str, assignments: list[dict]
    ) -> dict:
        """Save a new schedule template.

        Each assignment: {employee_id, weekday_offset (0=Mon..6=Sun), shift_id}.
        """
        import datetime as _dt

        templates = self._load_templates()
        new_id = max((t.get("id", 0) for t in templates), default=0) + 1
        template = {
            "id": new_id,
            "name": name,
            "description": description,
            "assignments": assignments,
            "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        }
        templates.append(template)
        self._save_templates(templates)
        return template

    def delete_schedule_template(self, template_id: int) -> bool:
        """Löscht eine Vorlage per ID. Liefert True, wenn gefunden und gelöscht."""
        templates = self._load_templates()
        new_list = [t for t in templates if t.get("id") != template_id]
        if len(new_list) == len(templates):
            return False
        self._save_templates(new_list)
        return True

    def apply_schedule_template(
        self, template_id: int, target_date: str, force: bool = False
    ) -> dict:
        """Wendet eine Vorlage auf die Woche ab target_date an (beliebiger Tag = Wochenanker).

        Der weekday_offset (0=Mo..6=So) jeder Zuweisung wird auf target_date addiert.
        Liefert die Zähler { created, updated, skipped }.
        """
        from datetime import datetime as _dtd
        from datetime import timedelta as _td

        templates = self._load_templates()
        tmpl = next((t for t in templates if t.get("id") == template_id), None)
        if tmpl is None:
            raise ValueError(f"Template {template_id} not found")

        try:
            week_start = _dtd.fromisoformat(target_date).date()
        except ValueError:
            raise ValueError(f"Invalid target_date: {target_date}")

        created = updated = skipped = 0
        for asgn in tmpl.get("assignments", []):
            employee_id = asgn.get("employee_id")
            shift_id = asgn.get("shift_id")
            weekday_offset = int(asgn.get("weekday_offset", 0))
            if employee_id is None or shift_id is None:
                continue

            target_day = week_start + _td(days=weekday_offset)
            date_str = target_day.isoformat()

            existing = self.get_schedule_day(date_str)
            already_exists = any(e.get("employee_id") == employee_id for e in existing)

            if already_exists and not force:
                skipped += 1
                continue

            if already_exists and force:
                self.delete_schedule_entry(employee_id, date_str)
                updated += 1
            else:
                created += 1

            self.add_schedule_entry(employee_id, date_str, shift_id)

        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "template_name": tmpl.get("name", ""),
        }

    def get_week_entries_for_template(
        self, year: int, month: int, week_start_day: int, group_id: int | None = None
    ) -> list[dict]:
        """Liefert alle Schichteinträge eines 7-Tage-Fensters ab week_start_day.

        Ergänzt je Eintrag 'weekday_offset' (0=Mo..6=So) und 'source_date'.
        """
        from calendar import monthrange as _mr
        from datetime import date as _date

        days_in_month = _mr(year, month)[1]
        result = []
        for offset in range(7):
            d = week_start_day + offset
            if d > days_in_month:
                break
            date_obj = _date(year, month, d)
            weekday_offset = date_obj.weekday()  # 0=Mon..6=Sun
            date_str = date_obj.isoformat()
            day_entries = self.get_schedule_day(date_str, group_id=group_id)
            for entry in day_entries:
                if entry.get("kind") == "shift":  # only regular shifts, not absences
                    entry["weekday_offset"] = weekday_offset
                    entry["source_date"] = date_str
                    result.append(entry)
        return result

    # ── Burnout-Radar ─────────────────────────────────────────
    def get_burnout_radar(
        self,
        year: int,
        month: int,
        streak_threshold: int = 6,
        overtime_threshold_pct: float = 20.0,
        group_id: int | None = None,
    ) -> list[dict]:
        """
        Analyse schedule to detect at-risk employees:
        1. Long consecutive work streaks (>= streak_threshold days)
        2. Significant overtime (actual_hours > target_hours * (1 + overtime_threshold_pct/100))
        Returns list of {employee_id, employee_name, risk_level, reasons, streak, overtime_pct}
        """
        import calendar as _cal
        from datetime import date as _date
        from datetime import timedelta as _td

        employees = self.get_employees()
        if group_id:
            group_member_ids = {
                r.get("EMPLOYEEID")
                for r in self._read("MAGRP")
                if r.get("GROUPID") == group_id
            }
            employees = [e for e in employees if e["ID"] in group_member_ids]

        # Arbeitstage-Set je MA für ein 6-Wochen-Fenster bauen (3 Wochen davor + laufender Monat)
        days_in_month = _cal.monthrange(year, month)[1]
        month_start = _date(year, month, 1)
        window_start = month_start - _td(days=21)  # 3 weeks before
        window_end = _date(year, month, days_in_month)

        # Abwesenheits-Set je MA im Fenster bauen (Krank-/Abwesenheitstage dürfen
        # nicht als Arbeitstage zählen — sonst bläht eine Kranksträhne den Burnout-Indikator auf)
        emp_absence_dates: dict[int, set] = {}
        for r in self._read("ABSEN"):
            d_str = r.get("DATE", "")
            if not d_str or len(d_str) < 10:
                continue
            try:
                d = _date.fromisoformat(d_str[:10])
            except ValueError:
                continue
            if window_start <= d <= window_end:
                eid = r.get("EMPLOYEEID")
                if eid:
                    emp_absence_dates.setdefault(eid, set()).add(d)

        # Collect all working days from MASHI + SPSHI in window
        emp_work_dates: dict[int, set] = {}
        for table in ("MASHI", "SPSHI"):
            for r in self._read(table):
                d_str = r.get("DATE", "")
                if not d_str or len(d_str) < 10:
                    continue
                try:
                    d = _date.fromisoformat(d_str[:10])
                except ValueError:
                    continue
                if window_start <= d <= window_end:
                    eid = r.get("EMPLOYEEID")
                    if eid:
                        # Skip absence days — planned shifts don't mean actually worked
                        if d in emp_absence_dates.get(eid, set()):
                            continue
                        emp_work_dates.setdefault(eid, set()).add(d)

        results = []
        for emp in employees:
            eid = emp["ID"]
            worked = sorted(emp_work_dates.get(eid, set()))
            if not worked:
                continue

            # Find longest streak ending in current month
            max_streak = 0
            streak_end_date = None
            i = 0
            while i < len(worked):
                streak = 1
                j = i + 1
                while j < len(worked) and (worked[j] - worked[j - 1]).days == 1:
                    streak += 1
                    j += 1
                if streak > max_streak:
                    max_streak = streak
                    streak_end_date = worked[j - 1]
                i = j

            # Nur Strähnen berücksichtigen, die in den laufenden Monat hineinreichen
            streak_in_month = (
                streak_end_date and streak_end_date >= month_start
                if streak_end_date
                else False
            )

            # Überstunden des laufenden Monats holen
            try:
                stats = self.get_employee_stats_month(eid, year, month)
                target_h = stats.get("target_hours", 0)
                actual_h = stats.get("actual_hours", 0)
                if target_h > 0:
                    ot_pct = ((actual_h - target_h) / target_h) * 100
                else:
                    ot_pct = 0
                ot_hours = actual_h - target_h
            except Exception:
                ot_pct = 0
                ot_hours = 0
                target_h = 0
                actual_h = 0

            reasons = []
            if streak_in_month and max_streak >= streak_threshold:
                reasons.append(f"{max_streak} Tage am Stück")
            if ot_pct >= overtime_threshold_pct:
                reasons.append(f"+{ot_pct:.0f}% Überstunden ({ot_hours:+.1f}h)")

            if not reasons:
                continue

            # Risk level
            risk_level = (
                "high"
                if (
                    (streak_in_month and max_streak >= streak_threshold + 2)
                    or ot_pct >= 30
                )
                else "medium"
            )

            results.append(
                {
                    "employee_id": eid,
                    "employee_name": f"{emp.get('NAME', '')}, {emp.get('FIRSTNAME', '')}",
                    "employee_short": emp.get("SHORTNAME", ""),
                    "risk_level": risk_level,
                    "reasons": reasons,
                    "streak": max_streak if streak_in_month else 0,
                    "overtime_pct": round(ot_pct, 1),
                    "overtime_hours": round(ot_hours, 1),
                    "actual_hours": round(actual_h, 1),
                    "target_hours": round(target_h, 1),
                }
            )

        # Sort: high risk first, then by streak desc
        results.sort(
            key=lambda x: (
                0 if x["risk_level"] == "high" else 1,
                -x["streak"],
                -x["overtime_pct"],
            )
        )
        return results

    # ── Schicht-Tauschbörse ───────────────────────────────────

    def _swap_requests_path(self) -> str:
        return os.path.join(_paths.data_dir(), "swap_requests.json")

    def _load_swap_requests(self) -> list[dict]:
        path = self._swap_requests_path()
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_swap_requests(self, entries: list[dict]) -> None:
        path = self._swap_requests_path()
        _atomic_write_json(path, entries, ensure_ascii=False, indent=2)

    def get_swap_requests(
        self, status: str | None = None, employee_id: int | None = None
    ) -> list[dict]:
        entries = self._load_swap_requests()
        if status:
            entries = [e for e in entries if e.get("status") == status]
        if employee_id is not None:
            entries = [
                e
                for e in entries
                if (
                    e.get("requester_id") == employee_id
                    or e.get("partner_id") == employee_id
                )
            ]
        return sorted(entries, key=lambda e: e.get("created_at", ""), reverse=True)

    def create_swap_request(
        self,
        requester_id: int,
        requester_date: str,
        partner_id: int,
        partner_date: str,
        note: str = "",
        status: str = "pending",
        created_by: str = "system",
    ) -> dict:
        import datetime as _dt

        now = _dt.datetime.now().isoformat(timespec="seconds")
        entries = self._load_swap_requests()
        new_id = max((e.get("id", 0) for e in entries), default=0) + 1
        entry = {
            "id": new_id,
            "requester_id": requester_id,
            "requester_date": requester_date,
            "partner_id": partner_id,
            "partner_date": partner_date,
            "note": note,
            "status": status,  # pending_partner | pending | approved | rejected | cancelled | expired
            "partner_accepted": None if status == "pending_partner" else True,
            "created_at": now,
            "resolved_at": None,
            "resolved_by": None,
            "reject_reason": "",
            "status_history": [
                {
                    "status": status,
                    "changed_at": now,
                    "changed_by": created_by,
                    "reason": "",
                }
            ],
        }
        entries.append(entry)
        self._save_swap_requests(entries)
        return entry

    def _add_status_history(
        self,
        entry: dict,
        new_status: str,
        changed_by: str,
        reason: str = "",
    ) -> None:
        """Append a status change record to entry['status_history'] (in-place)."""
        import datetime as _dt

        if "status_history" not in entry:
            entry["status_history"] = []
        entry["status_history"].append(
            {
                "status": new_status,
                "changed_at": _dt.datetime.now().isoformat(timespec="seconds"),
                "changed_by": changed_by,
                "reason": reason,
            }
        )

    def get_swap_request_history(self, swap_id: int) -> list[dict] | None:
        """Liefert die Status-Historie eines Tauschantrags, sonst None."""
        entries = self._load_swap_requests()
        for entry in entries:
            if entry.get("id") == swap_id:
                return entry.get("status_history", [])
        return None

    def partner_respond_swap(
        self, swap_id: int, accept: bool, partner_name: str = "partner"
    ) -> dict | None:
        """Partner accepts or declines a swap request.
        If accepted: status changes from pending_partner → pending.
        If declined: status changes to rejected (by partner)."""
        import datetime as _dt

        entries = self._load_swap_requests()
        for entry in entries:
            if entry.get("id") == swap_id:
                if entry["status"] != "pending_partner":
                    return None
                entry["partner_accepted"] = accept
                if accept:
                    new_status = "pending"
                    reason = ""
                else:
                    new_status = "rejected"
                    reason = "Partner hat abgelehnt"
                    entry["resolved_at"] = _dt.datetime.now().isoformat(
                        timespec="seconds"
                    )
                    entry["resolved_by"] = partner_name
                    entry["reject_reason"] = reason
                entry["status"] = new_status
                self._add_status_history(entry, new_status, partner_name, reason)
                self._save_swap_requests(entries)
                return entry
        return None

    def resolve_swap_request(
        self,
        swap_id: int,
        action: str,
        resolved_by: str = "planner",
        reject_reason: str = "",
    ) -> dict | None:
        """action: 'approve' or 'reject'"""
        import datetime as _dt

        entries = self._load_swap_requests()
        for entry in entries:
            if entry.get("id") == swap_id:
                if entry["status"] != "pending":
                    return None  # already resolved
                new_status = "approved" if action == "approve" else "rejected"
                entry["status"] = new_status
                entry["resolved_at"] = _dt.datetime.now().isoformat(timespec="seconds")
                entry["resolved_by"] = resolved_by
                entry["reject_reason"] = reject_reason
                self._add_status_history(entry, new_status, resolved_by, reject_reason)
                self._save_swap_requests(entries)
                return entry
        return None

    def cancel_swap_request(self, swap_id: int, cancelled_by: str = "user") -> int:
        entries = self._load_swap_requests()
        for entry in entries:
            if entry.get("id") == swap_id:
                entry["status"] = "cancelled"
                self._add_status_history(entry, "cancelled", cancelled_by, "")
                self._save_swap_requests(entries)
                return 1
        return 0

    def expire_old_swap_requests(self, max_age_days: int = 7) -> list[int]:
        """Set status='expired' on all pending/pending_partner requests older than max_age_days.

        Returns list of expired swap IDs.
        """
        import datetime as _dt

        cutoff = _dt.datetime.now() - _dt.timedelta(days=max_age_days)
        entries = self._load_swap_requests()
        expired_ids: list[int] = []
        for entry in entries:
            if entry.get("status") not in ("pending", "pending_partner"):
                continue
            created_raw = entry.get("created_at", "")
            try:
                created = _dt.datetime.fromisoformat(created_raw)
            except (ValueError, TypeError):
                continue
            if created < cutoff:
                entry["status"] = "expired"
                entry["resolved_at"] = _dt.datetime.now().isoformat(timespec="seconds")
                entry["resolved_by"] = "system"
                self._add_status_history(
                    entry,
                    "expired",
                    "system",
                    f"Automatisch abgelaufen nach {max_age_days} Tagen",
                )
                expired_ids.append(entry["id"])
        if expired_ids:
            self._save_swap_requests(entries)
        return expired_ids

    def delete_swap_request(self, swap_id: int) -> int:
        entries = self._load_swap_requests()
        new_entries = [e for e in entries if e.get("id") != swap_id]
        if len(new_entries) == len(entries):
            return 0
        self._save_swap_requests(new_entries)
        return 1

    # ── iCal Feed Tokens ──────────────────────────────────────────

    def _ical_tokens_path(self) -> str:
        """Pfad zur ical_tokens.json im veränderlichen Daten-Verzeichnis.

        Historisch lag die Datei NEBEN den DBFs (``db_path``) — dort gehört
        sie nicht hin: der DBF-Bestand kann read-only gemountet oder ein
        periodisch überschriebener Sync-Spiegel sein; Tokens waren dann nicht
        anlegbar bzw. gingen beim nächsten Sync verloren. Ein Altbestand wird
        beim Laden einmalig übernommen (``_load_ical_tokens``)."""
        return os.path.join(_paths.data_dir(), "ical_tokens.json")

    def _legacy_ical_tokens_path(self) -> str:
        """Alter Speicherort neben den DBFs (nur noch für die Übernahme)."""
        return os.path.join(self.db_path, "ical_tokens.json")

    def _load_ical_tokens(self) -> dict[str, dict]:
        """Load iCal tokens: {token: {employee_id, created_at}}."""
        path = self._ical_tokens_path()
        if not os.path.exists(path):
            # Einmalige Übernahme des Altbestands aus dem DBF-Verzeichnis;
            # die Alt-Datei bleibt liegen (der Bestand kann read-only sein).
            legacy = self._legacy_ical_tokens_path()
            if os.path.exists(legacy):
                try:
                    with open(legacy, encoding="utf-8") as f:
                        data = json.load(f)
                    if data:
                        self._save_ical_tokens(data)
                    return data
                except (json.JSONDecodeError, OSError):
                    return {}
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_ical_tokens(self, data: dict[str, dict]) -> None:
        """Save iCal tokens to JSON file."""
        path = self._ical_tokens_path()
        _atomic_write_json(path, data, ensure_ascii=False, indent=2)

    # ── User→Mitarbeiter-Zuordnung (5USER_EMPLOYEE.json) ────────────────────
    # 5USER trägt keine Mitarbeiter-ID, und das ORIGINAL verknüpft die beiden
    # Tabellen NICHT (5EMACC/5GRACC sind Verwalter-Zugriffslisten, kein „User
    # IST Mitarbeiter"). „Mein Kalender" ist eine App-Neuerung; diese explizite,
    # App-verwaltete Zuordnung ersetzt die fragile Heuristik „Benutzername ==
    # Nachname". Liegt wie die iCal-Tokens im veränderlichen data_dir (der
    # DBF-Bestand kann read-only gemountet/gespiegelt sein).

    def _user_employee_path(self) -> str:
        """Pfad zur 5USER_EMPLOYEE.json im veränderlichen Daten-Verzeichnis."""
        return os.path.join(_paths.data_dir(), "5USER_EMPLOYEE.json")

    def _legacy_user_employee_path(self) -> str:
        """Alter Speicherort neben den DBFs (nur für die einmalige Übernahme)."""
        return os.path.join(self.db_path, "5USER_EMPLOYEE.json")

    def _load_user_employee_links(self) -> dict[str, int]:
        """Lädt die Zuordnung ``{user_id(str): employee_id(int)}``."""
        path = self._user_employee_path()
        if not os.path.exists(path):
            legacy = self._legacy_user_employee_path()
            if os.path.exists(legacy):
                try:
                    with open(legacy, encoding="utf-8") as f:
                        data = json.load(f)
                    if data:
                        self._save_user_employee_links(data)
                    return data
                except (json.JSONDecodeError, OSError):
                    return {}
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_user_employee_links(self, data: dict) -> None:
        path = self._user_employee_path()
        _atomic_write_json(path, data, ensure_ascii=False, indent=2)

    def get_linked_employee_id(self, user_id: int) -> int | None:
        """Explizit zugeordnete Mitarbeiter-ID eines Users, sonst None."""
        val = self._load_user_employee_links().get(str(user_id))
        return int(val) if val is not None else None

    def set_user_employee_link(self, user_id: int, employee_id: int) -> None:
        """Setzt/überschreibt die Zuordnung User→Mitarbeiter."""
        links = self._load_user_employee_links()
        links[str(user_id)] = int(employee_id)
        self._save_user_employee_links(links)

    def delete_user_employee_link(self, user_id: int) -> bool:
        """Entfernt die Zuordnung; True, wenn eine bestand."""
        links = self._load_user_employee_links()
        if str(user_id) in links:
            del links[str(user_id)]
            self._save_user_employee_links(links)
            return True
        return False

    def get_ical_token_for_employee(self, employee_id: int) -> str | None:
        """Liefert das aktuelle iCal-Feed-Token eines Mitarbeiters, sonst None."""
        tokens = self._load_ical_tokens()
        for token, info in tokens.items():
            if info.get("employee_id") == employee_id:
                return token
        return None

    def create_ical_token(self, employee_id: int) -> str:
        """Erzeugt ein neues iCal-Feed-Token für einen Mitarbeiter.

        Widerruft zuvor ein evtl. bestehendes Token dieses MA.
        Liefert den neuen Token-String.
        """
        import secrets

        tokens = self._load_ical_tokens()
        # Bestehendes Token dieses MA widerrufen
        tokens = {
            t: info
            for t, info in tokens.items()
            if info.get("employee_id") != employee_id
        }
        # Generate new token (URL-safe, 32 bytes = 43 chars)
        new_token = secrets.token_urlsafe(32)
        tokens[new_token] = {
            "employee_id": employee_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._save_ical_tokens(tokens)
        return new_token

    def revoke_ical_token(self, employee_id: int) -> bool:
        """Widerruft das iCal-Feed-Token eines MA. Liefert True, wenn eines widerrufen wurde."""
        tokens = self._load_ical_tokens()
        new_tokens = {
            t: info
            for t, info in tokens.items()
            if info.get("employee_id") != employee_id
        }
        if len(new_tokens) == len(tokens):
            return False
        self._save_ical_tokens(new_tokens)
        return True

    def resolve_ical_token(self, token: str) -> int | None:
        """Schlägt ein iCal-Token nach und liefert die employee_id, sonst None."""
        tokens = self._load_ical_tokens()
        info = tokens.get(token)
        if info is None:
            return None
        return info.get("employee_id")

    # ── Schichtplan-Kommentare (Q069) ─────────────────────────

    def _schedule_comments_path(self) -> str:
        return os.path.join(_paths.data_dir(), "schedule_comments.json")

    def _load_schedule_comments(self) -> list[dict]:
        path = self._schedule_comments_path()
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_schedule_comments(self, entries: list[dict]) -> None:
        path = self._schedule_comments_path()
        _atomic_write_json(path, entries, ensure_ascii=False, indent=2)

    def get_schedule_comments(
        self,
        group_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict]:
        """Liefert Plan-Kommentare, optional nach Gruppe und Zeitraum gefiltert."""
        entries = self._load_schedule_comments()
        if group_id is not None:
            entries = [e for e in entries if e.get("group_id") == group_id]
        if date_from:
            entries = [e for e in entries if e.get("date", "") >= date_from]
        if date_to:
            entries = [e for e in entries if e.get("date", "") <= date_to]
        return entries

    def add_schedule_comment(
        self,
        date: str,
        group_id: int,
        text: str,
        author: str = "",
    ) -> dict:
        """Fügt einen Kommentar zu Datum+Gruppe hinzu; ersetzt einen bestehenden desselben Datums+Gruppe."""
        entries = self._load_schedule_comments()
        # Bestehenden Kommentar desselben Datums+Gruppe entfernen (Regel: einer je Tag und Gruppe)
        entries = [
            e for e in entries
            if not (e.get("date") == date and e.get("group_id") == group_id)
        ]
        # Generate new id
        max_id = max((e.get("id", 0) for e in entries), default=0)
        new_entry = {
            "id": max_id + 1,
            "date": date,
            "group_id": group_id,
            "text": text,
            "author": author,
            "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        }
        entries.append(new_entry)
        self._save_schedule_comments(entries)
        return new_entry

    def delete_schedule_comment(self, comment_id: int) -> int:
        """Delete a comment by ID. Returns number deleted (0 or 1)."""
        entries = self._load_schedule_comments()
        new_entries = [e for e in entries if e.get("id") != comment_id]
        deleted = len(entries) - len(new_entries)
        if deleted:
            self._save_schedule_comments(new_entries)
        return deleted
