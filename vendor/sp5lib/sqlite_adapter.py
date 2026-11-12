"""
SQLite Adapter — Proof of Concept for SP5 DBF → SQLite migration.

This module is purely ADDITIVE. It does NOT replace database.py or dbf_reader.py.
Its purpose is to demonstrate a clean SQLite mirror of the three core tables
(EMPL, GROUP, BOOK) and serve as a foundation for a future full migration.

Usage:
    from sp5lib.sqlite_adapter import SP5SQLiteAdapter
    adapter = SP5SQLiteAdapter("/path/to/sp5.sqlite")
    adapter.init_db()
    adapter.sync_from_dbf("/path/to/sp5_db/Daten")
    employees = adapter.get_employees()

Design goals:
  - No side effects on the DBF layer (read-only access to DBF files)
  - Thread-safe via SQLite WAL mode + connection-per-call pattern
  - Schema closely mirrors DBF field names for easy 1:1 comparison
"""

import logging
import os
import sqlite3
from datetime import UTC
from typing import Any

_log = logging.getLogger("sp5api.sqlite_adapter")


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS employees (
    id          INTEGER PRIMARY KEY,   -- EMPL.ID
    position    INTEGER,               -- EMPL.POSITION (sort order)
    number      TEXT,                  -- EMPL.NUMBER (employee number/badge)
    name        TEXT NOT NULL,         -- EMPL.NAME
    firstname   TEXT,                  -- EMPL.FIRSTNAME
    shortname   TEXT,                  -- EMPL.SHORTNAME
    salutation  TEXT,                  -- EMPL.SALUTATION
    street      TEXT,
    zip         TEXT,
    town        TEXT,
    phone       TEXT,
    email       TEXT,
    photo       TEXT,
    function    TEXT,
    sex         TEXT,
    birthday    TEXT,                  -- ISO date YYYY-MM-DD
    empstart    TEXT,                  -- ISO date YYYY-MM-DD
    empend      TEXT,                  -- ISO date YYYY-MM-DD
    active      INTEGER DEFAULT 1,     -- derived: 1 if not deleted
    synced_at   TEXT                   -- UTC timestamp of last sync
);

CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY,   -- GROUP.ID
    name        TEXT NOT NULL,         -- GROUP.NAME
    shortname   TEXT,                  -- GROUP.SHORTNAME
    super_id    INTEGER REFERENCES groups(id),  -- GROUP.SUPERID (parent group)
    position    INTEGER,               -- GROUP.POSITION
    hide        INTEGER DEFAULT 0,     -- GROUP.HIDE
    synced_at   TEXT
);

