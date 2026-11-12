"""
Repository pattern for database-agnostic data access.

These repositories encapsulate all SQL queries behind a clean Python API.
The same code works on SQLite and PostgreSQL — switching backends only
requires changing the connection URL passed to get_engine().

This is the key benefit of the SQLAlchemy ORM: application code never
writes raw SQL, so a database migration is a config change, not a rewrite.
"""


from sqlalchemy import select
from sqlalchemy.orm import Session

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


class EmployeeRepository:
    """Data access for Employee entities."""

    def __init__(self, session: Session):
        self.session = session

    def get_all(self, include_hidden: bool = False) -> list[Employee]:
        """Return all employees, ordered by position."""
        stmt = select(Employee).order_by(Employee.position)
        if not include_hidden:
            stmt = stmt.where(Employee.hide == False)  # noqa: E712
        return list(self.session.scalars(stmt).all())

    def get_by_id(self, emp_id: int) -> Employee | None:
        """Return a single employee by ID, or None."""
        return self.session.get(Employee, emp_id)

    def create(self, **kwargs) -> Employee:
        """Create and persist a new employee."""
        emp = Employee(**kwargs)
        self.session.add(emp)
        self.session.flush()  # Assign ID without committing
        return emp

    def update(self, emp_id: int, **kwargs) -> Employee | None:
        """Update an employee by ID. Returns the updated employee or None."""
        emp = self.get_by_id(emp_id)
        if emp is None:
            return None
        for key, value in kwargs.items():
            if hasattr(emp, key):
                setattr(emp, key, value)
        self.session.flush()
        return emp

    def soft_delete(self, emp_id: int) -> bool:
        """Soft-delete an employee (set hide=True). Returns True if found."""
        emp = self.get_by_id(emp_id)
        if emp is None:
            return False
        emp.hide = True
        self.session.flush()
        return True

    def search(self, query: str, include_hidden: bool = False) -> list[Employee]:
        """Search employees by name or shortname (case-insensitive)."""
        pattern = f"%{query}%"
        stmt = (
            select(Employee)
            .where(
                (Employee.name.ilike(pattern))
                | (Employee.firstname.ilike(pattern))
                | (Employee.shortname.ilike(pattern))
            )
            .order_by(Employee.position)
        )
        if not include_hidden:
            stmt = stmt.where(Employee.hide == False)  # noqa: E712
        return list(self.session.scalars(stmt).all())

    def count(self, include_hidden: bool = False) -> int:
        """Return the total number of employees."""
        stmt = select(Employee)
        if not include_hidden:
            stmt = stmt.where(Employee.hide == False)  # noqa: E712
        return len(list(self.session.scalars(stmt).all()))


