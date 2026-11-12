"""
SQLAlchemy ORM models for OpenSchichtplaner5.

Proof-of-concept covering Employees and Groups — the two most central
entities in the shift planning domain. Column names follow the existing
DBF field naming where sensible, with Pythonic aliases for readability.

These models are database-agnostic: they work identically on SQLite and
PostgreSQL (or any other SQLAlchemy-supported backend).
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class Company(Base):
    """Company / tenant — top-level organisational unit."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(
        String(200), nullable=False, unique=True, doc="Company name"
    )
    slug: Mapped[str] = mapped_column(
        String(200), nullable=False, unique=True, doc="URL-safe identifier"
    )
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.now()
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Relationships
    employees: Mapped[list["Employee"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    groups: Mapped[list["Group"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Company(id={self.id}, name='{self.name}', slug='{self.slug}')>"

    def to_dict(self) -> dict:
        return {
            "ID": self.id,
            "NAME": self.name,
            "SLUG": self.slug,
            "IS_ACTIVE": self.is_active,
        }


class Employee(Base):
    """Employee (Mitarbeiter) — maps to 5EMPL.DBF."""

    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position: Mapped[int] = mapped_column(Integer, default=0, doc="Sort order")
    number: Mapped[str | None] = mapped_column(
        String(50), default="", doc="Employee number / badge ID"
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, doc="Last name")
    firstname: Mapped[str | None] = mapped_column(
        String(100), default="", doc="First name"
    )
    shortname: Mapped[str | None] = mapped_column(
        String(10), default="", doc="Short identifier (e.g. HMU)"
    )
    sex: Mapped[int | None] = mapped_column(
        Integer, default=0, doc="Gender (0=unset, 1=m, 2=f)"
    )

    # Working hours
    hrsday: Mapped[float] = mapped_column(Float, default=0.0, doc="Hours per day")
    hrsweek: Mapped[float] = mapped_column(Float, default=0.0, doc="Hours per week")
    hrsmonth: Mapped[float] = mapped_column(Float, default=0.0, doc="Hours per month")
    workdays: Mapped[str | None] = mapped_column(
        String(30),
        default="1 1 1 1 1 0 0 0",
        doc="Working days bitmask (Mon-Sun + holiday)",
    )

    # Contact info
    salutation: Mapped[str | None] = mapped_column(String(50), default="")
    street: Mapped[str | None] = mapped_column(String(200), default="")
    zip: Mapped[str | None] = mapped_column(String(20), default="")
    town: Mapped[str | None] = mapped_column(String(100), default="")
    phone: Mapped[str | None] = mapped_column(String(50), default="")
    email: Mapped[str | None] = mapped_column(String(200), default="")

    # Employment dates
    birthday: Mapped[str | None] = mapped_column(
        String(10), default=None, doc="ISO date YYYY-MM-DD"
    )
    empstart: Mapped[str | None] = mapped_column(
        String(10), default=None, doc="Employment start date"
    )
    empend: Mapped[str | None] = mapped_column(
        String(10), default=None, doc="Employment end date"
    )
    function: Mapped[str | None] = mapped_column(
        String(100), default="", doc="Job title / function"
    )

    # Company
    company_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
        doc="Owning company (tenant)",
    )

    # Flags
    hide: Mapped[bool] = mapped_column(
        Boolean, default=False, doc="Soft-deleted / hidden"
    )

    # Notes (free-text fields from DBF)
    note1: Mapped[str | None] = mapped_column(Text, default="")
    note2: Mapped[str | None] = mapped_column(Text, default="")
    note3: Mapped[str | None] = mapped_column(Text, default="")
    note4: Mapped[str | None] = mapped_column(Text, default="")

    # Metadata
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    company: Mapped[Optional["Company"]] = relationship(back_populates="employees")
    group_assignments: Mapped[list["GroupAssignment"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Employee(id={self.id}, name='{self.name}', firstname='{self.firstname}')>"

    def to_dict(self) -> dict:
        """Convert to dictionary matching the existing API response format."""
        return {
            "ID": self.id,
            "POSITION": self.position,
            "NUMBER": self.number or "",
            "NAME": self.name,
            "FIRSTNAME": self.firstname or "",
            "SHORTNAME": self.shortname or "",
            "SEX": self.sex or 0,
            "HRSDAY": self.hrsday,
            "HRSWEEK": self.hrsweek,
            "HRSMONTH": self.hrsmonth,
            "WORKDAYS": self.workdays or "",
            "HIDE": self.hide,
            "EMAIL": self.email or "",
            "PHONE": self.phone or "",
            "FUNCTION": self.function or "",
            # Beschäftigungszeitraum: von der Berechnungsschicht zum Klemmen
            # der Auswertungszeiträume benötigt (Spec 3.1 Nr. 8).
            "BIRTHDAY": self.birthday or "",
            "EMPSTART": self.empstart or "",
            "EMPEND": self.empend or "",
        }


class Group(Base):
    """Shift group (Schichtgruppe) — maps to 5GROUP.DBF."""

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, doc="Group name")
    shortname: Mapped[str | None] = mapped_column(
        String(20), default="", doc="Short name"
    )
    super_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("groups.id", ondelete="SET NULL"),
        default=None,
        doc="Parent group ID (hierarchical groups)",
    )
    position: Mapped[int] = mapped_column(Integer, default=0, doc="Sort order")
    hide: Mapped[bool] = mapped_column(
        Boolean, default=False, doc="Soft-deleted / hidden"
    )

    # Company (tenant)
    company_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
        doc="Owning company (tenant)",
    )

    # Metadata
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    company: Mapped[Optional["Company"]] = relationship(back_populates="groups")
    member_assignments: Mapped[list["GroupAssignment"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    parent: Mapped[Optional["Group"]] = relationship(
        remote_side=[id], foreign_keys=[super_id]
    )

    def __repr__(self) -> str:
        return f"<Group(id={self.id}, name='{self.name}')>"

    def to_dict(self) -> dict:
        """Convert to dictionary matching the existing API response format."""
        return {
            "ID": self.id,
            "NAME": self.name,
            "SHORTNAME": self.shortname or "",
            "SUPERID": self.super_id or 0,
            "POSITION": self.position,
            "HIDE": self.hide,
        }


class GroupAssignment(Base):
    """Employee ↔ Group membership — maps to 5GRASG.DBF."""

    __tablename__ = "group_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )

    # Relationships
    employee: Mapped["Employee"] = relationship(back_populates="group_assignments")
    group: Mapped["Group"] = relationship(back_populates="member_assignments")

    # Unique constraint: one assignment per employee per group
    __table_args__ = (
        Index("idx_group_assignment_unique", "employee_id", "group_id", unique=True),
    )

    def __repr__(self) -> str:
        return f"<GroupAssignment(employee_id={self.employee_id}, group_id={self.group_id})>"


# ── Phase 2: shift / leave-type / workplace definitions ──────────────────────
# Canonical, backend-agnostic definitions (work identically on SQLite & Postgres).
# Re-exported from models_pg.py so existing `from sp5lib.orm.models_pg import …`
# imports keep working and the SQLite/Postgres schemas stay a single source of truth.


class Shift(Base):
    """Shift definition (Schicht) — maps to 5SHIFT.DBF."""

    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, doc="Shift name")
    shortname: Mapped[str | None] = mapped_column(String(20), default="")
    position: Mapped[int] = mapped_column(Integer, default=0, doc="Sort order")
    hide: Mapped[bool] = mapped_column(Boolean, default=False, doc="Soft-deleted / hidden")
    colortext: Mapped[int] = mapped_column(Integer, default=0)
    colorbar: Mapped[int] = mapped_column(Integer, default=0)
    colorbk: Mapped[int] = mapped_column(Integer, default=16777215)
    duration0: Mapped[float] = mapped_column(Float, default=0.0)
    duration1: Mapped[float] = mapped_column(Float, default=0.0)
    duration2: Mapped[float] = mapped_column(Float, default=0.0)
    duration3: Mapped[float] = mapped_column(Float, default=0.0)
    duration4: Mapped[float] = mapped_column(Float, default=0.0)
    duration5: Mapped[float] = mapped_column(Float, default=0.0)
    duration6: Mapped[float] = mapped_column(Float, default=0.0)
    duration7: Mapped[float] = mapped_column(Float, default=0.0)
    startend0: Mapped[str | None] = mapped_column(String(50), default="")
    startend1: Mapped[str | None] = mapped_column(String(50), default="")
    startend2: Mapped[str | None] = mapped_column(String(50), default="")
    startend3: Mapped[str | None] = mapped_column(String(50), default="")
    startend4: Mapped[str | None] = mapped_column(String(50), default="")
    startend5: Mapped[str | None] = mapped_column(String(50), default="")
    startend6: Mapped[str | None] = mapped_column(String(50), default="")
    startend7: Mapped[str | None] = mapped_column(String(50), default="")

    def __repr__(self) -> str:
        return f"<Shift(id={self.id}, name='{self.name}')>"

    def to_dict(self) -> dict:
        d = {
            "ID": self.id,
            "NAME": self.name,
            "SHORTNAME": self.shortname or "",
            "POSITION": self.position,
            "HIDE": self.hide,
            "COLORTEXT": self.colortext,
            "COLORBAR": self.colorbar,
            "COLORBK": self.colorbk,
        }
        for i in range(8):
            d[f"DURATION{i}"] = getattr(self, f"duration{i}")
            d[f"STARTEND{i}"] = getattr(self, f"startend{i}") or ""
        return d


class LeaveType(Base):
    """Leave / absence type (Abwesenheitsart) — maps to 5LEAVT.DBF."""

    __tablename__ = "leave_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, doc="Leave type name")
    shortname: Mapped[str | None] = mapped_column(String(20), default="")
    position: Mapped[int] = mapped_column(Integer, default=0, doc="Sort order")
    hide: Mapped[bool] = mapped_column(Boolean, default=False, doc="Soft-deleted / hidden")
    entitled: Mapped[bool] = mapped_column(Boolean, default=False)
    stdentit: Mapped[float] = mapped_column(Float, default=0.0)
    chargetyp: Mapped[int] = mapped_column(Integer, default=0)
    colortext: Mapped[int] = mapped_column(Integer, default=0)
    colorbar: Mapped[int] = mapped_column(Integer, default=0)
    colorbk: Mapped[int] = mapped_column(Integer, default=16777215)

    def __repr__(self) -> str:
        return f"<LeaveType(id={self.id}, name='{self.name}')>"

    def to_dict(self) -> dict:
        return {
            "ID": self.id,
            "NAME": self.name,
            "SHORTNAME": self.shortname or "",
            "POSITION": self.position,
            "HIDE": self.hide,
            "ENTITLED": self.entitled,
            "STDENTIT": self.stdentit,
            "CHARGETYP": self.chargetyp,
            "COLORTEXT": self.colortext,
            "COLORBAR": self.colorbar,
            "COLORBK": self.colorbk,
        }


class Workplace(Base):
    """Workplace / location (Arbeitsplatz) — maps to 5WOPL.DBF."""

    __tablename__ = "workplaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, doc="Workplace name")
    shortname: Mapped[str | None] = mapped_column(String(20), default="")
    position: Mapped[int] = mapped_column(Integer, default=0, doc="Sort order")
    hide: Mapped[bool] = mapped_column(Boolean, default=False, doc="Soft-deleted / hidden")
    colortext: Mapped[int] = mapped_column(Integer, default=0)
    colorbar: Mapped[int] = mapped_column(Integer, default=0)
    colorbk: Mapped[int] = mapped_column(Integer, default=16777215)

    def __repr__(self) -> str:
        return f"<Workplace(id={self.id}, name='{self.name}')>"

    def to_dict(self) -> dict:
        return {
            "ID": self.id,
            "NAME": self.name,
            "SHORTNAME": self.shortname or "",
            "POSITION": self.position,
            "HIDE": self.hide,
            "COLORTEXT": self.colortext,
            "COLORBAR": self.colorbar,
            "COLORBK": self.colorbk,
        }


# ── Phase 3: time-based schedule entries ─────────────────────────────────────
# The actual roster (not just the master-data definitions). Foreign keys to
# employees / shifts / leave types are modelled as plain indexed Integer
# columns *without* DB-level FK constraints, so dirty legacy data (dangling
# references) syncs without breaking. Defined canonically here and re-exported
# from models_pg.py (ShiftAssignment is also aliased there as ScheduleEntry).


class ShiftAssignment(Base):
    """Regular shift assignment (Dienstplan-Eintrag) — maps to 5MASHI.DBF."""

    __tablename__ = "schedule_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(Integer, nullable=False)
    date: Mapped[str] = mapped_column(String(10), nullable=False, doc="ISO date YYYY-MM-DD")
    shift_id: Mapped[int] = mapped_column(Integer, nullable=False)
    workplace_id: Mapped[int] = mapped_column(Integer, default=0)
    entry_type: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index("idx_schedule_emp_date", "employee_id", "date"),
        Index("idx_schedule_date", "date"),
    )

    def __repr__(self) -> str:
        return f"<ShiftAssignment(id={self.id}, emp={self.employee_id}, date='{self.date}')>"

    def to_dict(self) -> dict:
        return {
            "ID": self.id,
            "DATE": self.date,
            "EMPLOYEEID": self.employee_id,
            "SHIFTID": self.shift_id,
            "WORKPLACID": self.workplace_id,
            "TYPE": self.entry_type,
        }


class SpecialShift(Base):
    """Special / one-off shift (Sonderschicht) — maps to 5SPSHI.DBF."""

    __tablename__ = "special_shifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(Integer, nullable=False)
    date: Mapped[str] = mapped_column(String(10), nullable=False, doc="ISO date YYYY-MM-DD")
    name: Mapped[str | None] = mapped_column(String(100), default="")
    shortname: Mapped[str | None] = mapped_column(String(20), default="")
    shift_id: Mapped[int] = mapped_column(Integer, default=0)
    workplace_id: Mapped[int] = mapped_column(Integer, default=0)
    entry_type: Mapped[int] = mapped_column(Integer, default=0)
    colortext: Mapped[int] = mapped_column(Integer, default=0)
    colorbar: Mapped[int] = mapped_column(Integer, default=0)
    colorbk: Mapped[int] = mapped_column(Integer, default=16777215)
    bold: Mapped[int] = mapped_column(Integer, default=0)
    startend: Mapped[str | None] = mapped_column(String(50), default="")
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    noextra: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index("idx_spshi_emp_date", "employee_id", "date"),
        Index("idx_spshi_date", "date"),
    )

    def __repr__(self) -> str:
        return f"<SpecialShift(id={self.id}, emp={self.employee_id}, date='{self.date}')>"

    def to_dict(self) -> dict:
        return {
            "ID": self.id,
            "DATE": self.date,
            "EMPLOYEEID": self.employee_id,
            "SHIFTID": self.shift_id,
            "WORKPLACID": self.workplace_id,
            "TYPE": self.entry_type,
            "NAME": self.name or "",
            "SHORTNAME": self.shortname or "",
            "COLORTEXT": self.colortext,
            "COLORBAR": self.colorbar,
            "COLORBK": self.colorbk,
            "BOLD": self.bold,
            "STARTEND": self.startend or "",
            "DURATION": self.duration,
            "NOEXTRA": self.noextra,
        }


class Absence(Base):
    """Absence / leave entry (Abwesenheit) — maps to 5ABSEN.DBF."""

    __tablename__ = "absences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(Integer, nullable=False)
    date: Mapped[str] = mapped_column(String(10), nullable=False, doc="ISO date YYYY-MM-DD")
    leave_type_id: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_type: Mapped[int] = mapped_column(Integer, default=0)
    interval: Mapped[int] = mapped_column(Integer, default=0)
    start: Mapped[int] = mapped_column(Integer, default=0)
    end: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index("idx_absence_emp_date", "employee_id", "date"),
        Index("idx_absence_date", "date"),
    )

    def __repr__(self) -> str:
        return f"<Absence(id={self.id}, emp={self.employee_id}, date='{self.date}')>"

    def to_dict(self) -> dict:
        return {
            "ID": self.id,
            "DATE": self.date,
            "EMPLOYEEID": self.employee_id,
            "LEAVETYPID": self.leave_type_id,
            "TYPE": self.entry_type,
            "INTERVAL": self.interval,
            "START": self.start,
            "END": self.end,
        }


