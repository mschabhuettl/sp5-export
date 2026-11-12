"""
DBF → SQLAlchemy sync utility.

Reads data from DBF files (the legacy data source) and upserts into
the SQLAlchemy-managed database. This enables a gradual migration:
DBF remains the source of truth while the ORM layer is built out.

Usage:
    from sp5lib.orm import get_engine, init_db
    from sp5lib.orm.sync import sync_employees, sync_groups, sync_all

    engine = get_engine("sqlite:///sp5.db")
    init_db(engine)
    stats = sync_all(engine, "/path/to/Daten")
"""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .base import get_session
from .models import (
    Absence,
    AccountBooking,
    Cycle,
    CycleAssignment,
    Employee,
    Group,
    GroupAssignment,
    Holiday,
    LeaveEntitlement,
    LeaveType,
    OvertimeEntry,
    Period,
    Restriction,
    Shift,
    ShiftAssignment,
    ShiftDemand,
    SpecialDemand,
    SpecialShift,
    Workplace,
)

_log = logging.getLogger("sp5api.orm.sync")


def _read_dbf(daten_path: str, table_name: str) -> list[dict[str, Any]]:
    """Read a DBF table, returning list of dicts."""
    import os

    from sp5lib.dbf_reader import read_dbf

    path = os.path.join(daten_path, f"5{table_name}.DBF")
    try:
        return read_dbf(path)
    except Exception as exc:
        _log.warning("Could not read %s: %s", path, exc)
        return []


def _valid_date(value: Any) -> str | None:
    """Return a normalised ISO date string, or None for empty/invalid input.

    read_dbf already parses DBF 'D' fields to 'YYYY-MM-DD' (or None for invalid
    calendar dates). This guards the schedule sync against blank/garbage dates.
    """
    if not value:
        return None
    s = str(value).strip()
    # Expect ISO 'YYYY-MM-DD'; reject anything that is not a plausible date.
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        return None
    return s


def sync_employees(session: Session, daten_path: str) -> int:
    """Sync employees from 5EMPL.DBF into the ORM employees table.

    Uses upsert semantics: existing records (by ID) are updated,
    new records are inserted. Returns the number of synced rows.
    """
    rows = _read_dbf(daten_path, "EMPL")
    count = 0

    for r in rows:
        emp_id = r.get("ID")
        if not emp_id:
            continue

        emp = session.get(Employee, emp_id)
        if emp is None:
            emp = Employee(id=emp_id)
            session.add(emp)

        emp.position = r.get("POSITION", 0) or 0
        emp.number = str(r.get("NUMBER") or "").strip()
        emp.name = str(r.get("NAME") or "").strip()
        emp.firstname = str(r.get("FIRSTNAME") or "").strip()
        emp.shortname = str(r.get("SHORTNAME") or "").strip()
        emp.sex = r.get("SEX", 0)
        emp.hrsday = float(r.get("HRSDAY", 0) or 0)
        emp.hrsweek = float(r.get("HRSWEEK", 0) or 0)
        emp.hrsmonth = float(r.get("HRSMONTH", 0) or 0)
        emp.workdays = str(r.get("WORKDAYS") or "").strip()
        emp.salutation = str(r.get("SALUTATION") or "").strip()
        emp.street = str(r.get("STREET") or "").strip()
        emp.zip = str(r.get("ZIP") or "").strip()
        emp.town = str(r.get("TOWN") or "").strip()
        emp.phone = str(r.get("PHONE") or "").strip()
        emp.email = str(r.get("EMAIL") or "").strip()
        emp.function = str(r.get("FUNCTION") or "").strip()
        emp.birthday = str(r.get("BIRTHDAY") or "").strip() or None
        emp.empstart = str(r.get("EMPSTART") or "").strip() or None
        emp.empend = str(r.get("EMPEND") or "").strip() or None
        emp.hide = bool(r.get("HIDE"))
        emp.note1 = str(r.get("NOTE1") or "").strip()
        emp.note2 = str(r.get("NOTE2") or "").strip()
        emp.note3 = str(r.get("NOTE3") or "").strip()
        emp.note4 = str(r.get("NOTE4") or "").strip()
        count += 1

    session.flush()
    return count