class GroupRepository:
    """Data access for Group entities."""

    def __init__(self, session: Session):
        self.session = session

    def get_all(self, include_hidden: bool = False) -> list[Group]:
        """Return all groups, ordered by position."""
        stmt = select(Group).order_by(Group.position)
        if not include_hidden:
            stmt = stmt.where(Group.hide == False)  # noqa: E712
        return list(self.session.scalars(stmt).all())

    def get_by_id(self, group_id: int) -> Group | None:
        """Return a single group by ID, or None."""
        return self.session.get(Group, group_id)

    def create(self, **kwargs) -> Group:
        """Create and persist a new group."""
        group = Group(**kwargs)
        self.session.add(group)
        self.session.flush()
        return group

    def update(self, group_id: int, **kwargs) -> Group | None:
        """Update a group by ID. Returns the updated group or None."""
        group = self.get_by_id(group_id)
        if group is None:
            return None
        for key, value in kwargs.items():
            if hasattr(group, key):
                setattr(group, key, value)
        self.session.flush()
        return group

    def soft_delete(self, group_id: int) -> bool:
        """Soft-delete a group (set hide=True). Returns True if found."""
        group = self.get_by_id(group_id)
        if group is None:
            return False
        group.hide = True
        self.session.flush()
        return True

    def get_members(self, group_id: int) -> list[Employee]:
        """Return all employees in a group."""
        stmt = (
            select(Employee)
            .join(GroupAssignment, GroupAssignment.employee_id == Employee.id)
            .where(GroupAssignment.group_id == group_id)
            .order_by(Employee.position)
        )
        return list(self.session.scalars(stmt).all())

    def get_member_ids(self, group_id: int) -> list[int]:
        """Return employee IDs in a group."""
        stmt = select(GroupAssignment.employee_id).where(
            GroupAssignment.group_id == group_id
        )
        return list(self.session.scalars(stmt).all())

    def add_member(self, group_id: int, employee_id: int) -> GroupAssignment:
        """Add an employee to a group. Idempotent — returns existing if already assigned."""
        existing = self.session.scalars(
            select(GroupAssignment).where(
                GroupAssignment.group_id == group_id,
                GroupAssignment.employee_id == employee_id,
            )
        ).first()
        if existing:
            return existing
        assignment = GroupAssignment(group_id=group_id, employee_id=employee_id)
        self.session.add(assignment)
        self.session.flush()
        return assignment

    def remove_member(self, group_id: int, employee_id: int) -> bool:
        """Remove an employee from a group. Returns True if found and removed."""
        assignment = self.session.scalars(
            select(GroupAssignment).where(
                GroupAssignment.group_id == group_id,
                GroupAssignment.employee_id == employee_id,
            )
        ).first()
        if assignment is None:
            return False
        self.session.delete(assignment)
        self.session.flush()
        return True

    def get_employee_groups(self, employee_id: int) -> list[Group]:
        """Return all groups an employee belongs to."""
        stmt = (
            select(Group)
            .join(GroupAssignment, GroupAssignment.group_id == Group.id)
            .where(GroupAssignment.employee_id == employee_id)
            .order_by(Group.position)
        )
        return list(self.session.scalars(stmt).all())


class ShiftRepository:
    """Data access for Shift definitions (5SHIFT.DBF)."""

    def __init__(self, session: Session):
        self.session = session

    def list(self, include_hidden: bool = False) -> list[Shift]:
        """Return all shifts, ordered by position."""
        stmt = select(Shift).order_by(Shift.position)
        if not include_hidden:
            stmt = stmt.where(Shift.hide == False)  # noqa: E712
        return list(self.session.scalars(stmt).all())

    def get(self, shift_id: int) -> Shift | None:
        """Return a single shift by ID, or None."""
        return self.session.get(Shift, shift_id)


class LeaveTypeRepository:
    """Data access for LeaveType definitions (5LEAVT.DBF)."""

    def __init__(self, session: Session):
        self.session = session

    def list(self, include_hidden: bool = False) -> list[LeaveType]:
        """Return all leave types, ordered by position."""
        stmt = select(LeaveType).order_by(LeaveType.position)
        if not include_hidden:
            stmt = stmt.where(LeaveType.hide == False)  # noqa: E712
        return list(self.session.scalars(stmt).all())

    def get(self, leave_type_id: int) -> LeaveType | None:
        """Return a single leave type by ID, or None."""
        return self.session.get(LeaveType, leave_type_id)


class WorkplaceRepository:
    """Data access for Workplace definitions (5WOPL.DBF)."""

    def __init__(self, session: Session):
        self.session = session

    def list(self, include_hidden: bool = False) -> list[Workplace]:
        """Return all workplaces, ordered by position."""
        stmt = select(Workplace).order_by(Workplace.position)
        if not include_hidden:
            stmt = stmt.where(Workplace.hide == False)  # noqa: E712
        return list(self.session.scalars(stmt).all())

    def get(self, workplace_id: int) -> Workplace | None:
        """Return a single workplace by ID, or None."""
        return self.session.get(Workplace, workplace_id)