# ── Phase 4: reference tables (holidays, accounting periods) ─────────────────
# Holiday is defined canonically here and re-exported from models_pg.py. Period
# is new in Phase 4. group_id is a plain Integer (no DB-level FK), consistent
# with the FK-tolerant approach used by the schedule tables.


class Holiday(Base):
    """Public holiday (Feiertag) — maps to 5HOLID.DBF."""

    __tablename__ = "holidays"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False, doc="ISO date YYYY-MM-DD")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    interval: Mapped[int] = mapped_column(Integer, default=0, doc="1 = recurring every year")

    __table_args__ = (Index("idx_holiday_date", "date"),)

    def __repr__(self) -> str:
        return f"<Holiday(id={self.id}, date='{self.date}', name='{self.name}')>"

    def to_dict(self) -> dict:
        return {
            "ID": self.id,
            "DATE": self.date,
            "NAME": self.name,
            "INTERVAL": self.interval,
        }


class Period(Base):
    """Accounting / planning period (Periode) — maps to 5PERIO.DBF.

    The DBF stores the label in ``DESCRIPT`` (not ``NAME``); ``to_dict()``
    mirrors the real DBF keys.
    """

    __tablename__ = "periods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(Integer, default=0, doc="Owning group (no FK)")
    start: Mapped[str | None] = mapped_column(String(10), default="", doc="ISO start date")
    end: Mapped[str | None] = mapped_column(String(10), default="", doc="ISO end date")
    color: Mapped[int] = mapped_column(Integer, default=16777215)
    description: Mapped[str | None] = mapped_column(String(200), default="")

    __table_args__ = (Index("idx_period_group", "group_id"),)

    def __repr__(self) -> str:
        return f"<Period(id={self.id}, start='{self.start}', end='{self.end}')>"

    def to_dict(self) -> dict:
        return {
            "ID": self.id,
            "GROUPID": self.group_id,
            "START": self.start or "",
            "END": self.end or "",
            "COLOR": self.color,
            "DESCRIPT": self.description or "",
        }


