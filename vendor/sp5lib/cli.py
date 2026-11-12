"""sp5lib — Kommandozeilen-Werkzeuge für Schichtplaner5-DBF-Datenbanken.

    sp5lib info     /pfad/zu/Daten
    sp5lib dump     /pfad/zu/Daten 5EMPL [--json|--csv] [--limit N]
    sp5lib validate /pfad/zu/Daten
    sp5lib sync     /pfad/zu/Daten --target sqlite:/pfad/sp5.db
    sp5lib sync     /pfad/zu/Daten --target postgres://user:pw@host:5432/db
"""

import argparse
import csv
import json
import os
import sys
from typing import Any

from sp5lib.dbf_reader import _dedupe_names, get_table_fields, read_dbf


def _dbf_files(db_dir: str) -> list[str]:
    """Alle .DBF-Dateinamen im Verzeichnis, sortiert."""
    return sorted(
        (n for n in os.listdir(db_dir) if n.upper().endswith(".DBF")),
        key=str.upper,
    )


def _resolve_table(db_dir: str, table: str) -> str | None:
    """Tabellennamen tolerant auflösen: EMPL, 5EMPL, 5empl.dbf → 5EMPL.DBF."""
    wanted = table.upper()
    candidates = {wanted, f"{wanted}.DBF", f"5{wanted}", f"5{wanted}.DBF"}
    for name in _dbf_files(db_dir):
        if name.upper() in candidates:
            return os.path.join(db_dir, name)
    return None


def _plain(value: Any) -> Any:
    """Binärfelder (bytes) für JSON/CSV als Hex-String darstellen."""
    return value.hex() if isinstance(value, bytes) else value


def _target_url(target: str) -> str:
    """``sqlite:PATH`` / ``postgres:URL`` in eine SQLAlchemy-URL übersetzen."""
    scheme, _, rest = target.partition(":")
    if scheme == "sqlite":
        if not rest:
            raise ValueError("sqlite-Target ohne Pfad (erwartet sqlite:PATH)")
        return "sqlite:///" + os.path.abspath(rest)
    if scheme in ("postgres", "postgresql"):
        if rest.startswith(("postgres:", "postgresql:")):
            return _target_url(rest)  # postgres:postgresql://… → auspacken
        if rest.startswith("//"):  # postgres://user:pw@host/db
            return "postgresql:" + rest
        if rest:  # postgres:user:pw@host/db
            return "postgresql://" + rest
        raise ValueError("postgres-Target ohne URL (erwartet postgres:URL)")
    raise ValueError(f"Unbekanntes Target {target!r} (erwartet sqlite:PATH oder postgres:URL)")


def cmd_info(args: argparse.Namespace) -> int:
    files = _dbf_files(args.db_dir)
    if not files:
        print(f"Keine .DBF-Dateien in {args.db_dir}", file=sys.stderr)
        return 1

    print(f"{'Tabelle':<16} {'Records':>8} {'Felder':>7}")
    total = 0
    build = None
    for name in files:
        path = os.path.join(args.db_dir, name)
        fields = get_table_fields(path)
        if not fields:
            print(f"{name:<16} {'-':>8} {'-':>7}  (Header nicht lesbar)")
            continue
        records = read_dbf(path)
        total += len(records)
        print(f"{name:<16} {len(records):>8} {len(fields):>7}")
        if name.upper() == "5BUILD.DBF" and records:
            build = records[0].get("BUILD")
    print(f"\nGesamt: {len(files)} Tabellen, {total} Records")
    if build is not None:
        print(f"Schichtplaner5-Build: {build}")
    return 0


def cmd_dump(args: argparse.Namespace) -> int:
    path = _resolve_table(args.db_dir, args.table)
    if path is None:
        print(f"Tabelle {args.table!r} nicht gefunden in {args.db_dir}", file=sys.stderr)
        return 1

    records = read_dbf(path)
    if args.limit is not None:
        records = records[: args.limit]

    if args.csv:
        names = _dedupe_names([str(f["name"]) for f in get_table_fields(path)])
        writer = csv.DictWriter(sys.stdout, fieldnames=names)
        writer.writeheader()
        for record in records:
            writer.writerow({k: _plain(v) for k, v in record.items()})
    else:
        json.dump(records, sys.stdout, ensure_ascii=False, indent=2, default=_plain)
        print()
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    files = _dbf_files(args.db_dir)
    if not files:
        print(f"Keine .DBF-Dateien in {args.db_dir}", file=sys.stderr)
        return 1

    problems = 0
    for name in files:
        path = os.path.join(args.db_dir, name)
        fields = get_table_fields(path)
        if not fields:
            print(f"FEHLER   {name}: Header nicht lesbar")
            problems += 1
            continue
        bad_names = [str(f["name"]) for f in fields if "�" in str(f["name"])]
        if bad_names:
            print(f"ENCODING {name}: defekte Feldnamen {bad_names}")
            problems += 1
            continue
        records = read_dbf(path)
        bad_values = sum(
            1 for r in records for v in r.values() if isinstance(v, str) and "�" in v
        )
        if bad_values:
            print(f"ENCODING {name}: {bad_values} Feldwerte mit Ersatzzeichen (U+FFFD)")
            problems += 1
        else:
            print(f"OK       {name} ({len(records)} Records)")

    if problems:
        print(f"\n{problems} von {len(files)} Tabellen mit Problemen")
        return 1
    print(f"\nAlle {len(files)} Tabellen fehlerfrei gelesen")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    try:
        url = _target_url(args.target)
    except ValueError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2

    from sp5lib.orm import get_engine, init_db
    from sp5lib.orm.sync import sync_all

    try:
        engine = get_engine(url)
        init_db(engine)
        stats = sync_all(engine, args.db_dir)
    except Exception as exc:
        print(f"Sync fehlgeschlagen: {exc}", file=sys.stderr)
        return 1

    for table, count in stats.items():
        print(f"{table:<20} {count:>6}")
    # str(engine.url) maskiert ein Passwort automatisch als ***
    print(f"\nGesamt: {sum(stats.values())} Records → {engine.url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sp5lib",
        description="Werkzeuge für Schichtplaner5-DBF-Datenbanken (lesen, prüfen, synchronisieren).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("info", help="Tabellenübersicht: Records je Tabelle, SP5-Build")
    p.add_argument("db_dir", help="Verzeichnis mit den 5*.DBF-Dateien")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("dump", help="Tabelle als JSON (Default) oder CSV ausgeben")
    p.add_argument("db_dir", help="Verzeichnis mit den 5*.DBF-Dateien")
    p.add_argument("table", metavar="TABELLE", help="Tabellenname, z. B. 5EMPL oder EMPL")
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="JSON-Ausgabe (Default)")
    fmt.add_argument("--csv", action="store_true", help="CSV-Ausgabe")
    p.add_argument("--limit", type=int, metavar="N", help="höchstens N Records ausgeben")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("validate", help="alle Tabellen lesen, Fehler/Encoding-Probleme melden")
    p.add_argument("db_dir", help="Verzeichnis mit den 5*.DBF-Dateien")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("sync", help="DBF nach SQLite/PostgreSQL synchronisieren (sp5lib.orm.sync)")
    p.add_argument("db_dir", help="Verzeichnis mit den 5*.DBF-Dateien")
    p.add_argument(
        "--target", required=True, metavar="sqlite:PATH|postgres:URL", help="Ziel-Datenbank"
    )
    p.set_defaults(func=cmd_sync)

    args = parser.parse_args(argv)
    if not os.path.isdir(args.db_dir):
        print(f"Fehler: kein Verzeichnis: {args.db_dir}", file=sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
