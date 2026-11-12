"""Export one planning month from a Schichtplaner5 folder as a Dispofold dataset.

    python3 sp5_export.py --data <SP5 folder> --month YYYY-MM --mapping mapping.json --out dataset.json

The SP5 folder is only read. Warnings and errors name SP5 ids, short names and counts, never names.
"""

import argparse
import json
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
    parser.add_argument("--month", required=True, help="Planungsmonat YYYY-MM")
    parser.add_argument("--mapping", required=True, help="Zuordnungsdatei (JSON)")
    parser.add_argument("--out", required=True, help="Ausgabedatei (JSON)")
    args = parser.parse_args(argv)

    try:
        parse_month(args.month)
    except ValueError:
        return _fail(f"ungültiger Monat {args.month!r}, erwartet YYYY-MM")
    data, out = Path(args.data).resolve(), Path(args.out).resolve()
    if out == data or data in out.parents:
        return _fail("die Ausgabedatei darf nicht im SP5-Ordner liegen")
    try:
        mapping = load_mapping(args.mapping)
    except (OSError, ValueError) as e:
        return _fail(f"Zuordnungsdatei {args.mapping} nicht lesbar ({type(e).__name__})")
    except Sp5Error as e:
        return _fail(f"Zuordnungsdatei {args.mapping}: {e}")

    try:
        dataset, warnings = import_sp5(data, args.month, mapping)
    except Sp5Error as e:
        return _fail(str(e))
    except Exception as e:
        # The message of an unexpected error may contain field values (names), so only the type and the
        # place are shown.
        frame = traceback.extract_tb(e.__traceback__)[-1]
        return _fail(f"unerwarteter Fehler {type(e).__name__} in {Path(frame.filename).name}:{frame.lineno}")

    try:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(dataset, f, ensure_ascii=False, indent=1)
            f.write("\n")
    except OSError as e:
        return _fail(f"Ausgabedatei {args.out} nicht schreibbar ({type(e).__name__})")
    for w in warnings:
        print(f"Warnung: {w}", file=sys.stderr)
    print(f"{len(dataset['people'])} Mitarbeiter, {len(dataset['shifts'])} Schichten, "
          f"{len(dataset['demand'])} Bedarfsregeln, {len(warnings)} Warnungen; geschrieben: {args.out}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