# ── Phase 5: account bookings, overtime, leave entitlements ──────────────────
# Defined canonically here and re-exported from models_pg.py, where the former
# names Booking / OvertimeRecord remain available as aliases. References
# (employee_id / leave_type_id) are plain indexed integers without DB-level FK
# constraints, consistent with the other roster tables.


class AccountBooking(Base):
    """Manual account / time booking — maps to 5BOOK.DBF."""

    __tablename__ = "bookings_pg"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(Integer, nullable=False)
    date: Mapped[str] = mapped_column(String(10), nullable=False, doc="ISO date YYYY-MM-DD")
    booking_type: Mapped[int] = mapped_column(Integer, default=0, doc="DBF TYPE")
    value: Mapped[float] = mapped_column(Float, default=0.0, doc="DBF VALUE")
    note: Mapped[str | None] = mapped_column(Text, default="", doc="DBF NOTE")

    __table_args__ = (
        Index("idx_book_emp_date", "employee_id", "date"),
        Index("idx_book_date", "date"),
    )

    def __repr__(self) -> str:
        return f"<AccountBooking(id={self.id}, emp={self.employee_id}, date='{self.date}')>"

    def to_dict(self) -> dict:
        return {
            "ID": self.id,
            "EMPLOYEEID": self.employee_id,
            "DATE": self.date,
            "TYPE": self.booking_type,
            "VALUE": self.value,
            "NOTE": self.note or "",
        }


