"""
Extended SQLAlchemy ORM models for PostgreSQL backend.

These models cover all DBF tables beyond the core Employee/Group/GroupAssignment
models already defined in models.py.
"""


from sqlalchemy import (
    Boolean,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

# Master-data (Phase 2) and schedule (Phase 3) entities are defined canonically
# in models.py (single source of truth for the SQLite + Postgres schema) and
# re-exported here so existing `from sp5lib.orm.models_pg import …` imports keep
# working unchanged. ScheduleEntry is the legacy name for ShiftAssignment.
from .models import (  # noqa: F401,E402
    Absence,
    AccountBooking,
    Cycle,
    CycleAssignment,
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

# Backward-compatible aliases for models that were renamed when they moved to
# models.py (same tables, same columns) — keep the old names importable:
#   ScheduleEntry       -> ShiftAssignment (5MASHI, "schedule_entries")
#   Booking             -> AccountBooking  (5BOOK,  "bookings_pg")
#   OvertimeRecord      -> OvertimeEntry   (5OVER,  "overtime_records")
#   StaffingRequirement -> ShiftDemand     (5SHDEM, "staffing_requirements")
ScheduleEntry = ShiftAssignment
Booking = AccountBooking
OvertimeRecord = OvertimeEntry
StaffingRequirement = ShiftDemand


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    descrip: Mapped[str | None] = mapped_column(String(200), default="")
    admin: Mapped[bool] = mapped_column(Boolean, default=False)
    rights: Mapped[int] = mapped_column(Integer, default=0)
    digest: Mapped[bytes | None] = mapped_column(LargeBinary(16), default=None)
    bcrypt_hash: Mapped[str | None] = mapped_column(String(100), default=None)
    hide: Mapped[bool] = mapped_column(Boolean, default=False)
    wduties: Mapped[bool] = mapped_column(Boolean, default=False)
    wabsences: Mapped[bool] = mapped_column(Boolean, default=False)
    wovertimes: Mapped[bool] = mapped_column(Boolean, default=False)
    wnotes: Mapped[bool] = mapped_column(Boolean, default=False)
    wdeviation: Mapped[bool] = mapped_column(Boolean, default=False)
    wcycleass: Mapped[bool] = mapped_column(Boolean, default=False)
    wswaponly: Mapped[bool] = mapped_column(Boolean, default=False)
    wpast: Mapped[bool] = mapped_column(Boolean, default=False)
    waccemwnd: Mapped[bool] = mapped_column(Boolean, default=True)
    waccgrwnd: Mapped[bool] = mapped_column(Boolean, default=True)
    showabs: Mapped[bool] = mapped_column(Boolean, default=False)
    shownotes: Mapped[bool] = mapped_column(Boolean, default=True)
    showstats: Mapped[bool] = mapped_column(Boolean, default=True)
    raccemwnd: Mapped[bool] = mapped_column(Boolean, default=True)
    raccgrwnd: Mapped[bool] = mapped_column(Boolean, default=True)
    backup: Mapped[bool] = mapped_column(Boolean, default=False)
    accadmwnd: Mapped[bool] = mapped_column(Boolean, default=False)
    addempl: Mapped[int] = mapped_column(Integer, default=0)
    totp_secret: Mapped[str | None] = mapped_column(String(64), default=None)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_backup_codes: Mapped[str | None] = mapped_column(Text, default=None)


class CycleEntry(Base):
    __tablename__ = "cycle_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[int] = mapped_column(Integer, nullable=False)
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    shift_id: Mapped[int] = mapped_column(Integer, default=0)
    workplace_id: Mapped[int] = mapped_column(Integer, default=0)


class Note(Base):
    __tablename__ = "notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(Integer, default=0)
    date: Mapped[str] = mapped_column(String(10), nullable=False)
    text1: Mapped[str | None] = mapped_column(Text, default="")
    text2: Mapped[str | None] = mapped_column(Text, default="")
    category: Mapped[str | None] = mapped_column(String(20), default="")


class HolidayBan(Base):
    __tablename__ = "holiday_bans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(Integer, default=0)
    start_date: Mapped[str] = mapped_column(String(10), nullable=False)
    end_date: Mapped[str] = mapped_column(String(10), nullable=False)
    restrict: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str | None] = mapped_column(String(200), default="")


class ExtraCharge(Base):
    __tablename__ = "extra_charges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    start: Mapped[int] = mapped_column(Integer, default=0)
    end: Mapped[int] = mapped_column(Integer, default=0)
    validity: Mapped[int] = mapped_column(Integer, default=0)
    validdays: Mapped[str | None] = mapped_column(String(20), default="0000000")
    holrule: Mapped[int] = mapped_column(Integer, default=0)
    hide: Mapped[bool] = mapped_column(Boolean, default=False)


class Settings(Base):
    __tablename__ = "settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=0)
    login: Mapped[int] = mapped_column(Integer, default=0)
    spshcat: Mapped[int] = mapped_column(Integer, default=0)
    overtcat: Mapped[int] = mapped_column(Integer, default=0)
    anoaname: Mapped[str | None] = mapped_column(String(100), default="Abwesend")
    anoashort: Mapped[str | None] = mapped_column(String(20), default="X")


class ChangelogEntry(Base):
    __tablename__ = "changelog"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[str] = mapped_column(String(30), nullable=False)
    user: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[int | None] = mapped_column(Integer, default=None)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    entity: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, default=0)
    details: Mapped[str | None] = mapped_column(Text, default="")
    old_value: Mapped[str | None] = mapped_column(Text, default=None)
    new_value: Mapped[str | None] = mapped_column(Text, default=None)
