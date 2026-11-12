"""
Database factory — returns the appropriate database backend based on configuration.

Usage:
    from sp5lib.db_factory import get_database

    db = get_database()  # Returns SP5Database or SP5PostgresDatabase
"""

import logging
import os

_log = logging.getLogger("sp5api.db_factory")

# Singleton für die PostgreSQL-Engine (teuer zu erzeugen)
_pg_instance = None


def get_database(db_path: str | None = None):
    """Return a database instance based on DB_BACKEND env variable.

    Args:
        db_path: Path to DBF directory (used only for DBF backend).
                 Ignored when DB_BACKEND=postgresql.

    Returns:
        SP5Database (DBF) or SP5PostgresDatabase (PostgreSQL)
    """
    from .db_config import BACKEND_POSTGRESQL, get_database_url, get_db_backend

    backend = get_db_backend()

    if backend == BACKEND_POSTGRESQL:
        global _pg_instance
        if _pg_instance is None:
            database_url = get_database_url()
            if not database_url:
                raise RuntimeError(
                    "DB_BACKEND=postgresql but DATABASE_URL is not set. "
                    "Set DATABASE_URL=postgresql://user:pass@host:5432/dbname"
                )
            from .pg_database import SP5PostgresDatabase
            _pg_instance = SP5PostgresDatabase(database_url)
            _pg_instance.init_db()
            _log.info("PostgreSQL backend initialized: %s", database_url.split("@")[-1] if "@" in database_url else "***")
        return _pg_instance
    else:
        from .database import SP5Database
        if db_path is None:
            db_path = os.environ.get(
                "SP5_DB_PATH",
                os.path.join(os.path.dirname(__file__), "..", "..", "..", "sp5_db", "Daten"),
            )
            db_path = os.path.normpath(db_path)
        return SP5Database(db_path)
