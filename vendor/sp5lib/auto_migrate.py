"""
Automatische Datenbank-Migration beim Anwendungsstart.

Vergleicht die Schema-Version der App mit der in der DB gespeicherten.
Liegt die DB zurück, laufen Alembic-Migrationen automatisch (PostgreSQL)
bzw. werden Schema-Erweiterungen angewandt (DBF).

Konfiguration über Umgebungsvariablen:
    AUTO_MIGRATE=false   → Auto-Migration deaktivieren (Default: true)
    AUTO_BACKUP=false    → Backup vor der Migration deaktivieren (Default: true)

Verwendung:
    from sp5lib.auto_migrate import run_startup_migration
    run_startup_migration()  # einmal im App-Lifespan-Startup aufrufen
"""

import logging
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from ._resource_paths import backend_dir as _backend_dir

_log = logging.getLogger("sp5api.auto_migrate")

# The current app schema version — bump this when adding new migrations.
# Format: Alembic revision head. We read it dynamically from Alembic.
APP_SCHEMA_VERSION = "head"

def _backend_path() -> Path:
    """Backend root directory (where alembic.ini lives), resolved lazily.

    Honors SP5_BACKEND_DIR so the host app can point at its alembic/ tree when this
    package is installed standalone. Evaluated on each call so it picks up the env
    var regardless of import order.
    """
    return Path(_backend_dir())


def _is_auto_migrate_enabled() -> bool:
    """Prüft, ob Auto-Migration via AUTO_MIGRATE-Env aktiviert ist."""
    val = os.environ.get("AUTO_MIGRATE", "true").lower().strip()
    return val not in ("false", "0", "no", "off")


def _is_auto_backup_enabled() -> bool:
    """Prüft, ob das Vor-Migrations-Backup via AUTO_BACKUP-Env aktiviert ist."""
    val = os.environ.get("AUTO_BACKUP", "true").lower().strip()
    return val not in ("false", "0", "no", "off")