class OvertimeEntry(Base):
    """Manual overtime adjustment — maps to 5OVER.DBF."""

    __tablename__ = "overtime_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(Integer, nullable=False)
    date: Mapped[str] = mapped_column(String(10), nullable=False, doc="ISO date YYYY-MM-DD")
    hours: Mapped[float] = mapped_column(Float, default=0.0)

    __table_args__ = (
        Index("idx_overtime_emp_date", "employee_id", "date"),
        Index("idx_overtime_date", "date"),
    )

    def __repr__(self) -> str:
        return f"<OvertimeEntry(id={self.id}, emp={self.employee_id}, hours={self.hours})>"

    def to_dict(self) -> dict:
        return {
            "ID": self.id,
            "EMPLOYEEID": self.employee_id,
            "DATE": self.date,
            "HOURS": self.hours,
        }


class LeaveEntitlement(Base):
    """Annual leave entitlement per employee — maps to 5LEAEN.DBF.

    DBF fields: ENTITLEMNT (entitlement), REST (carry_forward), INDAYS (in_days).
    """

    __tablename__ = "leave_entitlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(Integer, nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    leave_type_id: Mapped[int] = mapped_column(Integer, default=0)
    entitlement: Mapped[float] = mapped_column(Float, default=0.0, doc="DBF ENTITLEMNT")
    carry_forward: Mapped[float] = mapped_column(Float, default=0.0, doc="DBF REST")
    in_days: Mapped[bool] = mapped_column(Boolean, default=True, doc="DBF INDAYS")

    __table_args__ = (Index("idx_leaen_emp_year", "employee_id", "year"),)

    def __repr__(self) -> str:
        return f"<LeaveEntitlement(id={self.id}, emp={self.employee_id}, year={self.year})>"

    def to_dict(self) -> dict:
        return {
            "ID": self.id,
            "EMPLOYEEID": self.employee_id,
            "YEAR": self.year,
            "LEAVETYPID": self.leave_type_id,
            "ENTITLEMNT": self.entitlement,
            "REST": self.carry_forward,
            "INDAYS": self.in_days,
        }


