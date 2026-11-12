"""
SQLAlchemy ORM layer for OpenSchichtplaner5.

This package provides a database-agnostic ORM abstraction that can target
SQLite (development/testing) or PostgreSQL (production) without changing
application code. It coexists with the existing DBF-based data layer and
is intended as a migration path, not a replacement.

Usage:
    from sp5lib.orm import get_engine, get_session, init_db
    from sp5lib.orm.models import Employee, Group

    engine = get_engine("sqlite:///sp5.db")
    init_db(engine)

    with get_session(engine) as session:
        employees = session.query(Employee).filter_by(hide=False).all()
"""

from .base import Base, get_engine, get_session, init_db
from .models import (
    Absence,
    AccountBooking,
    Company,
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
from .repository import (
    AbsenceRepository,
    AccountBookingRepository,
    CycleAssignmentRepository,
    CycleRepository,
    EmployeeRepository,
    GroupRepository,
    HolidayRepository,
    LeaveEntitlementRepository,
    LeaveTypeRepository,
    OvertimeEntryRepository,
    PeriodRepository,
    RestrictionRepository,
    ShiftAssignmentRepository,
    ShiftDemandRepository,
    ShiftRepository,
    SpecialDemandRepository,
    SpecialShiftRepository,
    WorkplaceRepository,
)

# Legacy aliases for models renamed when they moved to models.py (same tables).
ScheduleEntry = ShiftAssignment
Booking = AccountBooking
OvertimeRecord = OvertimeEntry
StaffingRequirement = ShiftDemand

__all__ = [
    # Engine / session / schema
    "Base",
    "get_engine",
    "get_session",
    "init_db",
    # Master-data models
    "Company",
    "Employee",
    "Group",
    "GroupAssignment",
    "Shift",
    "LeaveType",
    "Workplace",
    # Schedule (Phase 3) models
    "ShiftAssignment",
    "ScheduleEntry",
    "SpecialShift",
    "Absence",
    # Reference (Phase 4) models
    "Holiday",
    "Period",
    # Account / overtime / entitlement (Phase 5) models
    "AccountBooking",
    "Booking",
    "OvertimeEntry",
    "OvertimeRecord",
    "LeaveEntitlement",
    # Demand / cycle / restriction (Phase 6) models
    "ShiftDemand",
    "StaffingRequirement",
    "SpecialDemand",
    "Cycle",
    "CycleAssignment",
    "Restriction",
    # Repositories
    "EmployeeRepository",
    "GroupRepository",
    "ShiftRepository",
    "LeaveTypeRepository",
    "WorkplaceRepository",
    "ShiftAssignmentRepository",
    "SpecialShiftRepository",
    "AbsenceRepository",
    "HolidayRepository",
    "PeriodRepository",
    "AccountBookingRepository",
    "OvertimeEntryRepository",
    "LeaveEntitlementRepository",
    "ShiftDemandRepository",
    "SpecialDemandRepository",
    "CycleRepository",
    "CycleAssignmentRepository",
    "RestrictionRepository",
]