class ShiftAssignmentRepository:
    """Data access for regular schedule entries (5MASHI.DBF)."""

    def __init__(self, session: Session):
        self.session = session

    def list(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        employee_id: int | None = None,
    ) -> list[ShiftAssignment]:
        """Return schedule entries filtered by ISO date range and/or employee."""
        stmt = select(ShiftAssignment)
        if date_from is not None:
            stmt = stmt.where(ShiftAssignment.date >= date_from)
        if date_to is not None:
            stmt = stmt.where(ShiftAssignment.date <= date_to)
        if employee_id is not None:
            stmt = stmt.where(ShiftAssignment.employee_id == employee_id)
        stmt = stmt.order_by(ShiftAssignment.date, ShiftAssignment.id)
        return list(self.session.scalars(stmt).all())

    def get(self, entry_id: int) -> ShiftAssignment | None:
        """Return a single schedule entry by ID, or None."""
        return self.session.get(ShiftAssignment, entry_id)


class SpecialShiftRepository:
    """Data access for special / one-off shifts (5SPSHI.DBF)."""

    def __init__(self, session: Session):
        self.session = session

    def list(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        employee_id: int | None = None,
    ) -> list[SpecialShift]:
        """Return special shifts filtered by ISO date range and/or employee."""
        stmt = select(SpecialShift)
        if date_from is not None:
            stmt = stmt.where(SpecialShift.date >= date_from)
        if date_to is not None:
            stmt = stmt.where(SpecialShift.date <= date_to)
        if employee_id is not None:
            stmt = stmt.where(SpecialShift.employee_id == employee_id)
        stmt = stmt.order_by(SpecialShift.date, SpecialShift.id)
        return list(self.session.scalars(stmt).all())

    def get(self, entry_id: int) -> SpecialShift | None:
        """Return a single special shift by ID, or None."""
        return self.session.get(SpecialShift, entry_id)


class AbsenceRepository:
    """Data access for absences / leave entries (5ABSEN.DBF)."""

    def __init__(self, session: Session):
        self.session = session

    def list(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        employee_id: int | None = None,
    ) -> list[Absence]:
        """Return absences filtered by ISO date range and/or employee."""
        stmt = select(Absence)
        if date_from is not None:
            stmt = stmt.where(Absence.date >= date_from)
        if date_to is not None:
            stmt = stmt.where(Absence.date <= date_to)
        if employee_id is not None:
            stmt = stmt.where(Absence.employee_id == employee_id)
        stmt = stmt.order_by(Absence.date, Absence.id)
        return list(self.session.scalars(stmt).all())

    def get(self, entry_id: int) -> Absence | None:
        """Return a single absence by ID, or None."""
        return self.session.get(Absence, entry_id)


class HolidayRepository:
    """Data access for public holidays (5HOLID.DBF)."""

    def __init__(self, session: Session):
        self.session = session

    def list(self, year: int | None = None) -> list[Holiday]:
        """Return holidays ordered by date.

        With ``year`` set, returns holidays in that calendar year plus all
        recurring (``interval == 1``) holidays, which apply every year.
        """
        stmt = select(Holiday).order_by(Holiday.date)
        if year is not None:
            prefix = f"{year:04d}-"
            stmt = stmt.where(
                (Holiday.date.startswith(prefix)) | (Holiday.interval == 1)
            )
        return list(self.session.scalars(stmt).all())

    def get(self, holiday_id: int) -> Holiday | None:
        """Return a single holiday by ID, or None."""
        return self.session.get(Holiday, holiday_id)