# ── Phase 6: demand, cycles, restrictions (read-mirror completion) ───────────
# Defined canonically here and re-exported from models_pg.py. StaffingRequirement
# remains available there as an alias of ShiftDemand. All references are plain
# indexed integers (no DB-level FK), consistent with the other roster tables.


class ShiftDemand(Base):
    """Recurring shift demand per weekday (Schichtbedarf) — maps to 5SHDEM.DBF."""

    __tablename__ = "staffing_requirements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(Integer, default=0)
    weekday: Mapped[int] = mapped_column(Integer, default=0)
    shift_id: Mapped[int] = mapped_column(Integer, default=0)
    workplace_id: Mapped[int] = mapped_column(Integer, default=0)
    min_staff: Mapped[int] = mapped_column(Integer, default=0, doc="DBF MIN")
    max_staff: Mapped[int] = mapped_column(Integer, default=0, doc="DBF MAX")

    __table_args__ = (
        Index("idx_shdem_shift", "shift_id"),
        Index("idx_shdem_weekday", "weekday"),
    )

    def __repr__(self) -> str:
        return f"<ShiftDemand(id={self.id}, shift={self.shift_id}, weekday={self.weekday})>"

    def to_dict(self) -> dict:
        return {
            "ID": self.id,
            "GROUPID": self.group_id,
            "WEEKDAY": self.weekday,
            "SHIFTID": self.shift_id,
            "WORKPLACID": self.workplace_id,
            "MIN": self.min_staff,
            "MAX": self.max_staff,
        }


