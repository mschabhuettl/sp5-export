"""
PostgreSQL database backend for OpenSchichtplaner5.

Implements the same public API as SP5Database (database.py) but backed by
PostgreSQL via SQLAlchemy ORM. This enables a seamless switch between
DBF and PostgreSQL without changing any router code.

Usage:
    from sp5lib.pg_database import SP5PostgresDatabase
    db = SP5PostgresDatabase("postgresql://user:pass@host:5432/sp5")
    employees = db.get_employees()
"""

import calendar
import dataclasses
import hashlib
import json
import logging
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker

from . import calculations as calc
from .color_utils import bgr_to_hex, is_light_color
from .database import SP5Database
from .orm.base import Base
from .orm.models import Employee, Group, GroupAssignment
from .orm.models_pg import (
    Absence,
    AccountBooking,
    ChangelogEntry,
    Cycle,
    CycleAssignment,
    CycleEntry,
    ExtraCharge,
    Holiday,
    LeaveEntitlement,
    LeaveType,
    Note,
    OvertimeEntry,
    ScheduleEntry,
    Shift,
    ShiftDemand,
    SpecialDemand,
    SpecialShift,
    User,
    Workplace,
)

_log = logging.getLogger("sp5api.pg")


class SP5PostgresDatabase:
    """PostgreSQL-Datenbank mit derselben API wie SP5Database."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self._engine = create_engine(
            database_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )
        self._SessionFactory = sessionmaker(bind=self._engine)
        # Für Kompatibilität mit SP5Database.db_path-Referenzen
        self.db_path = ""

    def init_db(self):
        """Create all tables."""
        Base.metadata.create_all(self._engine)

    @contextmanager
    def _session(self):
        """Context-Manager für eine transaktionale Session."""
        session = self._SessionFactory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _color_fields(self, record: dict) -> dict:
        """Convert BGR color fields to hex strings."""
        for key in ("COLORTEXT", "COLORBAR", "COLORBK", "CBKLABEL", "CBKSCHED", "CFGLABEL"):
            if key in record and isinstance(record[key], int):
                record[key + "_HEX"] = bgr_to_hex(record[key])
                record[key + "_LIGHT"] = is_light_color(record[key])
        return record

    # ── Berechnungsschicht-Adapter (sp5lib.calculations, Spec Kap. 3) ──
    # Dieselben calc-Aufrufe wie SP5Database (database.py); ORM-Zeilen werden
    # über to_dict() in die DBF-Schlüsselform der Berechnungsschicht gebracht.

    def _calc_holidays(self) -> dict[date, int]:
        """Feiertags-Spiegel als date->INTERVAL-Kalender (wie SP5Database)."""
        with self._session() as s:
            rows = [h.to_dict() for h in s.scalars(select(Holiday)).all()]
        return calc.holiday_calendar(rows)

    @staticmethod
    def _calc_context(emp: dict) -> calc.EmployeeContext:
        """5EMPL-Berechnungsparameter aus dem ORM-Dict.

        Das PG-Schema führt (noch) keine CALCBASE/HRSTOTAL/DEDUCTHOL-Spalten.
        Brücke: HRSMONTH > 0 wird als Monatsbasis (CALCBASE=2) gerechnet —
        das erhält die bisherige PG-Semantik ("HRSMONTH dominiert"), jetzt
        mit korrekter Monats-Zerlegung (Spec 3.3.4); sonst Tagesbasis.
        """
        ctx = SP5Database._calc_context(emp)
        if "CALCBASE" not in emp and ctx.hrs_month > calc.EPSILON:
            ctx = dataclasses.replace(ctx, calcbase=2)
        return ctx

    def _movement_by_employee(
        self, model, von: date, bis: date, employee_id: int | None = None
    ) -> dict[int, list[dict]]:
        """Bewegungsdaten (date-Spalte) je Mitarbeiter im Zeitraum [von, bis]."""
        lo, hi = von.isoformat(), bis.isoformat()
        out: dict[int, list[dict]] = {}
        with self._session() as s:
            stmt = select(model).where(model.date >= lo, model.date <= hi)
            if employee_id is not None:
                stmt = stmt.where(model.employee_id == employee_id)
            for r in s.scalars(stmt).all():
                out.setdefault(r.employee_id, []).append(r.to_dict())
        return out

    def _cycle_shifts_by_employee(
        self, von: date, bis: date, employee_id: int | None = None
    ) -> dict[int, list[dict]]:
        """Expandierte Zyklusdienste je Mitarbeiter (wie SP5Database).

        Tage mit eigenem schedule_entries-Satz werden übersprungen (Schutz
        gegen Doppelzählung materialisierter Pläne). 5CYEXC hat im ORM-Spiegel
        keine Tabelle; Ausnahmen entfallen daher im PG-Pfad.
        """
        with self._session() as s:
            stmt = select(CycleAssignment)
            if employee_id is not None:
                stmt = stmt.where(CycleAssignment.employee_id == employee_id)
            assignments = []
            for a in s.scalars(stmt).all():
                rec = a.to_dict()
                entrance = str(rec.get("ENTRANCE") or "").strip()
                # defensiv: Altbestand mit Nicht-Zahl im Feld → Einstieg 0
                rec["ENTRANCE"] = int(entrance) if entrance.lstrip("-").isdigit() else 0
                assignments.append(rec)
            if not assignments:
                return {}
            cycles = [c.to_dict() for c in s.scalars(select(Cycle)).all()]
            entries = [
                {
                    "CYCLEEID": e.cycle_id,
                    "INDEX": e.index,
                    "SHIFTID": e.shift_id,
                    "WORKPLACID": e.workplace_id,
                }
                for e in s.scalars(select(CycleEntry)).all()
            ]
            mashi_days = {
                (r.employee_id, r.date[:10])
                for r in s.scalars(select(ScheduleEntry)).all()
            }
        expanded = calc.expand_cycle_assignments(
            assignments, cycles=cycles, cycle_entries=entries, von=von, bis=bis
        )
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

        Plan-Quellen mit einem Tag Vorlauf wie in SP5Database (Spec 3.4.3
        Nr. 8, Tageswechsel-Fenster des Vortags).
        """
        margin = von - timedelta(days=1)
        return {
            "holidays": self._calc_holidays(),
            "shifts_by_id": {s["ID"]: s for s in self.get_shifts(include_hidden=True)},
            "leave_types_by_id": {
                lt["ID"]: lt for lt in self.get_leave_types(include_hidden=True)
            },
            "manual": self._movement_by_employee(ScheduleEntry, margin, bis, employee_id),
            "special": self._movement_by_employee(SpecialShift, margin, bis, employee_id),
            "cycle": self._cycle_shifts_by_employee(margin, bis, employee_id),
            "absences": self._movement_by_employee(Absence, von, bis, employee_id),
            "bookings": self._movement_by_employee(AccountBooking, von, bis, employee_id),
            "overtimes": self._movement_by_employee(OvertimeEntry, von, bis, employee_id),
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

    # ── Geteilte Berechnungs-Fassaden (Code aus SP5Database) ──────────
    # Diese database.py-Methoden greifen ausschließlich über die oben
    # gespiegelten Adapter (_calc_inputs/_plan_kwargs/_calc_context/...) und
    # Fassaden-Leser (get_employee/get_shifts/get_extracharges/...) auf die
    # Daten zu — die Wiederverwendung derselben Funktionsobjekte hält beide
    # Backends per Konstruktion äquivalent (geprüft in test_pg_calculations).
    calculate_time_balance = SP5Database.calculate_time_balance
    _time_balance_from_inputs = SP5Database._time_balance_from_inputs
    get_zeitkonto = SP5Database.get_zeitkonto
    get_employee_stats_year = SP5Database.get_employee_stats_year
    get_employee_stats_month = SP5Database.get_employee_stats_month
    get_schedule_year = SP5Database.get_schedule_year
    calculate_extracharge_hours = SP5Database.calculate_extracharge_hours
    get_leave_balance_group = SP5Database.get_leave_balance_group
    _is_night_shift = SP5Database._is_night_shift
    _decode_startend = staticmethod(SP5Database._decode_startend)
    _time_str_to_minutes = staticmethod(SP5Database._time_str_to_minutes)

    # ── Employees ──────────────────────────────────────────────

    def get_employees(self, include_hidden: bool = False) -> list[dict]:
        with self._session() as s:
            # Original-Default "Sortierung > Name" - identisch zum DBF-Pfad
            # (SP5Database.get_employees), sonst bricht die Aequivalenz.
            stmt = select(Employee).order_by(
                func.lower(func.coalesce(Employee.name, "")),
                func.lower(func.coalesce(Employee.firstname, "")),
                Employee.position,
            )
            if not include_hidden:
                stmt = stmt.where(Employee.hide == False)
            rows = s.scalars(stmt).all()
            result = []
            for emp in rows:
                r = emp.to_dict()
                wd = r.get("WORKDAYS", "")
                r["WORKDAYS_LIST"] = [x == "1" for x in wd.split()] if wd else []
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
                    r["SHORTNAME_GENERATED"] = True
                else:
                    r["SHORTNAME_GENERATED"] = False
                self._color_fields(r)
                result.append(r)
            return result

    def get_employee(self, emp_id: int) -> dict | None:
        for e in self.get_employees(include_hidden=True):
            if e.get("ID") == emp_id:
                return e
        return None

    def create_employee(self, data: dict) -> dict:
        with self._session() as s:
            # Uniqueness check
            shortname = (data.get("SHORTNAME") or "").strip().upper()
            if shortname:
                existing = s.scalars(
                    select(Employee).where(Employee.hide == False, func.upper(Employee.shortname) == shortname)
                ).first()
                if existing:
                    raise ValueError(f"DUPLICATE:SHORTNAME:{shortname}")

            max_pos = s.scalar(select(func.max(Employee.position))) or 0
            emp = Employee(
                position=data.get("POSITION", max_pos + 1),
                number=data.get("NUMBER", ""),
                name=data.get("NAME", ""),
                firstname=data.get("FIRSTNAME", ""),
                shortname=data.get("SHORTNAME", ""),
                sex=data.get("SEX", 0),
                hrsday=data.get("HRSDAY", 0.0),
                hrsweek=data.get("HRSWEEK", 0.0),
                hrsmonth=data.get("HRSMONTH", 0.0),
                workdays=data.get("WORKDAYS", "1 1 1 1 1 0 0 0"),
                hide=bool(data.get("HIDE")),
                email=data.get("EMAIL", ""),
                phone=data.get("PHONE", ""),
                function=data.get("FUNCTION", ""),
                salutation=data.get("SALUTATION", ""),
                street=data.get("STREET", ""),
                zip=data.get("ZIP", ""),
                town=data.get("TOWN", ""),
                birthday=data.get("BIRTHDAY"),
                empstart=data.get("EMPSTART"),
                empend=data.get("EMPEND"),
                note1=data.get("NOTE1", ""),
                note2=data.get("NOTE2", ""),
                note3=data.get("NOTE3", ""),
                note4=data.get("NOTE4", ""),
            )
            s.add(emp)
            s.flush()
            return {**emp.to_dict(), "id": emp.id}

    def update_employee(self, emp_id: int, data: dict) -> dict:
        with self._session() as s:
            emp = s.get(Employee, emp_id)
            if emp is None:
                raise ValueError(f"Employee {emp_id} not found")
            updatable = (
                "NAME", "FIRSTNAME", "SHORTNAME", "NUMBER", "SEX",
                "HRSDAY", "HRSWEEK", "HRSMONTH", "HRSTOTAL", "WORKDAYS",
                "HIDE", "BOLD", "POSITION", "SALUTATION", "STREET", "ZIP",
                "TOWN", "PHONE", "EMAIL", "FUNCTION", "BIRTHDAY", "EMPSTART",
                "EMPEND", "CALCBASE", "DEDUCTHOL", "NOTE1", "NOTE2", "NOTE3",
                "NOTE4", "PHOTO",
            )
            update_data = {}
            for key in updatable:
                if key in data and data[key] is not None:
                    attr = key.lower()
                    if hasattr(emp, attr):
                        setattr(emp, attr, data[key])
                        update_data[key] = data[key]
            s.flush()
            return {"id": emp_id, **update_data}

    def delete_employee(self, emp_id: int) -> int:
        with self._session() as s:
            emp = s.get(Employee, emp_id)
            if emp is None:
                return 0
            emp.hide = True
            s.flush()
            return 1

    def activate_employee(self, emp_id: int) -> int:
        with self._session() as s:
            emp = s.get(Employee, emp_id)
            if emp is None:
                return 0
            emp.hide = False
            s.flush()
            return 1

    # ── Groups ─────────────────────────────────────────────────

    def get_groups(self, include_hidden: bool = False) -> list[dict]:
        with self._session() as s:
            stmt = select(Group).order_by(Group.position)
            if not include_hidden:
                stmt = stmt.where(Group.hide == False)
            rows = s.scalars(stmt).all()
            result = []
            for g in rows:
                r = g.to_dict()
                self._color_fields(r)
                result.append(r)
            return result

    def get_group_members(self, group_id: int) -> list[int]:
        with self._session() as s:
            stmt = select(GroupAssignment.employee_id).where(GroupAssignment.group_id == group_id)
            return list(s.scalars(stmt).all())

    def get_all_group_members(self) -> dict[int, list[int]]:
        with self._session() as s:
            rows = s.execute(select(GroupAssignment.group_id, GroupAssignment.employee_id)).all()
            result: dict[int, list[int]] = {}
            for gid, eid in rows:
                result.setdefault(gid, []).append(eid)
            return result

    def get_employee_groups(self, emp_id: int) -> list[int]:
        with self._session() as s:
            stmt = select(GroupAssignment.group_id).where(GroupAssignment.employee_id == emp_id)
            return list(s.scalars(stmt).all())

    def create_group(self, data: dict) -> dict:
        with self._session() as s:
            max_pos = s.scalar(select(func.max(Group.position))) or 0
            g = Group(
                name=data.get("NAME", ""),
                shortname=data.get("SHORTNAME", ""),
                super_id=data.get("SUPERID", 0) or None,
                position=data.get("POSITION", max_pos + 1),
                hide=bool(data.get("HIDE")),
            )
            s.add(g)
            s.flush()
            return {**g.to_dict(), "id": g.id}

    def update_group(self, group_id: int, data: dict) -> dict:
        with self._session() as s:
            g = s.get(Group, group_id)
            if g is None:
                raise ValueError(f"Group {group_id} not found")
            update_data = {}
            for key in ("NAME", "SHORTNAME", "SUPERID", "POSITION", "HIDE"):
                if key in data and data[key] is not None:
                    attr = key.lower()
                    if key == "SUPERID":
                        attr = "super_id"
                    if hasattr(g, attr):
                        setattr(g, attr, data[key])
                        update_data[key] = data[key]
            s.flush()
            return {"id": group_id, **update_data}

    def delete_group(self, group_id: int) -> int:
        with self._session() as s:
            g = s.get(Group, group_id)
            if g is None:
                return 0
            g.hide = True
            # Remove memberships
            s.execute(delete(GroupAssignment).where(GroupAssignment.group_id == group_id))
            s.flush()
            return 1

    def add_group_member(self, group_id: int, employee_id: int) -> dict:
        with self._session() as s:
            existing = s.scalars(
                select(GroupAssignment).where(
                    GroupAssignment.group_id == group_id,
                    GroupAssignment.employee_id == employee_id,
                )
            ).first()
            if existing:
                return {"id": existing.id, "group_id": group_id, "employee_id": employee_id}
            ga = GroupAssignment(group_id=group_id, employee_id=employee_id)
            s.add(ga)
            s.flush()
            return {"ID": ga.id, "GROUPID": group_id, "EMPLOYEEID": employee_id}

    def remove_group_member(self, group_id: int, employee_id: int) -> int:
        with self._session() as s:
            result = s.execute(
                delete(GroupAssignment).where(
                    GroupAssignment.group_id == group_id,
                    GroupAssignment.employee_id == employee_id,
                )
            )
            return result.rowcount

    def get_all_group_assignments(self) -> list[dict]:
        with self._session() as s:
            rows = s.execute(select(GroupAssignment.employee_id, GroupAssignment.group_id)).all()
            return [{"employee_id": eid, "group_id": gid} for eid, gid in rows]

    # ── Shifts ─────────────────────────────────────────────────

    def get_shifts(self, include_hidden: bool = False) -> list[dict]:
        with self._session() as s:
            stmt = select(Shift).order_by(Shift.position)
            if not include_hidden:
                stmt = stmt.where(Shift.hide == False)
            rows = s.scalars(stmt).all()
            result = []
            for sh in rows:
                r = sh.to_dict()
                self._color_fields(r)
                # Parse TIMES_BY_WEEKDAY
                times: dict[int, Any] = {}
                for i in range(7):
                    val = r.get(f"STARTEND{i}", "").strip()
                    if val and "-" in val:
                        parts = val.split("-")
                        if len(parts) == 2:
                            times[i] = {"start": parts[0].strip(), "end": parts[1].strip()}
                        else:
                            times[i] = None
                    else:
                        times[i] = None
                r["TIMES_BY_WEEKDAY"] = times
                result.append(r)
            return result

    def get_shift(self, shift_id: int) -> dict | None:
        for s in self.get_shifts(include_hidden=True):
            if s.get("ID") == shift_id:
                return s
        return None

    def create_shift(self, data: dict) -> dict:
        with self._session() as s:
            name_lower = (data.get("NAME") or "").strip().lower()
            existing = s.scalars(
                select(Shift).where(Shift.hide == False, func.lower(Shift.name) == name_lower)
            ).first()
            if existing:
                raise ValueError(f"DUPLICATE:SHIFTNAME:{data.get('NAME')}")
            max_pos = s.scalar(select(func.max(Shift.position))) or 0
            sh = Shift(
                name=data.get("NAME", ""),
                shortname=data.get("SHORTNAME", ""),
                position=data.get("POSITION", max_pos + 1),
                colortext=data.get("COLORTEXT", 0),
                colorbar=data.get("COLORBAR", 0),
                colorbk=data.get("COLORBK", 16777215),
                duration0=data.get("DURATION0", 0.0),
                hide=bool(data.get("HIDE")),
            )
            for i in range(1, 8):
                if f"DURATION{i}" in data:
                    setattr(sh, f"duration{i}", data[f"DURATION{i}"])
                if f"STARTEND{i}" in data:
                    setattr(sh, f"startend{i}", data[f"STARTEND{i}"])
            if "STARTEND0" in data:
                sh.startend0 = data["STARTEND0"]
            s.add(sh)
            s.flush()
            return {**sh.to_dict(), "id": sh.id}

    def update_shift(self, shift_id: int, data: dict) -> dict:
        with self._session() as s:
            sh = s.get(Shift, shift_id)
            if sh is None:
                raise ValueError(f"Shift {shift_id} not found")
            update_data = {}
            for key in ("NAME", "SHORTNAME", "POSITION", "COLORTEXT", "COLORBAR", "COLORBK", "DURATION0", "HIDE"):
                if key in data:
                    setattr(sh, key.lower(), data[key])
                    update_data[key] = data[key]
            for i in range(8):
                for prefix in ("DURATION", "STARTEND"):
                    k = f"{prefix}{i}"
                    if k in data:
                        setattr(sh, k.lower(), data[k])
                        update_data[k] = data[k]
            s.flush()
            return {"id": shift_id, **update_data}

    def hide_shift(self, shift_id: int) -> int:
        with self._session() as s:
            sh = s.get(Shift, shift_id)
            if sh is None:
                return 0
            sh.hide = True
            s.flush()
            return 1

    # ── Leave Types ────────────────────────────────────────────

    def get_leave_types(self, include_hidden: bool = False) -> list[dict]:
        with self._session() as s:
            stmt = select(LeaveType).order_by(LeaveType.position)
            if not include_hidden:
                stmt = stmt.where(LeaveType.hide == False)
            rows = s.scalars(stmt).all()
            result = []
            for lt in rows:
                r = lt.to_dict()
                self._color_fields(r)
                result.append(r)
            return result

    def get_leave_type(self, lt_id: int) -> dict | None:
        for lt in self.get_leave_types(include_hidden=True):
            if lt.get("ID") == lt_id:
                return lt
        return None

    def create_leave_type(self, data: dict) -> dict:
        with self._session() as s:
            max_pos = s.scalar(select(func.max(LeaveType.position))) or 0
            lt = LeaveType(
                name=data.get("NAME", ""),
                shortname=data.get("SHORTNAME", ""),
                position=data.get("POSITION", max_pos + 1),
                colortext=data.get("COLORTEXT", 0),
                colorbar=data.get("COLORBAR", 0),
                colorbk=data.get("COLORBK", 16777215),
                entitled=bool(data.get("ENTITLED")),
                stdentit=data.get("STDENTIT", 0.0),
                hide=bool(data.get("HIDE")),
            )
            s.add(lt)
            s.flush()
            return {**lt.to_dict(), "id": lt.id}

    def update_leave_type(self, lt_id: int, data: dict) -> dict:
        with self._session() as s:
            lt = s.get(LeaveType, lt_id)
            if lt is None:
                raise ValueError(f"LeaveType {lt_id} not found")
            update_data = {}
            for key in ("NAME", "SHORTNAME", "POSITION", "COLORTEXT", "COLORBAR", "COLORBK", "ENTITLED", "STDENTIT", "HIDE"):
                if key in data:
                    setattr(lt, key.lower(), data[key])
                    update_data[key] = data[key]
            s.flush()
            return {"id": lt_id, **update_data}

    def hide_leave_type(self, lt_id: int) -> int:
        with self._session() as s:
            lt = s.get(LeaveType, lt_id)
            if lt is None:
                return 0
            lt.hide = True
            s.flush()
            return 1

    # ── Workplaces ─────────────────────────────────────────────

    def get_workplaces(self, include_hidden: bool = False) -> list[dict]:
        with self._session() as s:
            stmt = select(Workplace).order_by(Workplace.position)
            if not include_hidden:
                stmt = stmt.where(Workplace.hide == False)
            rows = s.scalars(stmt).all()
            result = []
            for wp in rows:
                r = wp.to_dict()
                self._color_fields(r)
                result.append(r)
            return result

    def create_workplace(self, data: dict) -> dict:
        with self._session() as s:
            max_pos = s.scalar(select(func.max(Workplace.position))) or 0
            wp = Workplace(
                name=data.get("NAME", ""),
                shortname=data.get("SHORTNAME", ""),
                position=data.get("POSITION", max_pos + 1),
                colortext=data.get("COLORTEXT", 0),
                colorbar=data.get("COLORBAR", 0),
                colorbk=data.get("COLORBK", 16777215),
                hide=bool(data.get("HIDE")),
            )
            s.add(wp)
            s.flush()
            return {**wp.to_dict(), "id": wp.id}

    def update_workplace(self, wp_id: int, data: dict) -> dict:
        with self._session() as s:
            wp = s.get(Workplace, wp_id)
            if wp is None:
                raise ValueError(f"Workplace {wp_id} not found")
            update_data = {}
            for key in ("NAME", "SHORTNAME", "POSITION", "COLORTEXT", "COLORBAR", "COLORBK", "HIDE"):
                if key in data:
                    setattr(wp, key.lower(), data[key])
                    update_data[key] = data[key]
            s.flush()
            return {"id": wp_id, **update_data}

    def hide_workplace(self, wp_id: int) -> int:
        with self._session() as s:
            wp = s.get(Workplace, wp_id)
            if wp is None:
                return 0
            wp.hide = True
            s.flush()
            return 1

    # ── Holidays ───────────────────────────────────────────────

    def get_holidays(self, year: int | None = None) -> list[dict]:
        with self._session() as s:
            rows = s.scalars(select(Holiday)).all()
            result = []
            for h in rows:
                r = h.to_dict()
                if year is not None:
                    year_str = str(year)
                    if h.interval == 1:
                        if h.date and len(h.date) >= 10:
                            r["DATE"] = year_str + h.date[4:]
                        result.append(r)
                    elif h.date.startswith(year_str):
                        result.append(r)
                else:
                    result.append(r)
            result.sort(key=lambda x: x.get("DATE", ""))
            return result

    def get_holiday_dates(self, year: int) -> set:
        return {r["DATE"] for r in self.get_holidays(year) if r.get("DATE")}

    def create_holiday(self, data: dict, repeat_years: int = 0) -> dict:
        from datetime import datetime as _dt

        from .database import SP5Database

        interval = SP5Database._validate_holiday_interval(data.get("INTERVAL", 0))
        with self._session() as s:
            h = Holiday(date=data.get("DATE", ""), name=data.get("NAME", ""), interval=interval)
            s.add(h)
            s.flush()
            repeated_ids: list[int] = []
            if repeat_years > 0:
                base = _dt.strptime(h.date, "%Y-%m-%d").date()
                for offset in range(1, repeat_years + 1):
                    try:
                        d = base.replace(year=base.year + offset)
                    except ValueError:  # 29.02. in Nicht-Schaltjahr
                        continue
                    extra = Holiday(date=d.isoformat(), name=h.name, interval=interval)
                    s.add(extra)
                    s.flush()
                    repeated_ids.append(extra.id)
            result = {**h.to_dict(), "id": h.id}
            if repeated_ids:
                result["repeated_ids"] = repeated_ids
            return result

    def update_holiday(self, holiday_id: int, data: dict) -> dict:
        from .database import SP5Database

        if "INTERVAL" in data:
            data = {**data, "INTERVAL": SP5Database._validate_holiday_interval(data["INTERVAL"])}
        with self._session() as s:
            h = s.get(Holiday, holiday_id)
            if h is None:
                raise ValueError(f"Holiday {holiday_id} not found")
            update_data = {}
            for key in ("DATE", "NAME", "INTERVAL"):
                if key in data:
                    setattr(h, key.lower(), data[key])
                    update_data[key] = data[key]
            s.flush()
            return {"id": holiday_id, **update_data}

    def delete_holiday(self, holiday_id: int) -> int:
        with self._session() as s:
            h = s.get(Holiday, holiday_id)
            if h is None:
                return 0
            s.delete(h)
            s.flush()
            return 1

    # ── Schedule ───────────────────────────────────────────────

    def get_schedule(self, year: int, month: int, group_id: int | None = None) -> list[dict]:
        prefix = f"{year:04d}-{month:02d}"
        entries = []
        with self._session() as s:
            # MASHI
            for r in s.scalars(select(ScheduleEntry).where(ScheduleEntry.date.startswith(prefix))).all():
                entries.append({
                    "employee_id": r.employee_id, "date": r.date, "kind": "shift",
                    "shift_id": r.shift_id, "workplace_id": r.workplace_id, "leave_type_id": None,
                })
            # SPSHI
            for r in s.scalars(select(SpecialShift).where(SpecialShift.date.startswith(prefix))).all():
                entries.append({
                    "employee_id": r.employee_id, "date": r.date, "kind": "special_shift",
                    "shift_id": r.shift_id, "workplace_id": r.workplace_id, "leave_type_id": None,
                    "custom_name": r.name, "custom_short": r.shortname,
                    "color_bk": bgr_to_hex(r.colorbk) if r.colorbk else None,
                    "color_text": bgr_to_hex(r.colortext) if r.colortext else None,
                })
            # ABSEN (Teiltage A10: interval/start_time/end_time wie im DBF-Backend)
            for r in s.scalars(select(Absence).where(Absence.date.startswith(prefix))).all():
                interval = int(r.interval or 0)
                entries.append({
                    "employee_id": r.employee_id, "date": r.date, "kind": "absence",
                    "shift_id": None, "workplace_id": None, "leave_type_id": r.leave_type_id,
                    "interval": interval,
                    "start_time": int(r.start or 0) if interval == 3 else 0,
                    "end_time": int(r.end or 0) if interval == 3 else 0,
                })

        shifts_map = {sh["ID"]: sh for sh in self.get_shifts(include_hidden=True)}
        lt_map = {lt["ID"]: lt for lt in self.get_leave_types(include_hidden=True)}

        for e in entries:
            if e["shift_id"] and e["shift_id"] in shifts_map:
                sh = shifts_map[e["shift_id"]]
                e["display_name"] = sh.get("SHORTNAME", sh.get("NAME", ""))
                e["color_bk"] = e.get("color_bk") or bgr_to_hex(sh.get("COLORBK", 16777215))
                e["color_text"] = e.get("color_text") or bgr_to_hex(sh.get("COLORTEXT", 0))
                e["shift_name"] = sh.get("NAME", "")
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

        if group_id is not None:
            member_ids = set(self.get_group_members(group_id))
            entries = [e for e in entries if e["employee_id"] in member_ids]

        return entries

    def add_schedule_entry(self, employee_id: int, date_str: str, shift_id: int) -> dict:
        with self._session() as s:
            existing = s.scalars(
                select(ScheduleEntry).where(
                    ScheduleEntry.employee_id == employee_id,
                    ScheduleEntry.date == date_str,
                )
            ).first()
            if existing:
                raise ValueError(
                    f"Schedule entry for employee {employee_id} on {date_str} already exists."
                )
            entry = ScheduleEntry(employee_id=employee_id, date=date_str, shift_id=shift_id)
            s.add(entry)
            s.flush()
            return {"ID": entry.id, "EMPLOYEEID": employee_id, "DATE": date_str, "SHIFTID": shift_id}

    def delete_schedule_entry(self, employee_id: int, date_str: str) -> int:
        count = 0
        with self._session() as s:
            count += s.execute(
                delete(ScheduleEntry).where(ScheduleEntry.employee_id == employee_id, ScheduleEntry.date == date_str)
            ).rowcount
            count += s.execute(
                delete(SpecialShift).where(SpecialShift.employee_id == employee_id, SpecialShift.date == date_str)
            ).rowcount
            count += s.execute(
                delete(Absence).where(Absence.employee_id == employee_id, Absence.date == date_str)
            ).rowcount
        return count

    def add_absence(
        self,
        employee_id: int,
        date_str: str,
        leave_type_id: int,
        interval: int = 0,
        start: int = 0,
        end: int = 0,
    ) -> dict:
        from .database import SP5Database

        start, end = SP5Database._validate_absence_interval(interval, start, end)
        with self._session() as s:
            existing = s.scalars(
                select(Absence).where(Absence.employee_id == employee_id, Absence.date == date_str)
            ).first()
            if existing:
                raise ValueError(f"Absence for employee {employee_id} on {date_str} already exists.")
            ab = Absence(
                employee_id=employee_id, date=date_str, leave_type_id=leave_type_id,
                interval=interval, start=start, end=end,
            )
            s.add(ab)
            s.flush()
            return {
                "ID": ab.id, "EMPLOYEEID": employee_id, "DATE": date_str,
                "LEAVETYPID": leave_type_id, "INTERVAL": interval,
                "START": start, "END": end,
            }

    def update_absence(
        self,
        employee_id: int,
        date_str: str,
        leave_type_id: int | None = None,
        interval: int | None = None,
        start: int = 0,
        end: int = 0,
    ) -> dict:
        from .database import SP5Database

        with self._session() as s:
            ab = s.scalars(
                select(Absence).where(Absence.employee_id == employee_id, Absence.date == date_str)
            ).first()
            if ab is None:
                raise ValueError(f"Absence for employee {employee_id} on {date_str} not found.")
            if leave_type_id is not None:
                ab.leave_type_id = leave_type_id
            if interval is not None:
                start, end = SP5Database._validate_absence_interval(interval, start, end)
                ab.interval = interval
                ab.start = start
                ab.end = end
            s.flush()
            return {
                "ID": ab.id, "EMPLOYEEID": ab.employee_id, "DATE": ab.date,
                "LEAVETYPID": ab.leave_type_id, "INTERVAL": ab.interval,
                "START": ab.start, "END": ab.end,
            }

    # ── Users ──────────────────────────────────────────────────

    def _role_from_user(self, u: User) -> str:
        if u.admin:
            return "Admin"
        if u.rights == 1:
            return "Planer"
        return "Leser"

    def get_users(self) -> list[dict]:
        with self._session() as s:
            rows = s.scalars(select(User).where(User.hide == False).order_by(User.position)).all()
            return [{
                "ID": u.id, "POSITION": u.position, "NAME": u.name,
                "DESCRIP": u.descrip or "", "ADMIN": u.admin,
                "RIGHTS": u.rights, "HIDE": u.hide,
                "WDUTIES": u.wduties, "WABSENCES": u.wabsences,
                "WOVERTIMES": u.wovertimes, "BACKUP": u.backup,
                "role": self._role_from_user(u),
            } for u in rows]

    def verify_user_password(self, name: str, password: str) -> dict | None:
        import bcrypt as _bcrypt
        with self._session() as s:
            u = s.scalars(
                select(User).where(User.hide == False, func.lower(User.name) == name.strip().lower())
            ).first()
            if u is None:
                return None
            # Try bcrypt first
            if u.bcrypt_hash:
                try:
                    if _bcrypt.checkpw(password.encode("utf-8"), u.bcrypt_hash.encode("utf-8")):
                        return self._build_user_dict(u)
                except Exception:
                    pass
            # Try MD5 fallback
            if u.digest:
                expected = hashlib.md5(password.encode("utf-8")).digest()
                digest_bytes = u.digest if isinstance(u.digest, bytes) else u.digest.encode("latin-1")
                if digest_bytes == expected:
                    # Auto-migrate to bcrypt
                    try:
                        u.bcrypt_hash = _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")
                        s.flush()
                    except Exception:
                        pass
                    return self._build_user_dict(u)
            return None

    def _build_user_dict(self, u: User) -> dict:
        role = self._role_from_user(u)
        is_admin = role == "Admin"
        return {
            "ID": u.id, "NAME": u.name, "DESCRIP": u.descrip or "",
            "ADMIN": u.admin, "RIGHTS": u.rights, "role": role,
            "WDUTIES": u.wduties if not is_admin else True,
            "WABSENCES": u.wabsences if not is_admin else True,
            "WOVERTIMES": u.wovertimes if not is_admin else True,
            "WNOTES": u.wnotes if not is_admin else True,
            "WDEVIATION": u.wdeviation if not is_admin else True,
            "WCYCLEASS": u.wcycleass if not is_admin else True,
            "WSWAPONLY": u.wswaponly if not is_admin else True,
            "WPAST": u.wpast if not is_admin else True,
            "ADDEMPL": bool(u.addempl) if not is_admin else True,
            "WACCEMWND": u.waccemwnd if not is_admin else True,
            "WACCGRWND": u.waccgrwnd if not is_admin else True,
            "BACKUP": u.backup if not is_admin else True,
            "SHOWABS": bool(u.showabs) if not is_admin else True,
            "SHOWNOTES": u.shownotes if not is_admin else True,
            "SHOWSTATS": u.showstats if not is_admin else True,
            "ACCADMWND": is_admin,
        }

    def get_user_permissions(self, user_id: int) -> dict | None:
        """Granulare 5USER-Flags (Spec 9.6) als {permission: bool}; Admin ⇒ alles True."""
        from .database import SP5Database

        keys = SP5Database._USER_PERMISSION_FIELDS
        with self._session() as s:
            u = s.scalars(select(User).where(User.id == user_id, User.hide == False)).first()
            if u is None:
                return None
            if u.admin:
                return dict.fromkeys(keys, True)
            return {key: bool(getattr(u, key)) for key in keys}

    def create_user(self, data: dict) -> dict:
        import bcrypt as _bcrypt
        with self._session() as s:
            name_lower = (data.get("NAME") or "").strip().lower()
            existing = s.scalars(
                select(User).where(User.hide == False, func.lower(User.name) == name_lower)
            ).first()
            if existing:
                raise ValueError(f"DUPLICATE:USERNAME:{data.get('NAME')}")

            role = data.get("role", "Leser")
            is_admin = role == "Admin"
            rights = 1 if role == "Planer" else 0
            write_perms = role in ("Admin", "Planer")

            password = data.get("PASSWORD", "")
            digest = hashlib.md5(password.encode("utf-8")).digest() if password else b"\x00" * 16
            bcrypt_hash = _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8") if password else None

            max_pos = s.scalar(select(func.max(User.position))) or 0
            u = User(
                position=max_pos + 1, name=data.get("NAME", ""),
                descrip=data.get("DESCRIP", ""), admin=is_admin,
                rights=rights, digest=digest, bcrypt_hash=bcrypt_hash,
                wduties=write_perms, wabsences=write_perms,
                wovertimes=write_perms, wnotes=write_perms,
                wdeviation=write_perms, wcycleass=write_perms,
                wpast=write_perms, backup=is_admin, accadmwnd=is_admin,
            )
            s.add(u)
            s.flush()
            return {"ID": u.id, "NAME": u.name, "DESCRIP": u.descrip, "ADMIN": u.admin, "RIGHTS": u.rights, "HIDE": False, "role": role}

    def update_user(self, user_id: int, data: dict) -> dict:
        import bcrypt as _bcrypt
        with self._session() as s:
            u = s.get(User, user_id)
            if u is None:
                raise ValueError(f"User {user_id} not found")
            if "NAME" in data:
                u.name = data["NAME"]
            if "DESCRIP" in data:
                u.descrip = data["DESCRIP"]
            if "role" in data:
                role = data["role"]
                u.admin = role == "Admin"
                u.rights = 1 if role == "Planer" else 0
                write_perms = role in ("Admin", "Planer")
                u.wduties = write_perms
                u.wabsences = write_perms
                u.wovertimes = write_perms
                u.wnotes = write_perms
                u.wdeviation = write_perms
                u.wcycleass = write_perms
                u.wpast = write_perms
                u.backup = role == "Admin"
                u.accadmwnd = role == "Admin"
            if "PASSWORD" in data and data["PASSWORD"]:
                u.digest = hashlib.md5(data["PASSWORD"].encode("utf-8")).digest()
                u.bcrypt_hash = _bcrypt.hashpw(data["PASSWORD"].encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")
            s.flush()
            return {"ID": user_id, "NAME": u.name, "DESCRIP": u.descrip, "role": self._role_from_user(u)}

    def delete_user(self, user_id: int) -> int:
        with self._session() as s:
            u = s.get(User, user_id)
            if u is None:
                return 0
            u.hide = True
            s.flush()
            return 1

    def change_password(self, user_id: int, new_password_plain: str) -> bool:
        import bcrypt as _bcrypt
        with self._session() as s:
            u = s.get(User, user_id)
            if u is None:
                return False
            u.digest = hashlib.md5(new_password_plain.encode("utf-8")).digest()
            u.bcrypt_hash = _bcrypt.hashpw(new_password_plain.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")
            s.flush()
            return True

    def check_user_permission(self, user_id: int, action: str) -> bool:
        with self._session() as s:
            u = s.get(User, user_id)
            if u is None or u.hide:
                return False
            perm_map = {
                "admin": "admin", "write_duties": "wduties", "write_absences": "wabsences",
                "write_overtimes": "wovertimes", "write_notes": "wnotes",
                "backup": "backup", "read_employees": "waccemwnd", "read_groups": "waccgrwnd",
            }
            attr = perm_map.get(action)
            if attr:
                return bool(getattr(u, attr, False))
            return u.admin

    # ── Notes ──────────────────────────────────────────────────

    def get_notes(self, date: str | None = None, employee_id: int | None = None) -> list[dict]:
        with self._session() as s:
            stmt = select(Note)
            if date:
                stmt = stmt.where(Note.date == date)
            if employee_id is not None:
                stmt = stmt.where(Note.employee_id == employee_id)
            return [{
                "id": n.id, "employee_id": n.employee_id, "date": n.date,
                "text1": n.text1 or "", "text2": n.text2 or "", "category": (n.category or "").strip(),
            } for n in s.scalars(stmt).all()]

    def add_note(self, date: str, text: str, employee_id: int = 0, text2: str = "", category: str = "") -> dict:
        with self._session() as s:
            n = Note(employee_id=employee_id, date=date, text1=text, text2=text2, category=category)
            s.add(n)
            s.flush()
            return {"id": n.id, "employee_id": employee_id, "date": date, "text1": text, "text2": text2, "category": category}

    def delete_note(self, note_id: int) -> int:
        with self._session() as s:
            n = s.get(Note, note_id)
            if n is None:
                return 0
            s.delete(n)
            s.flush()
            return 1

    # ── Statistics (Berechnungsschicht, wie SP5Database) ──────

    def get_statistics(
        self,
        year: int | None = None,
        month: int | None = None,
        group_id: int | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict]:
        """Per-Mitarbeiter-Statistik für einen Monat oder freien Zeitraum.

        Identische calc-Aufrufe wie SP5Database.get_statistics (Spec 3.3/3.4:
        CALCBASE-Dispatcher, tagindex-korrekte Schichtstunden, 5SPSHI-
        Ersetzung, expandierte Zyklusdienste, Abwesenheits-Anrechnung 3.5,
        Konten 3.6). Zeitraum: ``year``+``month`` oder ``date_from``/
        ``date_to`` (hat Vorrang, Spec 3.9.1).
        """
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

        employees = self.get_employees(include_hidden=False)
        if group_id is not None:
            member_ids = set(self.get_group_members(group_id))
            employees = [e for e in employees if e["ID"] in member_ids]

        # Mitarbeiter → primäre Gruppe (wie SP5Database)
        groups_all = self.get_groups()
        emp_group: dict[int, str] = {}
        emp_group_id: dict[int, int] = {}
        all_gm = self.get_all_group_members()
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
                if lt.get("ENTITLED"):
                    vac += 1
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

    # ── Personaltabelle (Spec 3.9.2/3.9.3, wie SP5Database) ───
    def get_personnel_table(
        self, date_from: str, date_to: str, group_id: int | None = None
    ) -> dict:
        """Personaltabelle über einen freien Auswertungszeitraum (Spec 3.9).

        Identische calc-Aufrufe wie SP5Database.get_personnel_table:
        Standard-Spalten über calc.personnel_table_row, Einteilungen je
        Schichtart (3.9.3 Nr. 4), Fehltage je Abwesenheitsart (Nr. 5) und
        bei Ein-Jahres-Zeitraum der Doppelwert genommen/verbleibend (Nr. 6).
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

        one_year = von == date(von.year, 1, 1) and bis == date(von.year, 12, 31)
        leaen_by_emp: dict[int, list[dict]] = {}
        if one_year:
            with self._session() as s:
                for r in s.scalars(select(LeaveEntitlement)).all():
                    leaen_by_emp.setdefault(r.employee_id, []).append(r.to_dict())

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

    # ── Personalauslastung (Spec 3.9.4, wie SP5Database) ──────
    def get_utilization(
        self, year: int, month: int, group_id: int | None = None
    ) -> list[dict]:
        """Personalauslastung gegen den Bedarf (Spec 3.9.4).

        Identische calc-Aufrufe wie SP5Database.get_utilization: Wochenbedarf
        (staffing_requirements) je Tagindex 0..7 über calc.demand_for_day,
        Tagesbedarf (special_demands) überschreibt seine Zelle, Zellstatus
        über calc.utilization_status, eingeteilt je MA höchstens einmal
        (calc.count_assigned).
        """
        von = date(year, month, 1)
        bis = self._last_of_month(year, month)
        holidays = self._calc_holidays()

        members_by_group = self.get_all_group_members()
        group_ids = (
            [group_id] if group_id is not None else sorted(members_by_group.keys())
        )

        with self._session() as s:
            shdem_rows = [r.to_dict() for r in s.scalars(select(ShiftDemand)).all()]
            spdem_rows = [r.to_dict() for r in s.scalars(select(SpecialDemand)).all()]

        shdem_by_group: dict[int, list[dict]] = {}
        shifts_by_group: dict[int, set[int]] = {}
        for r in shdem_rows:
            gid = r.get("GROUPID") or 0
            shdem_by_group.setdefault(gid, []).append(r)
            sid = int(r.get("SHIFTID") or 0)
            if sid:
                shifts_by_group.setdefault(gid, set()).add(sid)

        spdem_by_cell: dict[tuple[int, str, int], dict] = {}
        for r in spdem_rows:
            gid = r.get("GROUPID") or 0
            sid = int(r.get("SHIFTID") or 0)
            d = r.get("DATE") or ""
            if sid and d:
                spdem_by_cell[(gid, d, sid)] = r

        manual = self._movement_by_employee(ScheduleEntry, von, bis)
        special = self._movement_by_employee(SpecialShift, von, bis)
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

    # ── Resturlaubs-Verfall (Spec 3.7.3, wie SP5Database) ─────
    def forfeit_rest(
        self,
        cutoff_date: str,
        group_id: int | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Resturlaub zum Stichtag verfallen lassen (Spec 3.7.3 / Dialog 5.17).

        Identische calc-Aufrufe wie SP5Database.forfeit_rest
        (calculations.forfeit_rest: REST nur kürzen, nie erhöhen; ENTITLEMNT
        bleibt unberührt). ``dry_run=True`` liefert die Kürzungen als
        Vorschau; sonst wird leave_entitlements.carry_forward aktualisiert.
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
        with self._session() as s:
            leaen_by_emp: dict[int, list[dict]] = {}
            for r in s.scalars(select(LeaveEntitlement)).all():
                leaen_by_emp.setdefault(r.employee_id, []).append(r.to_dict())
            absen_by_emp: dict[int, list[dict]] = {}
            for r in s.scalars(select(Absence)).all():
                absen_by_emp.setdefault(r.employee_id, []).append(r.to_dict())

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
                    with self._session() as s:
                        matches = s.scalars(
                            select(LeaveEntitlement).where(
                                LeaveEntitlement.employee_id == eid,
                                LeaveEntitlement.year == year,
                                LeaveEntitlement.leave_type_id == lt_id,
                            )
                        ).all()
                        for rec in matches:
                            rec.carry_forward = new_rest
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

        return {
            "cutoff_date": cutoff.isoformat(),
            "year": year,
            "group_id": group_id,
            "dry_run": dry_run,
            "employees_processed": len(employees),
            "cuts": cuts,
            "total_forfeited": round(total_forfeited, 4),
        }

    # ── Zuschlagsarten (5XCHAR-Spiegel, nur lesend) ───────────
    def get_extracharges(self, include_hidden: bool = False) -> list[dict]:
        """Zuschlagsarten in DBF-Schlüsselform (für calculate_extracharge_hours)."""
        with self._session() as s:
            stmt = select(ExtraCharge)
            if not include_hidden:
                stmt = stmt.where(ExtraCharge.hide == False)
            result = [
                {
                    "ID": r.id,
                    "NAME": r.name or "",
                    "POSITION": r.position or 0,
                    "START": r.start or 0,
                    "END": r.end or 0,
                    "VALIDITY": r.validity or 0,
                    "VALIDDAYS": r.validdays or "",
                    "HOLRULE": r.holrule or 0,
                    "HIDE": 1 if r.hide else 0,
                }
                for r in s.scalars(stmt).all()
            ]
        result.sort(key=lambda x: x.get("POSITION", 0))
        return result

    # ── Urlaubskonto (Spec 3.7.1, wie SP5Database) ────────────
    def get_leave_balance(self, employee_id: int, year: int) -> dict:
        """Urlaubskonto: Anspruch + Übertrag − verbraucht = verbleibend.

        Identische calc-Aufrufe wie SP5Database.get_leave_balance: je
        anspruchsverbundener Art (ENTITLED=1) über calculations.leave_account
        (Spec 3.7.1, Verbrauch nach 3.5.2/3.5.3 inkl. INTERVAL-Halbtagen) und
        über die Arten summiert. Ohne 5LEAEN-Satz kein Default-Anspruch.
        """
        emp = self.get_employee(employee_id)
        ctx = (
            self._calc_context(emp)
            if emp
            else calc.EmployeeContext(workdays=(False,) * 8)
        )
        holidays = self._calc_holidays()
        with self._session() as s:
            leaen_rows = [
                r.to_dict()
                for r in s.scalars(
                    select(LeaveEntitlement).where(
                        LeaveEntitlement.employee_id == employee_id
                    )
                ).all()
            ]
            absences = [
                r.to_dict()
                for r in s.scalars(
                    select(Absence).where(Absence.employee_id == employee_id)
                ).all()
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

    # ── Jahresabschluss: im PG-Backend nicht implementiert ────
    def get_annual_close_preview(
        self,
        year: int,
        group_id: int | None = None,
        carry_forward_days: float = 10,
        keep_entitlements: bool = False,
    ) -> dict:
        raise NotImplementedError(
            "Jahresabschluss ist im PostgreSQL-Backend nicht implementiert: "
            "die 5LEAEN-Fortschreibung je Art (Spec 3.7.2) braucht eine "
            "Entitlement-Schreibfassade (set_leave_entitlement), die der "
            "ORM-Spiegel noch nicht hat."
        )

    def run_annual_close(
        self,
        year: int,
        group_id: int | None = None,
        carry_forward_days: float = 10,
        keep_entitlements: bool = False,
    ) -> dict:
        raise NotImplementedError(
            "Jahresabschluss ist im PostgreSQL-Backend nicht implementiert: "
            "die 5LEAEN-Fortschreibung je Art (Spec 3.7.2) braucht eine "
            "Entitlement-Schreibfassade (set_leave_entitlement), die der "
            "ORM-Spiegel noch nicht hat."
        )

    # ── Stats ──────────────────────────────────────────────────

    def get_stats(self) -> dict:
        with self._session() as s:
            return {
                "employees": s.scalar(select(func.count(Employee.id)).where(Employee.hide == False)) or 0,
                "groups": s.scalar(select(func.count(Group.id)).where(Group.hide == False)) or 0,
                "shifts": s.scalar(select(func.count(Shift.id)).where(Shift.hide == False)) or 0,
                "leave_types": s.scalar(select(func.count(LeaveType.id)).where(LeaveType.hide == False)) or 0,
                "workplaces": s.scalar(select(func.count(Workplace.id)).where(Workplace.hide == False)) or 0,
                "holidays": s.scalar(select(func.count(Holiday.id))) or 0,
                "users": s.scalar(select(func.count(User.id)).where(User.hide == False)) or 0,
            }

    # ── Changelog ─────────────────────────────────────────────

    def log_action(self, user: str, action: str, entity: str, entity_id: int,
                   details: str = "", old_value=None, new_value=None, user_id: int | None = None) -> dict:
        from datetime import datetime as _dt
        entry = {
            "timestamp": _dt.now().isoformat(timespec="seconds"),
            "user": user, "action": action, "entity": entity,
            "entity_id": entity_id, "details": details,
        }
        if user_id is not None:
            entry["user_id"] = user_id
        with self._session() as s:
            ce = ChangelogEntry(
                timestamp=entry["timestamp"], user=user, user_id=user_id,
                action=action, entity=entity, entity_id=entity_id,
                details=details,
                old_value=json.dumps(old_value) if old_value else None,
                new_value=json.dumps(new_value) if new_value else None,
            )
            s.add(ce)
            # Keep max 5000 entries
            total = s.scalar(select(func.count(ChangelogEntry.id)))
            if total and total > 5000:
                oldest = s.scalars(select(ChangelogEntry).order_by(ChangelogEntry.id).limit(total - 5000)).all()
                for old in oldest:
                    s.delete(old)
            s.flush()
        return entry

    def get_changelog(self, limit: int = 100, user: str | None = None,
                      entity_type: str | None = None, date_from: str | None = None,
                      date_to: str | None = None) -> list[dict]:
        with self._session() as s:
            stmt = select(ChangelogEntry).order_by(ChangelogEntry.timestamp.desc())
            if user:
                stmt = stmt.where(func.lower(ChangelogEntry.user) == user.lower())
            if entity_type:
                stmt = stmt.where(func.lower(ChangelogEntry.entity) == entity_type.lower())
            if date_from:
                stmt = stmt.where(ChangelogEntry.timestamp >= date_from)
            if date_to:
                stmt = stmt.where(ChangelogEntry.timestamp <= date_to + "T23:59:59")
            stmt = stmt.limit(limit)
            return [{
                "timestamp": ce.timestamp, "user": ce.user, "action": ce.action,
                "entity": ce.entity, "entity_id": ce.entity_id, "details": ce.details or "",
            } for ce in s.scalars(stmt).all()]

    # ── Tagesplan (vereinfacht für API-Kompatibilität) ─────────

    def get_schedule_day(self, date_str: str, group_id: int | None = None) -> list[dict]:
        """Liefert die Einträge eines Tages. Vereinfachte Implementierung."""
        employees = self.get_employees(include_hidden=False)
        if group_id is not None:
            member_ids = set(self.get_group_members(group_id))
            employees = [e for e in employees if e["ID"] in member_ids]

        shifts_map = {sh["ID"]: sh for sh in self.get_shifts(include_hidden=True)}
        lt_map = {lt["ID"]: lt for lt in self.get_leave_types(include_hidden=True)}

        day_entries: dict[int, dict] = {}

        with self._session() as s:
            for r in s.scalars(select(ScheduleEntry).where(ScheduleEntry.date == date_str)).all():
                day_entries[r.employee_id] = {"kind": "shift", "shift_id": r.shift_id, "workplace_id": r.workplace_id, "leave_type_id": None}
            for r in s.scalars(select(SpecialShift).where(SpecialShift.date == date_str)).all():
                day_entries[r.employee_id] = {
                    "kind": "special_shift", "shift_id": r.shift_id, "workplace_id": r.workplace_id,
                    "leave_type_id": None, "custom_name": r.name, "custom_short": r.shortname,
                    "color_bk": bgr_to_hex(r.colorbk), "color_text": bgr_to_hex(r.colortext),
                    "spshi_id": r.id, "spshi_type": r.entry_type, "spshi_startend": r.startend or "",
                    "spshi_duration": r.duration,
                }
            for r in s.scalars(select(Absence).where(Absence.date == date_str)).all():
                day_entries[r.employee_id] = {"kind": "absence", "shift_id": None, "workplace_id": None, "leave_type_id": r.leave_type_id}

        result = []
        for emp in employees:
            eid = emp["ID"]
            entry = day_entries.get(eid, {})
            kind = entry.get("kind")
            shift_id = entry.get("shift_id")
            leave_type_id = entry.get("leave_type_id")

            shift_name = shift_short = leave_name = display_name = ""
            color_bk = "#FFFFFF"
            color_text = "#000000"

            if shift_id and shift_id in shifts_map:
                sh = shifts_map[shift_id]
                shift_name = sh.get("NAME", "")
                shift_short = sh.get("SHORTNAME", "")
                color_bk = entry.get("color_bk") or bgr_to_hex(sh.get("COLORBK", 16777215))
                color_text = entry.get("color_text") or bgr_to_hex(sh.get("COLORTEXT", 0))
                display_name = shift_short or shift_name
            elif leave_type_id and leave_type_id in lt_map:
                lt = lt_map[leave_type_id]
                leave_name = lt.get("NAME", "")
                color_bk = bgr_to_hex(lt.get("COLORBK", 16777215))
                color_text = bgr_to_hex(lt.get("COLORBAR", 0))
                display_name = lt.get("SHORTNAME", lt.get("NAME", ""))

            result.append({
                "employee_id": eid,
                "employee_name": f"{emp.get('NAME', '')}, {emp.get('FIRSTNAME', '')}".strip(", "),
                "employee_short": emp.get("SHORTNAME", ""),
                "shift_id": shift_id, "shift_name": shift_name, "shift_short": shift_short,
                "color_bk": color_bk, "color_text": color_text,
                "workplace_id": entry.get("workplace_id"), "workplace_name": "",
                "kind": kind, "leave_name": leave_name, "display_name": display_name,
                "spshi_id": entry.get("spshi_id"), "spshi_type": entry.get("spshi_type"),
                "spshi_startend": entry.get("spshi_startend", ""),
                "spshi_duration": entry.get("spshi_duration", 0.0),
            })
        return result

    # ── TOTP 2FA ──────────────────────────────────────────────

    def totp_get_status(self, user_id: int) -> bool:
        with self._session() as s:
            u = s.get(User, user_id)
            return bool(u and u.totp_enabled)

    def totp_generate_secret(self, user_id: int) -> str:
        import pyotp
        secret = pyotp.random_base32()
        with self._session() as s:
            u = s.get(User, user_id)
            if u:
                u.totp_secret = secret
                u.totp_enabled = False
                s.flush()
        return secret

    def totp_enable(self, user_id: int, code: str) -> list[str] | None:
        import secrets

        import pyotp
        with self._session() as s:
            u = s.get(User, user_id)
            if not u or not u.totp_secret:
                return None
            totp = pyotp.TOTP(u.totp_secret)
            if not totp.verify(code, valid_window=1):
                return None
            backup_codes = [secrets.token_hex(4).upper() for _ in range(8)]
            backup_hashes = [hashlib.sha256(c.encode()).hexdigest() for c in backup_codes]
            u.totp_enabled = True
            u.totp_backup_codes = json.dumps(backup_hashes)
            s.flush()
            return backup_codes

    def totp_verify(self, user_id: int, code: str) -> bool:
        import pyotp
        with self._session() as s:
            u = s.get(User, user_id)
            if not u or not u.totp_enabled or not u.totp_secret:
                return False
            totp = pyotp.TOTP(u.totp_secret)
            if totp.verify(code, valid_window=1):
                return True
            code_hash = hashlib.sha256(code.strip().upper().encode()).hexdigest()
            backup = json.loads(u.totp_backup_codes or "[]")
            if code_hash in backup:
                backup.remove(code_hash)
                u.totp_backup_codes = json.dumps(backup)
                s.flush()
                return True
            return False

    def totp_disable(self, user_id: int) -> bool:
        with self._session() as s:
            u = s.get(User, user_id)
            if not u:
                return False
            u.totp_secret = None
            u.totp_enabled = False
            u.totp_backup_codes = None
            s.flush()
            return True
