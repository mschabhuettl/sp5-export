"""Export one or more planning months from a Schichtplaner5 folder as a Dispofold dataset.

    python3 sp5_export.py --data <SP5 folder> --month YYYY-MM [--to YYYY-MM] --mapping mapping.json --out dataset.json

The SP5 folder is only read. Warnings and errors name SP5 ids, short names and counts, never names.
"""

import argparse
import csv
import json
import os
import sys
import traceback
from pathlib import Path

from sp5export.sp5 import Sp5Error, import_sp5, load_mapping
from sp5export.timeutil import parse_month


def _fail(text: str) -> int:
    print(f"Fehler: {text}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SP5-Ordner (nur lesend) als Datensatz für Dispofold "
                                                 "exportieren.")
    parser.add_argument("--data", required=True, help="SP5-Datenordner (enthält 5EMPL.DBF usw.)")
    parser.add_argument("--month", required=True, help="(erster) Planungsmonat YYYY-MM")
    parser.add_argument("--to", help="letzter Planungsmonat YYYY-MM, höchstens 12 Monate nach --month")
    parser.add_argument("--history-months", type=int, default=12, help="Monate Historie vor --month (0 bis 24)")
    parser.add_argument("--areas-from-history", type=int, metavar="N",
                        help="Bereiche ergänzen, in denen jemand in der Historie mindestens N Dienste hatte")
    parser.add_argument("--report", help="CSV mit SP5-Ids: gearbeitete und per Gruppe zugeordnete Bereiche")
    parser.add_argument("--mapping", required=True, help="Zuordnungsdatei (JSON)")
    parser.add_argument("--out", required=True, help="Ausgabedatei (JSON)")
    args = parser.parse_args(argv)

    for month in (args.month, args.to):
        try:
            if month is not None:
                parse_month(month)
        except ValueError:
            return _fail(f"ungültiger Monat {month!r}, erwartet YYYY-MM")
    data, out = Path(args.data).resolve(), Path(args.out).resolve()
    report = Path(args.report).resolve() if args.report else None
    for path in (out, report):
        if path is not None and (path == data or data in path.parents):
            return _fail("Ausgabe- und Berichtsdatei dürfen nicht im SP5-Ordner liegen")
    if report == out:
        return _fail("Ausgabe- und Berichtsdatei müssen verschieden sein")
    try:
        mapping = load_mapping(args.mapping)
    except (OSError, ValueError) as e:
        return _fail(f"Zuordnungsdatei {args.mapping} nicht lesbar ({type(e).__name__})")
    except Sp5Error as e:
        return _fail(f"Zuordnungsdatei {args.mapping}: {e}")

    try:
        rows: list = []
        dataset, warnings = import_sp5(data, args.month, mapping, args.to, args.history_months,
                                       args.areas_from_history, rows)
    except Sp5Error as e:
        return _fail(str(e))
    except Exception as e:
        # The message of an unexpected error may contain field values (names), so only the type and the
        # place are shown.
        frame = traceback.extract_tb(e.__traceback__)[-1]
        return _fail(f"unerwarteter Fehler {type(e).__name__} in {Path(frame.filename).name}:{frame.lineno}")

    def write_dataset(f):
        json.dump(dataset, f, ensure_ascii=False, indent=1)
        f.write("\n")

    def write_report(f):
        w = csv.writer(f, delimiter=";")
        w.writerow(["person", "bereich", "dienste_historie", "bereich_aus_gruppen"])
        w.writerows([pid, area, n, "ja" if held else "nein"] for pid, area, n, held in rows)

    # Both files are written next to their target first and only then moved into place, so that a failed
    # export leaves no new or half-written file.
    targets = [(out, args.out, "Ausgabedatei", write_dataset)]
    if report is not None:
        targets.append((report, args.report, "Berichtsdatei", write_report))
    written = []
    try:
        for path, name, kind, write in targets:
            tmp = path.with_name(path.name + ".tmp")
            try:
                with open(tmp, "w", encoding="utf-8", newline="") as f:
                    written.append(tmp)
                    write(f)
            except OSError as e:
                return _fail(f"{kind} {name} nicht schreibbar ({type(e).__name__})")
        for (path, *_), tmp in zip(targets, written):
            os.replace(tmp, path)
        written = []
    finally:
        for tmp in written:
            tmp.unlink(missing_ok=True)
    for w in warnings:
        print(f"Warnung: {w}", file=sys.stderr)
    print(f"{len(dataset['periods'])} Planungsmonate, {len(dataset['people'])} Mitarbeiter, "
          f"{len(dataset['shifts'])} Schichten, "
          f"{len(dataset['demand'])} Bedarfsregeln, {len(warnings)} Warnungen; geschrieben: {args.out}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