class SpecialDemand(Base):
    """Date-specific shift demand (Sonderbedarf) — maps to 5SPDEM.DBF."""

    __tablename__ = "special_demands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(Integer, default=0)
    date: Mapped[str] = mapped_column(String(10), nullable=False, doc="ISO date YYYY-MM-DD")
    shift_id: Mapped[int] = mapped_column(Integer, default=0)
    workplace_id: Mapped[int] = mapped_column(Integer, default=0)
    min_staff: Mapped[int] = mapped_column(Integer, default=0, doc="DBF MIN")
    max_staff: Mapped[int] = mapped_column(Integer, default=0, doc="DBF MAX")

    __table_args__ = (
        Index("idx_spdem_date", "date"),
        Index("idx_spdem_shift", "shift_id"),
    )

    def __repr__(self) -> str:
        return f"<SpecialDemand(id={self.id}, date='{self.date}', shift={self.shift_id})>"

    def to_dict(self) -> dict:
        return {
            "ID": self.id,
            "GROUPID": self.group_id,
            "DATE": self.date,
            "SHIFTID": self.shift_id,
            "WORKPLACID": self.workplace_id,
            "MIN": self.min_staff,
            "MAX": self.max_staff,
        }


class Cycle(Base):
    """Shift rotation cycle definition (Zyklus) — maps to 5CYCLE.DBF."""

    __tablename__ = "cycles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    size: Mapped[int] = mapped_column(Integer, default=1, doc="Cycle length")
    unit: Mapped[int] = mapped_column(Integer, default=1)
    hide: Mapped[bool] = mapped_column(Boolean, default=False)

    def __repr__(self) -> str:
        return f"<Cycle(id={self.id}, name='{self.name}')>"

    def to_dict(self) -> dict:
        return {
            "ID": self.id,
            "NAME": self.name,
            "POSITION": self.position,
            "SIZE": self.size,
            "UNIT": self.unit,
            "HIDE": self.hide,
        }