def sync_groups(session: Session, daten_path: str) -> int:
    """Sync groups from 5GROUP.DBF into the ORM groups table.

    ``super_id`` (the self-referential parent FK) is resolved in a second pass
    against the full set of known group IDs. A reference to a group that does
    not exist (dangling reference in dirty legacy data) is set to NULL and
    logged, instead of raising ``FOREIGN KEY constraint failed`` and aborting
    the whole sync. The two-pass approach also makes ordering irrelevant
    (parents may appear after their children in the DBF).
    """
    rows = _read_dbf(daten_path, "GROUP")
    count = 0

    # Pass 1: upsert scalar columns; defer super_id until all groups exist.
    for r in rows:
        group_id = r.get("ID")
        if not group_id:
            continue

        group = session.get(Group, group_id)
        if group is None:
            group = Group(id=group_id)
            session.add(group)

        group.name = str(r.get("NAME") or "").strip()
        group.shortname = str(r.get("SHORTNAME") or "").strip()
        group.super_id = None
        group.position = r.get("POSITION", 0) or 0
        group.hide = bool(r.get("HIDE"))
        count += 1

    session.flush()

    # Pass 2: resolve super_id against the now-complete set of group IDs.
    valid_ids = set(session.scalars(select(Group.id)).all())
    for r in rows:
        group_id = r.get("ID")
        if not group_id:
            continue
        super_id = r.get("SUPERID") or None
        if super_id and super_id not in valid_ids:
            _log.warning(
                "group %s references missing super_id %s — setting NULL",
                group_id,
                super_id,
            )
            super_id = None
        if super_id:
            group = session.get(Group, group_id)
            group.super_id = super_id

    session.flush()
    return count


def sync_group_assignments(session: Session, daten_path: str) -> int:
    """Sync group assignments from 5GRASG.DBF (full delete + re-insert).

    The ``ID`` column in 5GRASG.DBF is **not** a globally unique key — it is a
    per-group running index, so the same value repeats across different
    employee/group pairs. We therefore do NOT use it as the primary key (the
    autoincrement ``GroupAssignment.id`` does that); the logical identity is the
    ``UNIQUE(employee_id, group_id)`` pair. Duplicate pairs are de-duplicated,
    and rows whose employee or group does not exist are skipped (those columns
    are real FKs, so a dangling reference would otherwise abort the sync).
    """
    rows = _read_dbf(daten_path, "GRASG")

    # Clear existing assignments and re-insert (simple full-sync approach).
    session.query(GroupAssignment).delete()
    session.flush()

    valid_emp_ids = set(session.scalars(select(Employee.id)).all())
    valid_group_ids = set(session.scalars(select(Group.id)).all())

    count = 0
    seen: set[tuple[int, int]] = set()
    skipped_dangling = 0
    for r in rows:
        emp_id = r.get("EMPLOYEEID")
        group_id = r.get("GROUPID")
        if not emp_id or not group_id:
            continue
        pair = (emp_id, group_id)
        if pair in seen:
            continue  # duplicate (employee, group) — keep a single assignment
        if emp_id not in valid_emp_ids or group_id not in valid_group_ids:
            skipped_dangling += 1
            continue

        seen.add(pair)
        # Let the autoincrement PK assign the id; the DBF ID is not unique.
        session.add(GroupAssignment(employee_id=emp_id, group_id=group_id))
        count += 1

    if skipped_dangling:
        _log.warning(
            "sync_group_assignments: skipped %d row(s) with dangling employee/group reference",
            skipped_dangling,
        )
    session.flush()
    return count


def sync_shifts(session: Session, daten_path: str) -> int:
    """Sync shift definitions from 5SHIFT.DBF into the ORM shifts table."""
    rows = _read_dbf(daten_path, "SHIFT")
    count = 0

    for r in rows:
        shift_id = r.get("ID")
        if not shift_id:
            continue

        shift = session.get(Shift, shift_id)
        if shift is None:
            shift = Shift(id=shift_id)
            session.add(shift)

        shift.name = str(r.get("NAME") or "").strip()
        shift.shortname = str(r.get("SHORTNAME") or "").strip()
        shift.position = r.get("POSITION", 0) or 0
        shift.hide = bool(r.get("HIDE"))
        shift.colortext = r.get("COLORTEXT", 0) or 0
        shift.colorbar = r.get("COLORBAR", 0) or 0
        shift.colorbk = r.get("COLORBK", 16777215) or 16777215
        for i in range(8):
            setattr(shift, f"duration{i}", float(r.get(f"DURATION{i}", 0) or 0))
            setattr(shift, f"startend{i}", str(r.get(f"STARTEND{i}") or "").strip())
        count += 1

    session.flush()
    return count


