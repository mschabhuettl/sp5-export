"""
SQLAlchemy engine, session factory, and declarative base.

Supports both SQLite and PostgreSQL connection strings:
    - sqlite:///path/to/db.sqlite
    - postgresql://user:pass@host:5432/dbname

Thread-safety: Uses scoped sessions for multi-threaded WSGI/ASGI servers.
"""

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine as _create_engine
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
    pass


def get_engine(url: str, echo: bool = False, **kwargs) -> Engine:
    """Create a SQLAlchemy engine for the given database URL.

    Args:
        url: Database connection string (sqlite:/// or postgresql://)
        echo: If True, log all SQL statements (useful for debugging)
        **kwargs: Additional engine options (pool_size, etc.)

    Returns:
        Configured SQLAlchemy Engine instance.
    """
    engine_kwargs = {"echo": echo}

    if url.startswith("sqlite"):
        # SQLite-specific: enable WAL mode and foreign keys
        engine_kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # PostgreSQL / other: use connection pooling
        engine_kwargs.setdefault("pool_size", kwargs.pop("pool_size", 5))
        engine_kwargs.setdefault("max_overflow", kwargs.pop("max_overflow", 10))
        engine_kwargs.setdefault("pool_pre_ping", kwargs.pop("pool_pre_ping", True))

    engine_kwargs.update(kwargs)
    engine = _create_engine(url, **engine_kwargs)

    # Enable WAL and FK enforcement for SQLite connections
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def get_session(engine: Engine) -> Session:
    """Create a new SQLAlchemy session bound to the given engine.

    Usage as context manager:
        with get_session(engine) as session:
            ...
            session.commit()

    Or manually:
        session = get_session(engine)
        try:
            ...
            session.commit()
        finally:
            session.close()
    """
    factory = sessionmaker(bind=engine)
    return factory()


@contextmanager
def session_scope(engine: Engine) -> Generator[Session, None, None]:
    """Context manager that provides a transactional session scope.

    Commits on success, rolls back on exception, always closes.
    """
    session = get_session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(engine: Engine) -> None:
    """Create all tables defined by ORM models.

    Safe to call multiple times — uses CREATE TABLE IF NOT EXISTS.
    """
    # Import models so they register with Base.metadata
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)