class CycleAssignment(Base):
    """Employee ↔ rotation-cycle assignment — maps to 5CYASS.DBF."""

    __tablename__ = "cycle_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(Integer, nullable=False)
    cycle_id: Mapped[int] = mapped_column(Integer, nullable=False)
    start: Mapped[str | None] = mapped_column(String(10), default="")
    end: Mapped[str | None] = mapped_column(String(10), default="")
    entrance: Mapped[str | None] = mapped_column(String(10), default="")

    __table_args__ = (Index("idx_cyass_emp", "employee_id"),)

    def __repr__(self) -> str:
        return f"<CycleAssignment(id={self.id}, emp={self.employee_id}, cycle={self.cycle_id})>"

    def to_dict(self) -> dict:
        return {
            "ID": self.id,
            "EMPLOYEEID": self.employee_id,
            "CYCLEID": self.cycle_id,
            "START": self.start or "",
            "END": self.end or "",
            "ENTRANCE": self.entrance or "",
        }


class Restriction(Base):
    """Employee shift restriction / ban — maps to 5RESTR.DBF.

    The free-text reason is stored in the DBF ``RESERVED`` field.
    """

    __tablename__ = "restrictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(Integer, nullable=False)
    shift_id: Mapped[int] = mapped_column(Integer, nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, default=0)
    restrict: Mapped[int] = mapped_column(Integer, default=1)
    reason: Mapped[str | None] = mapped_column(String(20), default="", doc="DBF RESERVED")

    __table_args__ = (Index("idx_restr_emp", "employee_id"),)

    def __repr__(self) -> str:
        return f"<Restriction(id={self.id}, emp={self.employee_id}, shift={self.shift_id})>"

    def to_dict(self) -> dict:
        return {
            "ID": self.id,
            "EMPLOYEEID": self.employee_id,
            "SHIFTID": self.shift_id,
            "WEEKDAY": self.weekday,
            "RESTRICT": self.restrict,
            "RESERVED": self.reason or "",
        }