def sync_leave_types(session: Session, daten_path: str) -> int:
    """Sync leave/absence types from 5LEAVT.DBF into the ORM leave_types table."""
    rows = _read_dbf(daten_path, "LEAVT")
    count = 0

    for r in rows:
        lt_id = r.get("ID")
        if not lt_id:
            continue

        lt = session.get(LeaveType, lt_id)
        if lt is None:
            lt = LeaveType(id=lt_id)
            session.add(lt)

        lt.name = str(r.get("NAME") or "").strip()
        lt.shortname = str(r.get("SHORTNAME") or "").strip()
        lt.position = r.get("POSITION", 0) or 0
        lt.hide = bool(r.get("HIDE"))
        lt.entitled = bool(r.get("ENTITLED"))
        lt.stdentit = float(r.get("STDENTIT", 0) or 0)
        lt.chargetyp = r.get("CHARGETYP", 0) or 0
        lt.colortext = r.get("COLORTEXT", 0) or 0
        lt.colorbar = r.get("COLORBAR", 0) or 0
        lt.colorbk = r.get("COLORBK", 16777215) or 16777215
        count += 1

    session.flush()
    return count


def sync_workplaces(session: Session, daten_path: str) -> int:
    """Sync workplace definitions from 5WOPL.DBF into the ORM workplaces table."""
    rows = _read_dbf(daten_path, "WOPL")
    count = 0

    for r in rows:
        wp_id = r.get("ID")
        if not wp_id:
            continue

        wp = session.get(Workplace, wp_id)
        if wp is None:
            wp = Workplace(id=wp_id)
            session.add(wp)

        wp.name = str(r.get("NAME") or "").strip()
        wp.shortname = str(r.get("SHORTNAME") or "").strip()
        wp.position = r.get("POSITION", 0) or 0
        wp.hide = bool(r.get("HIDE"))
        wp.colortext = r.get("COLORTEXT", 0) or 0
        wp.colorbar = r.get("COLORBAR", 0) or 0
        wp.colorbk = r.get("COLORBK", 16777215) or 16777215
        count += 1

    session.flush()
    return count


def sync_shift_assignments(session: Session, daten_path: str) -> int:
    """Sync regular schedule entries from 5MASHI.DBF.

    Rows with a blank/invalid DATE are skipped and logged. employee_id /
    shift_id / workplace_id are stored as plain integers (no FK constraint),
    so dangling references in dirty data do not break the sync.
    """
    rows = _read_dbf(daten_path, "MASHI")
    count = 0
    skipped = 0

    for r in rows:
        entry_id = r.get("ID")
        if not entry_id:
            continue
        date = _valid_date(r.get("DATE"))
        if date is None:
            skipped += 1
            continue

        entry = session.get(ShiftAssignment, entry_id)
        if entry is None:
            entry = ShiftAssignment(id=entry_id)
            session.add(entry)

        entry.date = date
        entry.employee_id = r.get("EMPLOYEEID", 0) or 0
        entry.shift_id = r.get("SHIFTID", 0) or 0
        entry.workplace_id = r.get("WORKPLACID", 0) or 0
        entry.entry_type = r.get("TYPE", 0) or 0
        count += 1

    if skipped:
        _log.warning("sync_shift_assignments: skipped %d row(s) with invalid date", skipped)
    session.flush()
    return count


def sync_special_shifts(session: Session, daten_path: str) -> int:
    """Sync special / one-off shifts from 5SPSHI.DBF (invalid dates skipped)."""
    rows = _read_dbf(daten_path, "SPSHI")
    count = 0
    skipped = 0

    for r in rows:
        entry_id = r.get("ID")
        if not entry_id:
            continue
        date = _valid_date(r.get("DATE"))
        if date is None:
            skipped += 1
            continue

        sp = session.get(SpecialShift, entry_id)
        if sp is None:
            sp = SpecialShift(id=entry_id)
            session.add(sp)

        sp.date = date
        sp.employee_id = r.get("EMPLOYEEID", 0) or 0
        sp.name = str(r.get("NAME") or "").strip()
        sp.shortname = str(r.get("SHORTNAME") or "").strip()
        sp.shift_id = r.get("SHIFTID", 0) or 0
        sp.workplace_id = r.get("WORKPLACID", 0) or 0
        sp.entry_type = r.get("TYPE", 0) or 0
        sp.colortext = r.get("COLORTEXT", 0) or 0
        sp.colorbar = r.get("COLORBAR", 0) or 0
        sp.colorbk = r.get("COLORBK", 16777215) or 16777215
        sp.bold = r.get("BOLD", 0) or 0
        sp.startend = str(r.get("STARTEND") or "").strip()
        sp.duration = float(r.get("DURATION", 0) or 0)
        sp.noextra = r.get("NOEXTRA", 0) or 0
        count += 1

    if skipped:
        _log.warning("sync_special_shifts: skipped %d row(s) with invalid date", skipped)
    session.flush()
    return count