def _get_alembic_head() -> str | None:
    """Liefert die aktuelle Alembic-Head-Revision aus den Migrationsskripten."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        alembic_cfg = Config(str(_backend_path() / "alembic.ini"))
        script = ScriptDirectory.from_config(alembic_cfg)
        head = script.get_current_head()
        return head
    except Exception as exc:
        _log.warning("Could not determine Alembic head revision: %s", exc)
        return None


def _get_db_revision(database_url: str) -> str | None:
    """Liefert die in der Datenbank gespeicherte Alembic-Revision."""
    try:
        from sqlalchemy import create_engine, inspect, text

        engine = create_engine(database_url)
        with engine.connect() as conn:
            # Check if alembic_version table exists
            inspector = inspect(engine)
            tables = inspector.get_table_names()
            if "alembic_version" not in tables:
                return None
            result = conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
            row = result.fetchone()
            return row[0] if row else None
    except Exception as exc:
        _log.warning("Could not read DB revision: %s", exc)
        return None


def _create_pg_backup(database_url: str) -> str | None:
    """Create a PostgreSQL backup before migration using pg_dump.

    Returns the backup file path on success, None on failure.
    """
    backup_dir = _backend_path() / "backups" / "pre_migration"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"pre_migration_{timestamp}.sql"

    try:
        # DATABASE_URL für pg_dump parsen
        # Format: postgresql://user:pass@host:port/dbname
        from urllib.parse import urlparse

        parsed = urlparse(database_url)
        env = os.environ.copy()
        if parsed.password:
            env["PGPASSWORD"] = parsed.password

        cmd = [
            "pg_dump",
            "-h", parsed.hostname or "localhost",
            "-p", str(parsed.port or 5432),
            "-U", parsed.username or "postgres",
            "-d", parsed.path.lstrip("/"),
            "-f", str(backup_file),
            "--no-owner",
            "--no-acl",
        ]
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            _log.info("Pre-migration backup created: %s", backup_file)
            return str(backup_file)
        else:
            _log.warning("pg_dump failed (rc=%d): %s", result.returncode, result.stderr)
            return None
    except FileNotFoundError:
        _log.warning("pg_dump not found — skipping PostgreSQL backup")
        return None
    except Exception as exc:
        _log.warning("Pre-migration backup failed: %s", exc)
        return None


def _create_dbf_backup(db_path: str) -> str | None:
    """Legt ein DBF-Backup als Kopie des Datenverzeichnisses an.

    Liefert bei Erfolg den Backup-Verzeichnispfad, sonst None.
    """
    if not os.path.isdir(db_path):
        _log.warning("DBF path does not exist, skipping backup: %s", db_path)
        return None

    backup_base = _backend_path() / "backups" / "pre_migration"
    backup_base.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_dir = backup_base / f"dbf_pre_migration_{timestamp}"

    try:
        shutil.copytree(db_path, str(backup_dir))
        _log.info("Pre-migration DBF backup created: %s", backup_dir)
        return str(backup_dir)
    except Exception as exc:
        _log.warning("DBF backup failed: %s", exc)
        return None


def _run_alembic_upgrade() -> list[str]:
    """Führt das Alembic-Upgrade auf head aus. Liefert die angewandten Revisions-IDs."""
    applied = []
    try:
        from alembic import command
        from alembic.config import Config

        alembic_cfg = Config(str(_backend_path() / "alembic.ini"))
        # Sicherstellen, dass DATABASE_URL in der Config steht
        database_url = os.environ.get("DATABASE_URL")
        if database_url:
            alembic_cfg.set_main_option("sqlalchemy.url", database_url)

        # Festhalten, welche Revisionen angewandt werden
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(alembic_cfg)
        current_rev = _get_db_revision(
            alembic_cfg.get_main_option("sqlalchemy.url") or database_url or ""
        )

        # Run upgrade
        command.upgrade(alembic_cfg, "head")

        # Determine which revisions were applied
        new_rev = _get_db_revision(
            alembic_cfg.get_main_option("sqlalchemy.url") or database_url or ""
        )
        if current_rev != new_rev:
            # Walk from current to head to list applied revisions
            for rev in script.walk_revisions(new_rev, current_rev):
                if rev.revision != current_rev:
                    applied.append(rev.revision)

        _log.info("Alembic upgrade complete. Applied %d revision(s): %s",
                   len(applied), applied)
    except Exception as exc:
        _log.error("Alembic upgrade failed: %s", exc, exc_info=True)
        raise

    return applied


def _get_dbf_schema_version(db_path: str) -> str | None:
    """Liest die Schema-Version aus der .sp5_version-Datei im DBF-Verzeichnis."""
    version_file = os.path.join(db_path, ".sp5_schema_version")
    if os.path.exists(version_file):
        try:
            return Path(version_file).read_text().strip()
        except Exception:
            return None
    return None


def _set_dbf_schema_version(db_path: str, version: str) -> None:
    """Schreibt die Schema-Version in die .sp5_version-Datei im DBF-Verzeichnis."""
    version_file = os.path.join(db_path, ".sp5_schema_version")
    try:
        Path(version_file).write_text(version)
    except Exception as exc:
        _log.warning("Could not write DBF schema version: %s", exc)


# Current DBF schema version — bump when adding new DBF schema extensions
DBF_SCHEMA_VERSION = "1.0.0"

# Registry der DBF-Schema-Erweiterungen: Version → Callable
# Jedes Callable erhält (db_path: str) und wendet seine Schema-Änderungen an.
_DBF_EXTENSIONS: dict[str, list] = {
    # Beispiel für künftige Nutzung:
    # "1.1.0": [_add_new_dbf_field],
}


def _apply_dbf_extensions(db_path: str, current_version: str | None) -> list[str]:
    """Apply pending DBF schema extensions.

    Returns list of version strings that were applied.
    """
    from packaging.version import InvalidVersion, Version

    applied = []
    try:
        current = Version(current_version) if current_version else Version("0.0.0")
    except InvalidVersion:
        current = Version("0.0.0")

    for ver_str, funcs in sorted(_DBF_EXTENSIONS.items()):
        try:
            ver = Version(ver_str)
        except InvalidVersion:
            continue
        if ver > current:
            _log.info("Applying DBF schema extension v%s...", ver_str)
            for func in funcs:
                func(db_path)
            applied.append(ver_str)

    if applied:
        _set_dbf_schema_version(db_path, DBF_SCHEMA_VERSION)
        _log.info("DBF schema updated to v%s. Applied: %s", DBF_SCHEMA_VERSION, applied)

    return applied


class MigrationResult:
    """Ergebnis eines Migrations-Laufs beim Start."""

    def __init__(self):
        self.backend: str = ""
        self.skipped: bool = False
        self.skip_reason: str = ""
        self.backup_path: str | None = None
        self.migrations_applied: list[str] = []
        self.previous_version: str | None = None
        self.current_version: str | None = None
        self.error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None

    @property
    def had_migrations(self) -> bool:
        return len(self.migrations_applied) > 0

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "backup_path": self.backup_path,
            "migrations_applied": self.migrations_applied,
            "previous_version": self.previous_version,
            "current_version": self.current_version,
            "error": self.error,
            "success": self.success,
        }

    def __repr__(self) -> str:
        if self.skipped:
            return f"MigrationResult(skipped={self.skip_reason})"
        if self.error:
            return f"MigrationResult(error={self.error})"
        return (
            f"MigrationResult(backend={self.backend}, "
            f"applied={len(self.migrations_applied)}, "
            f"{self.previous_version} → {self.current_version})"
        )


def run_startup_migration() -> MigrationResult:
    """Run automatic database migration on startup.

    This is the main entry point. Call once during application lifespan startup.

    Returns:
        MigrationResult with details about what happened.
    """
    result = MigrationResult()

    # Prüfen, ob Auto-Migration aktiviert ist
    if not _is_auto_migrate_enabled():
        result.skipped = True
        result.skip_reason = "AUTO_MIGRATE=false"
        _log.info("Auto-migration disabled (AUTO_MIGRATE=false)")
        return result

    from .db_config import BACKEND_POSTGRESQL, get_db_backend

    backend = get_db_backend()
    result.backend = backend

    if backend == BACKEND_POSTGRESQL:
        return _migrate_postgresql(result)
    else:
        return _migrate_dbf(result)


def _migrate_postgresql(result: MigrationResult) -> MigrationResult:
    """Handle PostgreSQL auto-migration via Alembic."""
    from .db_config import get_database_url

    database_url = get_database_url()
    if not database_url:
        result.skipped = True
        result.skip_reason = "DATABASE_URL not set"
        _log.warning("PostgreSQL backend but DATABASE_URL not set — skipping migration")
        return result

    # Get current DB revision
    current_rev = _get_db_revision(database_url)
    result.previous_version = current_rev

    # Get target (head) revision
    head_rev = _get_alembic_head()

    if current_rev == head_rev and current_rev is not None:
        result.current_version = current_rev
        result.skipped = True
        result.skip_reason = "already at head"
        _log.info("Database already at head revision (%s) — no migration needed", current_rev)
        return result

    _log.info("Migration needed: DB=%s → head=%s", current_rev or "(empty)", head_rev)

    # Create backup before migration
    if _is_auto_backup_enabled():
        result.backup_path = _create_pg_backup(database_url)

    # Run Alembic upgrade
    try:
        applied = _run_alembic_upgrade()
        result.migrations_applied = applied
        result.current_version = _get_db_revision(database_url)
        _log.info(
            "PostgreSQL migration complete: %s → %s (%d revision(s))",
            result.previous_version or "(empty)",
            result.current_version,
            len(applied),
        )
    except Exception as exc:
        result.error = str(exc)
        _log.error("PostgreSQL migration failed: %s", exc)

    return result


def _migrate_dbf(result: MigrationResult) -> MigrationResult:
    """Handle DBF schema extensions."""
    db_path = os.environ.get(
        "SP5_DB_PATH",
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "sp5_db", "Daten"),
    )
    db_path = os.path.normpath(db_path)

    if not os.path.isdir(db_path):
        result.skipped = True
        result.skip_reason = f"DBF path not found: {db_path}"
        _log.warning("DBF directory not found: %s — skipping migration", db_path)
        return result

    current_version = _get_dbf_schema_version(db_path)
    result.previous_version = current_version

    if current_version == DBF_SCHEMA_VERSION:
        result.current_version = current_version
        result.skipped = True
        result.skip_reason = "already at current version"
        _log.info("DBF schema already at v%s — no migration needed", current_version)
        return result

    _log.info("DBF migration needed: %s → %s",
              current_version or "(none)", DBF_SCHEMA_VERSION)

    # Create backup before migration
    if _is_auto_backup_enabled():
        result.backup_path = _create_dbf_backup(db_path)

    # Apply extensions
    try:
        applied = _apply_dbf_extensions(db_path, current_version)
        result.migrations_applied = applied

        # Auch ohne anwendbare Erweiterungen die Version stempeln
        _set_dbf_schema_version(db_path, DBF_SCHEMA_VERSION)
        result.current_version = DBF_SCHEMA_VERSION

        _log.info(
            "DBF migration complete: %s → %s (%d extension(s))",
            result.previous_version or "(none)",
            result.current_version,
            len(applied),
        )
    except Exception as exc:
        result.error = str(exc)
        _log.error("DBF migration failed: %s", exc)

    return result