class PeriodRepository:
    """Data access for accounting / planning periods (5PERIO.DBF)."""

    def __init__(self, session: Session):
        self.session = session

    def list(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        group_id: int | None = None,
    ) -> list[Period]:
        """Return periods ordered by start date, optionally filtered.

        ``date_from`` / ``date_to`` filter on the period start date (ISO
        strings); ``group_id`` restricts to one group.
        """
        stmt = select(Period)
        if date_from is not None:
            stmt = stmt.where(Period.start >= date_from)
        if date_to is not None:
            stmt = stmt.where(Period.start <= date_to)
        if group_id is not None:
            stmt = stmt.where(Period.group_id == group_id)
        stmt = stmt.order_by(Period.start, Period.id)
        return list(self.session.scalars(stmt).all())

    def get(self, period_id: int) -> Period | None:
        """Return a single period by ID, or None."""
        return self.session.get(Period, period_id)


class AccountBookingRepository:
    """Data access for manual account / time bookings (5BOOK.DBF)."""

    def __init__(self, session: Session):
        self.session = session

    def list(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        employee_id: int | None = None,
    ) -> list[AccountBooking]:
        """Return bookings filtered by ISO date range and/or employee."""
        stmt = select(AccountBooking)
        if date_from is not None:
            stmt = stmt.where(AccountBooking.date >= date_from)
        if date_to is not None:
            stmt = stmt.where(AccountBooking.date <= date_to)
        if employee_id is not None:
            stmt = stmt.where(AccountBooking.employee_id == employee_id)
        stmt = stmt.order_by(AccountBooking.date, AccountBooking.id)
        return list(self.session.scalars(stmt).all())

    def get(self, booking_id: int) -> AccountBooking | None:
        """Return a single booking by ID, or None."""
        return self.session.get(AccountBooking, booking_id)


class OvertimeEntryRepository:
    """Data access for manual overtime adjustments (5OVER.DBF)."""

    def __init__(self, session: Session):
        self.session = session

    def list(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        employee_id: int | None = None,
    ) -> list[OvertimeEntry]:
        """Return overtime entries filtered by ISO date range and/or employee."""
        stmt = select(OvertimeEntry)
        if date_from is not None:
            stmt = stmt.where(OvertimeEntry.date >= date_from)
        if date_to is not None:
            stmt = stmt.where(OvertimeEntry.date <= date_to)
        if employee_id is not None:
            stmt = stmt.where(OvertimeEntry.employee_id == employee_id)
        stmt = stmt.order_by(OvertimeEntry.date, OvertimeEntry.id)
        return list(self.session.scalars(stmt).all())

    def get(self, entry_id: int) -> OvertimeEntry | None:
        """Return a single overtime entry by ID, or None."""
        return self.session.get(OvertimeEntry, entry_id)


class LeaveEntitlementRepository:
    """Data access for annual leave entitlements (5LEAEN.DBF)."""

    def __init__(self, session: Session):
        self.session = session

    def list(
        self,
        year: int | None = None,
        employee_id: int | None = None,
    ) -> list[LeaveEntitlement]:
        """Return leave entitlements filtered by year and/or employee."""
        stmt = select(LeaveEntitlement)
        if year is not None:
            stmt = stmt.where(LeaveEntitlement.year == year)
        if employee_id is not None:
            stmt = stmt.where(LeaveEntitlement.employee_id == employee_id)
        stmt = stmt.order_by(LeaveEntitlement.year, LeaveEntitlement.id)
        return list(self.session.scalars(stmt).all())

    def get(self, entitlement_id: int) -> LeaveEntitlement | None:
        """Return a single entitlement by ID, or None."""
        return self.session.get(LeaveEntitlement, entitlement_id)


class ShiftDemandRepository:
    """Data access for recurring shift demand (5SHDEM.DBF)."""

    def __init__(self, session: Session):
        self.session = session

    def list(
        self,
        shift_id: int | None = None,
        weekday: int | None = None,
        group_id: int | None = None,
    ) -> list[ShiftDemand]:
        """Return shift demand rows, optionally filtered by shift/weekday/group."""
        stmt = select(ShiftDemand)
        if shift_id is not None:
            stmt = stmt.where(ShiftDemand.shift_id == shift_id)
        if weekday is not None:
            stmt = stmt.where(ShiftDemand.weekday == weekday)
        if group_id is not None:
            stmt = stmt.where(ShiftDemand.group_id == group_id)
        stmt = stmt.order_by(ShiftDemand.weekday, ShiftDemand.shift_id, ShiftDemand.id)
        return list(self.session.scalars(stmt).all())

    def get(self, demand_id: int) -> ShiftDemand | None:
        """Return a single shift-demand row by ID, or None."""
        return self.session.get(ShiftDemand, demand_id)