def sync_absences(session: Session, daten_path: str) -> int:
    """Sync absences from 5ABSEN.DBF (invalid dates skipped)."""
    rows = _read_dbf(daten_path, "ABSEN")
    count = 0
    skipped = 0

    for r in rows:
        entry_id = r.get("ID")
        if not entry_id:
            continue
        date = _valid_date(r.get("DATE"))
        if date is None:
            skipped += 1
            continue

        ab = session.get(Absence, entry_id)
        if ab is None:
            ab = Absence(id=entry_id)
            session.add(ab)

        ab.date = date
        ab.employee_id = r.get("EMPLOYEEID", 0) or 0
        # DBF uses LEAVETYPID; tolerate the LEAVETYPEID spelling too.
        ab.leave_type_id = r.get("LEAVETYPID") or r.get("LEAVETYPEID") or 0
        ab.entry_type = r.get("TYPE", 0) or 0
        ab.interval = r.get("INTERVAL", 0) or 0
        ab.start = r.get("START", 0) or 0
        ab.end = r.get("END", 0) or 0
        count += 1

    if skipped:
        _log.warning("sync_absences: skipped %d row(s) with invalid date", skipped)
    session.flush()
    return count


def sync_holidays(session: Session, daten_path: str) -> int:
    """Sync public holidays from 5HOLID.DBF (invalid dates skipped)."""
    rows = _read_dbf(daten_path, "HOLID")
    count = 0
    skipped = 0

    for r in rows:
        hol_id = r.get("ID")
        if not hol_id:
            continue
        date = _valid_date(r.get("DATE"))
        if date is None:
            skipped += 1
            continue

        hol = session.get(Holiday, hol_id)
        if hol is None:
            hol = Holiday(id=hol_id)
            session.add(hol)

        hol.date = date
        hol.name = str(r.get("NAME") or "").strip()
        hol.interval = r.get("INTERVAL", 0) or 0
        count += 1

    if skipped:
        _log.warning("sync_holidays: skipped %d row(s) with invalid date", skipped)
    session.flush()
    return count


def sync_periods(session: Session, daten_path: str) -> int:
    """Sync accounting/planning periods from 5PERIO.DBF.

    The DBF stores the label in DESCRIPT and the owning group in GROUPID
    (a plain integer, no FK). START/END are date strings as parsed by read_dbf.
    """
    rows = _read_dbf(daten_path, "PERIO")
    count = 0

    for r in rows:
        per_id = r.get("ID")
        if not per_id:
            continue

        per = session.get(Period, per_id)
        if per is None:
            per = Period(id=per_id)
            session.add(per)

        per.group_id = r.get("GROUPID", 0) or 0
        per.start = str(r.get("START") or "").strip()
        per.end = str(r.get("END") or "").strip()
        per.color = r.get("COLOR", 16777215) or 16777215
        per.description = str(r.get("DESCRIPT") or "").strip()
        count += 1

    session.flush()
    return count


def sync_book(session: Session, daten_path: str) -> int:
    """Sync manual account/time bookings from 5BOOK.DBF (invalid dates skipped)."""
    rows = _read_dbf(daten_path, "BOOK")
    count = 0
    skipped = 0

    for r in rows:
        book_id = r.get("ID")
        if not book_id:
            continue
        date = _valid_date(r.get("DATE"))
        if date is None:
            skipped += 1
            continue

        bk = session.get(AccountBooking, book_id)
        if bk is None:
            bk = AccountBooking(id=book_id)
            session.add(bk)

        bk.employee_id = r.get("EMPLOYEEID", 0) or 0
        bk.date = date
        bk.booking_type = r.get("TYPE", 0) or 0
        bk.value = float(r.get("VALUE", 0) or 0)
        bk.note = str(r.get("NOTE") or "").strip()
        count += 1

    if skipped:
        _log.warning("sync_book: skipped %d row(s) with invalid date", skipped)
    session.flush()
    return count