CREATE TABLE IF NOT EXISTS bookings (
    id          INTEGER PRIMARY KEY,   -- BOOK.ID
    employee_id INTEGER REFERENCES employees(id),  -- BOOK.EMPLOYEEID
    date        TEXT NOT NULL,         -- BOOK.DATE (ISO YYYY-MM-DD)
    type        INTEGER,               -- BOOK.TYPE (booking type code)
    value       REAL,                  -- BOOK.VALUE (hours or units)
    note        TEXT,                  -- BOOK.NOTE
    synced_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_bookings_employee ON bookings(employee_id);
CREATE INDEX IF NOT EXISTS idx_bookings_date ON bookings(date);
CREATE INDEX IF NOT EXISTS idx_groups_super ON groups(super_id);
"""


# ---------------------------------------------------------------------------
# Adapter class
# ---------------------------------------------------------------------------


class SP5SQLiteAdapter:
    """Dünne SQLite-Schicht, die die drei Kern-DBF-Tabellen spiegelt."""

    def __init__(self, sqlite_path: str):
        self.sqlite_path = sqlite_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ------------------------------------------------------------------ #
    #  Schema                                                              #
    # ------------------------------------------------------------------ #

    def init_db(self) -> None:
        """Create all tables if they don't exist yet."""
        os.makedirs(os.path.dirname(self.sqlite_path) or ".", exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_DDL)
        _log.info("SQLite schema initialised at %s", self.sqlite_path)

    # ------------------------------------------------------------------ #
    #  Sync from DBF                                                       #
    # ------------------------------------------------------------------ #

    def sync_from_dbf(self, daten_path: str) -> dict[str, int]:
        """
        Read the three core DBF tables and upsert into SQLite.

        Returns a dict with row counts per table:
            {"employees": N, "groups": M, "bookings": K}

        This is a full replace sync (DELETE + INSERT) for simplicity.
        A production version would use incremental change detection.
        """
        # Import hier, um Zirkular-Abhängigkeiten bei Standalone-Nutzung zu vermeiden
        try:
            from .dbf_reader import read_dbf
        except ImportError:
            # Standalone-Betrieb des Adapters erlauben (z. B. für Tests)
            import sys

            sys.path.insert(0, os.path.dirname(__file__))
            from dbf_reader import read_dbf  # type: ignore[no-redef]

        def _dbf(name: str) -> list[dict[str, Any]]:
            path = os.path.join(daten_path, f"5{name}.DBF")
            try:
                return read_dbf(path)
            except Exception as exc:
                _log.warning("Could not read %s: %s", path, exc)
                return []

        from datetime import datetime

        now = datetime.now(UTC).isoformat()

        def _date(val) -> str | None:
            """Normalise SP5 date value to ISO string or None."""
            if not val:
                return None
            s = str(val).strip()
            if len(s) == 8 and s.isdigit():
                return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
            if len(s) == 10 and s[4] == "-":
                return s
            return None

        employees = _dbf("EMPL")
        groups = _dbf("GROUP")
        bookings = _dbf("BOOK")

        conn = self._connect()
        try:
            # FK-Checks für den ganzen Sync deaktivieren (selbstreferenzierende Gruppen etc.)
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN")
            # ── employees ──────────────────────────────────────────────
            conn.execute("DELETE FROM employees")
            conn.executemany(
                """INSERT INTO employees
                   (id, position, number, name, firstname, shortname,
                    salutation, street, zip, town, phone, email, photo,
                    function, sex, birthday, empstart, empend, active, synced_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        r.get("ID"),
                        r.get("POSITION"),
                        str(r.get("NUMBER") or "").strip(),
                        str(r.get("NAME") or "").strip(),
                        str(r.get("FIRSTNAME") or "").strip(),
                        str(r.get("SHORTNAME") or "").strip(),
                        str(r.get("SALUTATION") or "").strip(),
                        str(r.get("STREET") or "").strip(),
                        str(r.get("ZIP") or "").strip(),
                        str(r.get("TOWN") or "").strip(),
                        str(r.get("PHONE") or "").strip(),
                        str(r.get("EMAIL") or "").strip(),
                        str(r.get("PHOTO") or "").strip(),
                        str(r.get("FUNCTION") or "").strip(),
                        str(r.get("SEX") or "").strip(),
                        _date(r.get("BIRTHDAY")),
                        _date(r.get("EMPSTART")),
                        _date(r.get("EMPEND")),
                        1,
                        now,
                    )
                    for r in employees
                    if r.get("ID")
                ],
            )

            # ── groups ─────────────────────────────────────────────────
            conn.execute("DELETE FROM groups")
            conn.executemany(
                """INSERT INTO groups
                   (id, name, shortname, super_id, position, hide, synced_at)
                   VALUES (?,?,?,?,?,?,?)""",
                [
                    (
                        r.get("ID"),
                        str(r.get("NAME") or "").strip(),
                        str(r.get("SHORTNAME") or "").strip(),
                        r.get("SUPERID") or None,
                        r.get("POSITION"),
                        1 if r.get("HIDE") else 0,
                        now,
                    )
                    for r in groups
                    if r.get("ID")
                ],
            )

            # ── bookings ───────────────────────────────────────────────
            conn.execute("DELETE FROM bookings")
            conn.executemany(
                """INSERT INTO bookings
                   (id, employee_id, date, type, value, note, synced_at)
                   VALUES (?,?,?,?,?,?,?)""",
                [
                    (
                        r.get("ID"),
                        r.get("EMPLOYEEID"),
                        _date(r.get("DATE")),
                        r.get("TYPE"),
                        r.get("VALUE"),
                        str(r.get("NOTE") or "").strip(),
                        now,
                    )
                    for r in bookings
                    if r.get("ID")
                ],
            )
            conn.execute("COMMIT")
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        counts = {
            "employees": len(employees),
            "groups": len(groups),
            "bookings": len(bookings),
        }
        _log.info("Sync complete: %s", counts)
        return counts

    # ------------------------------------------------------------------ #
    #  Simple query helpers (demo only)                                    #
    # ------------------------------------------------------------------ #

    def get_employees(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM employees WHERE active=1 ORDER BY position, name"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_groups(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM groups ORDER BY position, name"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_bookings_for_employee(self, employee_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM bookings WHERE employee_id=? ORDER BY date",
                (employee_id,),
            ).fetchall()
        return [dict(r) for r in rows]