class SpecialDemandRepository:
    """Data access for date-specific shift demand (5SPDEM.DBF)."""

    def __init__(self, session: Session):
        self.session = session

    def list(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        shift_id: int | None = None,
    ) -> list[SpecialDemand]:
        """Return special demand rows filtered by ISO date range and/or shift."""
        stmt = select(SpecialDemand)
        if date_from is not None:
            stmt = stmt.where(SpecialDemand.date >= date_from)
        if date_to is not None:
            stmt = stmt.where(SpecialDemand.date <= date_to)
        if shift_id is not None:
            stmt = stmt.where(SpecialDemand.shift_id == shift_id)
        stmt = stmt.order_by(SpecialDemand.date, SpecialDemand.id)
        return list(self.session.scalars(stmt).all())

    def get(self, demand_id: int) -> SpecialDemand | None:
        """Return a single special-demand row by ID, or None."""
        return self.session.get(SpecialDemand, demand_id)


class CycleRepository:
    """Data access for rotation-cycle definitions (5CYCLE.DBF)."""

    def __init__(self, session: Session):
        self.session = session

    def list(self, include_hidden: bool = False) -> list[Cycle]:
        """Return all cycles, ordered by position."""
        stmt = select(Cycle).order_by(Cycle.position)
        if not include_hidden:
            stmt = stmt.where(Cycle.hide == False)  # noqa: E712
        return list(self.session.scalars(stmt).all())

    def get(self, cycle_id: int) -> Cycle | None:
        """Return a single cycle by ID, or None."""
        return self.session.get(Cycle, cycle_id)


class CycleAssignmentRepository:
    """Data access for employee ↔ cycle assignments (5CYASS.DBF)."""

    def __init__(self, session: Session):
        self.session = session

    def list(
        self,
        employee_id: int | None = None,
        cycle_id: int | None = None,
    ) -> list[CycleAssignment]:
        """Return cycle assignments, optionally filtered by employee/cycle."""
        stmt = select(CycleAssignment)
        if employee_id is not None:
            stmt = stmt.where(CycleAssignment.employee_id == employee_id)
        if cycle_id is not None:
            stmt = stmt.where(CycleAssignment.cycle_id == cycle_id)
        stmt = stmt.order_by(CycleAssignment.employee_id, CycleAssignment.start, CycleAssignment.id)
        return list(self.session.scalars(stmt).all())

    def get(self, assignment_id: int) -> CycleAssignment | None:
        """Return a single cycle assignment by ID, or None."""
        return self.session.get(CycleAssignment, assignment_id)


class RestrictionRepository:
    """Data access for employee shift restrictions (5RESTR.DBF)."""

    def __init__(self, session: Session):
        self.session = session

    def list(
        self,
        employee_id: int | None = None,
        shift_id: int | None = None,
    ) -> list[Restriction]:
        """Return restrictions, optionally filtered by employee/shift."""
        stmt = select(Restriction)
        if employee_id is not None:
            stmt = stmt.where(Restriction.employee_id == employee_id)
        if shift_id is not None:
            stmt = stmt.where(Restriction.shift_id == shift_id)
        stmt = stmt.order_by(Restriction.employee_id, Restriction.id)
        return list(self.session.scalars(stmt).all())

    def get(self, restriction_id: int) -> Restriction | None:
        """Return a single restriction by ID, or None."""
        return self.session.get(Restriction, restriction_id)