def sync_overtime(session: Session, daten_path: str) -> int:
    """Sync manual overtime adjustments from 5OVER.DBF (invalid dates skipped)."""
    rows = _read_dbf(daten_path, "OVER")
    count = 0
    skipped = 0

    for r in rows:
        ot_id = r.get("ID")
        if not ot_id:
            continue
        date = _valid_date(r.get("DATE"))
        if date is None:
            skipped += 1
            continue

        ot = session.get(OvertimeEntry, ot_id)
        if ot is None:
            ot = OvertimeEntry(id=ot_id)
            session.add(ot)

        ot.employee_id = r.get("EMPLOYEEID", 0) or 0
        ot.date = date
        ot.hours = float(r.get("HOURS", 0) or 0)
        count += 1

    if skipped:
        _log.warning("sync_overtime: skipped %d row(s) with invalid date", skipped)
    session.flush()
    return count


def sync_leave_entitlements(session: Session, daten_path: str) -> int:
    """Sync annual leave entitlements from 5LEAEN.DBF.

    Keyed by year (not a date); rows without an ID are skipped. DBF fields:
    ENTITLEMNT -> entitlement, REST -> carry_forward, INDAYS -> in_days.
    """
    rows = _read_dbf(daten_path, "LEAEN")
    count = 0

    for r in rows:
        le_id = r.get("ID")
        if not le_id:
            continue

        le = session.get(LeaveEntitlement, le_id)
        if le is None:
            le = LeaveEntitlement(id=le_id)
            session.add(le)

        le.employee_id = r.get("EMPLOYEEID", 0) or 0
        le.year = r.get("YEAR", 0) or 0
        le.leave_type_id = r.get("LEAVETYPID", 0) or 0
        le.entitlement = float(r.get("ENTITLEMNT", 0) or 0)
        le.carry_forward = float(r.get("REST", 0) or 0)
        le.in_days = bool(r.get("INDAYS", 1))
        count += 1

    session.flush()
    return count


def sync_shift_demand(session: Session, daten_path: str) -> int:
    """Sync recurring shift demand from 5SHDEM.DBF (keyed by weekday, no date)."""
    rows = _read_dbf(daten_path, "SHDEM")
    count = 0

    for r in rows:
        dem_id = r.get("ID")
        if not dem_id:
            continue

        dem = session.get(ShiftDemand, dem_id)
        if dem is None:
            dem = ShiftDemand(id=dem_id)
            session.add(dem)

        dem.group_id = r.get("GROUPID", 0) or 0
        dem.weekday = r.get("WEEKDAY", 0) or 0
        dem.shift_id = r.get("SHIFTID", 0) or 0
        dem.workplace_id = r.get("WORKPLACID", 0) or 0
        dem.min_staff = r.get("MIN", 0) or 0
        dem.max_staff = r.get("MAX", 0) or 0
        count += 1

    session.flush()
    return count


def sync_special_demand(session: Session, daten_path: str) -> int:
    """Sync date-specific shift demand from 5SPDEM.DBF (invalid dates skipped)."""
    rows = _read_dbf(daten_path, "SPDEM")
    count = 0
    skipped = 0

    for r in rows:
        dem_id = r.get("ID")
        if not dem_id:
            continue
        date = _valid_date(r.get("DATE"))
        if date is None:
            skipped += 1
            continue

        dem = session.get(SpecialDemand, dem_id)
        if dem is None:
            dem = SpecialDemand(id=dem_id)
            session.add(dem)

        dem.group_id = r.get("GROUPID", 0) or 0
        dem.date = date
        dem.shift_id = r.get("SHIFTID", 0) or 0
        dem.workplace_id = r.get("WORKPLACID", 0) or 0
        dem.min_staff = r.get("MIN", 0) or 0
        dem.max_staff = r.get("MAX", 0) or 0
        count += 1

    if skipped:
        _log.warning("sync_special_demand: skipped %d row(s) with invalid date", skipped)
    session.flush()
    return count


