"""
Database backend configuration.

Controls which database backend is used: DBF (legacy) or PostgreSQL.
Configuration via environment variables:

  DB_BACKEND=dbf          → Use DBF files (default)
  DB_BACKEND=postgresql   → Use PostgreSQL via SQLAlchemy

  DATABASE_URL=postgresql://user:pass@host:5432/dbname
  SP5_DB_PATH=/path/to/Daten  → DBF directory (for dbf backend)
"""

import logging
import os

_log = logging.getLogger("sp5api.db_config")

# Valid backend choices
BACKEND_DBF = "dbf"
BACKEND_POSTGRESQL = "postgresql"

def get_db_backend() -> str:
    """Liefert das konfigurierte Datenbank-Backend ('dbf' oder 'postgresql')."""
    backend = os.environ.get("DB_BACKEND", BACKEND_DBF).lower().strip()
    if backend not in (BACKEND_DBF, BACKEND_POSTGRESQL):
        _log.warning("Unknown DB_BACKEND=%r, falling back to 'dbf'", backend)
        return BACKEND_DBF
    return backend


def get_database_url() -> str | None:
    """Liefert die DATABASE_URL für PostgreSQL, sonst None."""
    return os.environ.get("DATABASE_URL")


def is_postgresql() -> bool:
    """Liefert True, wenn das PostgreSQL-Backend aktiv ist."""
    return get_db_backend() == BACKEND_POSTGRESQL