def sync_cycles(session: Session, daten_path: str) -> int:
    """Sync rotation-cycle definitions from 5CYCLE.DBF."""
    rows = _read_dbf(daten_path, "CYCLE")
    count = 0

    for r in rows:
        cyc_id = r.get("ID")
        if not cyc_id:
            continue

        cyc = session.get(Cycle, cyc_id)
        if cyc is None:
            cyc = Cycle(id=cyc_id)
            session.add(cyc)

        cyc.name = str(r.get("NAME") or "").strip()
        cyc.position = r.get("POSITION", 0) or 0
        cyc.size = r.get("SIZE", 1) or 1
        cyc.unit = r.get("UNIT", 1) or 1
        cyc.hide = bool(r.get("HIDE"))
        count += 1

    session.flush()
    return count


def sync_cycle_assignments(session: Session, daten_path: str) -> int:
    """Sync employee↔cycle assignments from 5CYASS.DBF (full delete + re-insert).

    Like 5GRASG, the DBF ``ID`` is not relied upon as a unique key: the
    autoincrement PK is used instead and the logical identity
    ``(employee_id, cycle_id, start)`` is de-duplicated. Rows without an
    employee or cycle reference are skipped.
    """
    rows = _read_dbf(daten_path, "CYASS")

    session.query(CycleAssignment).delete()
    session.flush()

    count = 0
    seen: set[tuple[int, int, str]] = set()
    for r in rows:
        emp_id = r.get("EMPLOYEEID")
        cycle_id = r.get("CYCLEID")
        if not emp_id or not cycle_id:
            continue
        start = str(r.get("START") or "").strip()
        key = (emp_id, cycle_id, start)
        if key in seen:
            continue
        seen.add(key)
        session.add(
            CycleAssignment(
                employee_id=emp_id,
                cycle_id=cycle_id,
                start=start,
                end=str(r.get("END") or "").strip(),
                entrance=str(r.get("ENTRANCE") or "").strip(),
            )
        )
        count += 1

    session.flush()
    return count


def sync_restrictions(session: Session, daten_path: str) -> int:
    """Sync employee shift restrictions from 5RESTR.DBF (reason <- RESERVED)."""
    rows = _read_dbf(daten_path, "RESTR")
    count = 0

    for r in rows:
        res_id = r.get("ID")
        if not res_id:
            continue

        res = session.get(Restriction, res_id)
        if res is None:
            res = Restriction(id=res_id)
            session.add(res)

        res.employee_id = r.get("EMPLOYEEID", 0) or 0
        res.shift_id = r.get("SHIFTID", 0) or 0
        res.weekday = r.get("WEEKDAY", 0) or 0
        res.restrict = r.get("RESTRICT", 1) or 0
        res.reason = str(r.get("RESERVED") or "").strip()
        count += 1

    session.flush()
    return count


def sync_all(engine, daten_path: str) -> dict[str, int]:
    """Sync all supported tables from DBF into the ORM database.

    Returns dict of table_name → row_count.
    """
    session = get_session(engine)
    try:
        stats = {}
        stats["employees"] = sync_employees(session, daten_path)
        stats["groups"] = sync_groups(session, daten_path)
        stats["group_assignments"] = sync_group_assignments(session, daten_path)
        stats["shifts"] = sync_shifts(session, daten_path)
        stats["leave_types"] = sync_leave_types(session, daten_path)
        stats["workplaces"] = sync_workplaces(session, daten_path)
        stats["shift_assignments"] = sync_shift_assignments(session, daten_path)
        stats["special_shifts"] = sync_special_shifts(session, daten_path)
        stats["absences"] = sync_absences(session, daten_path)
        stats["holidays"] = sync_holidays(session, daten_path)
        stats["periods"] = sync_periods(session, daten_path)
        stats["bookings"] = sync_book(session, daten_path)
        stats["overtime"] = sync_overtime(session, daten_path)
        stats["leave_entitlements"] = sync_leave_entitlements(session, daten_path)
        stats["shift_demand"] = sync_shift_demand(session, daten_path)
        stats["special_demand"] = sync_special_demand(session, daten_path)
        stats["cycles"] = sync_cycles(session, daten_path)
        stats["cycle_assignments"] = sync_cycle_assignments(session, daten_path)
        stats["restrictions"] = sync_restrictions(session, daten_path)
        session.commit()
        _log.info("ORM sync complete: %s", stats)
        return stats
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
