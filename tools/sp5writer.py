"""Write a synthetic dataset as an SP5 folder (DBF tables), for tests and demos only.

The folder looks like a small real installation: groups are teams and qualifications that do not
map 1:1 to areas, shifts carry SP5 short names, and there are people and shifts the importer must
drop (service line, administration, volunteers, hidden and former employees).
"""

import argparse
import json
import logging
import struct
import sys
from datetime import date, timedelta
from pathlib import Path

from sp5lib.dbf_writer import append_record, get_table_fields

from tools.generator import generate_with_mixes

C_NAME = ("C", 100)  # UTF-16: 49 characters
C_SHORT = ("C", 24)  # UTF-16: 11 characters

SPECS = {
    "5EMPL": [("ID", "N", 11), ("NAME", *C_NAME), ("FIRSTNAME", *C_NAME), ("SHORTNAME", *C_SHORT),
              ("NUMBER", "C", 20), ("POSITION", "N", 11), ("HIDE", "N", 1), ("WORKDAYS", "C", 16),
              ("CALCBASE", "N", 11), ("HRSDAY", "F", 10), ("HRSWEEK", "F", 10), ("HRSMONTH", "F", 10),
              ("EMPSTART", "D", 8), ("EMPEND", "D", 8)],
    "5GROUP": [("ID", "N", 11), ("NAME", *C_NAME), ("SHORTNAME", *C_SHORT), ("POSITION", "N", 11),
               ("HIDE", "N", 1), ("SUPERID", "N", 11)],
    "5GRASG": [("ID", "N", 11), ("EMPLOYEEID", "N", 11), ("GROUPID", "N", 11)],
    "5SHIFT": [("ID", "N", 11), ("NAME", *C_NAME), ("SHORTNAME", *C_SHORT), ("POSITION", "N", 11),
               ("HIDE", "N", 1), ("COLORBK", "N", 11)]
              + [(f"STARTEND{i}", "C", 36) for i in range(8)]
              + [(f"DURATION{i}", "F", 10) for i in range(8)],
    "5LEAVT": [("ID", "N", 11), ("NAME", *C_NAME), ("SHORTNAME", *C_SHORT), ("POSITION", "N", 11),
               ("HIDE", "N", 1), ("CHARGETYP", "N", 11), ("DEDUCTACT", "N", 1), ("DEDUCTOVT", "N", 1),
               ("COLORBK", "N", 11)],
    "5ABSEN": [("ID", "N", 11), ("EMPLOYEEID", "N", 11), ("DATE", "D", 8), ("LEAVETYPID", "N", 11),
               ("TYPE", "N", 11), ("INTERVAL", "N", 11)],
    "5RESTR": [("ID", "N", 11), ("EMPLOYEEID", "N", 11), ("WEEKDAY", "N", 5), ("SHIFTID", "N", 11),
               ("RESTRICT", "N", 5), ("RESERVED", "C", 20)],
    "5SHDEM": [("ID", "N", 11), ("GROUPID", "N", 11), ("WEEKDAY", "N", 5), ("SHIFTID", "N", 11),
               ("WORKPLACID", "N", 11), ("MIN", "N", 5), ("MAX", "N", 5)],
    "5SPDEM": [("ID", "N", 11), ("GROUPID", "N", 11), ("DATE", "D", 8), ("SHIFTID", "N", 11),
               ("WORKPLACID", "N", 11), ("MIN", "N", 5), ("MAX", "N", 5)],
    "5HOLID": [("ID", "N", 11), ("DATE", "D", 8), ("NAME", *C_NAME), ("INTERVAL", "N", 11)],
    "5MASHI": [("ID", "N", 11), ("EMPLOYEEID", "N", 11), ("SHIFTID", "N", 11), ("DATE", "D", 8),
               ("WORKPLACID", "N", 11), ("TYPE", "N", 11)],
    "5SPSHI": [("ID", "N", 11), ("EMPLOYEEID", "N", 11), ("SHIFTID", "N", 11), ("DATE", "D", 8),
               ("NAME", *C_NAME), ("SHORTNAME", *C_SHORT), ("STARTEND", "C", 36), ("DURATION", "F", 10),
               ("TYPE", "N", 11), ("WORKPLACID", "N", 11)],
    "5CYCLE": [("ID", "N", 11), ("NAME", *C_NAME), ("POSITION", "N", 11), ("SIZE", "N", 11), ("UNIT", "N", 11),
               ("HIDE", "N", 1)],
    "5CYENT": [("ID", "N", 11), ("CYCLEEID", "N", 11), ("INDEX", "N", 11), ("SHIFTID", "N", 11),
               ("WORKPLACID", "N", 11)],
    "5CYASS": [("ID", "N", 11), ("EMPLOYEEID", "N", 11), ("CYCLEID", "N", 11), ("START", "D", 8), ("END", "D", 8),
               ("ENTRANCE", "N", 11)],
    "5CYEXC": [("ID", "N", 11), ("EMPLOYEEID", "N", 11), ("CYCLEASSID", "N", 11), ("DATE", "D", 8)],
}

# Qualification groups per area; some AN people are in a second group of the same area.
AREA_GROUPS = {"AN": "Team Annahme", "DR": "Team Disposition", "TV": "Team Transport", "LF": "Lageführung",
               "TS": "Technik"}
GROUPS = list(AREA_GROUPS.values()) + ["Springer Annahme", "Schicht A", "Schicht B", "Schicht C", "Gesamt",
                                       "Servicetelefon", "Verwaltung", "Ehrenamt"]
SUBGROUPS = {"Technik Bereitschaft": "Technik"}  # subgroup -> parent (5GROUP.SUPERID)
# Out-of-scope shifts: short name, name, start, end
OTHER_SHIFTS = [("X1-T", "Servicetelefon Tag", "07:30", "19:30"), ("X1-N", "Servicetelefon Nacht", "19:30", "07:30"),
                ("X2", "Servicetelefon früh", "06:00", "14:00"), ("X3", "Servicetelefon spät", "12:00", "20:00"),
                ("X4", "Verwaltung", "07:30", "16:00")]
# Out-of-scope people: count, groups, shift short names they work
OTHER_PEOPLE = [(7, ["Servicetelefon"], ["X1-T", "X1-N", "X2", "X3"]),
                (4, ["Verwaltung"], ["X4"]),
                (5, ["Ehrenamt", "Team Transport"], ["V1"])]
WEEKDAY_INDEX = {"WD": [0, 1, 2, 3, 4], "WE": [5, 6], "HOL": [7]}


def _empty_table(path: Path, spec: list[tuple]) -> list[dict]:
    record_size = 1 + sum(f[2] for f in spec)
    header = bytearray(32)
    header[0] = 0x03
    header[1:4] = bytes([26, 1, 1])
    struct.pack_into("<IHH", header, 4, 0, 32 + 32 * len(spec) + 1, record_size)
    fields = b"".join(name.encode("ascii").ljust(11, b"\x00") + kind.encode("ascii") + b"\x00" * 4
                      + bytes([length, 0]) + b"\x00" * 14 for name, kind, length in spec)
    path.write_bytes(bytes(header) + fields + b"\x0d\x1a")
    return get_table_fields(str(path))


class _Folder:
    def __init__(self, folder: Path):
        self.folder = folder
        self.fields = {name: _empty_table(folder / f"{name}.DBF", spec) for name, spec in SPECS.items()}
        self.ids = dict.fromkeys(SPECS, 0)

    def add(self, table: str, **values) -> int:
        if "ID" not in values:
            self.ids[table] += 1
            values["ID"] = self.ids[table]
        append_record(str(self.folder / f"{table}.DBF"), self.fields[table], values)
        return values["ID"]


def _bgr(color: str) -> int:
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return (b << 16) | (g << 8) | r


def _hours(start: str, end: str) -> float:
    s = int(start[:2]) * 60 + int(start[3:])
    e = int(end[:2]) * 60 + int(end[3:])
    return round(((e - s) % 1440 or 1440) / 60, 2)


def write_sp5_folder(dataset: dict, mixes: dict, folder: str | Path, special_demand: list[dict] = ()) -> None:
    """Write the dataset to ``folder``. ``mixes`` is the per-shift staffing behind the demand (see
    generator.generate_with_mixes); ``special_demand`` adds 5SPDEM rows {date, shiftId, min, max}."""
    # The library warns about the missing change journal of every write; a fresh folder has none.
    logging.getLogger("sp5lib.dbf_writer").setLevel(logging.ERROR)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    db = _Folder(folder)
    period = dataset["periods"][0]
    first_day = date.fromisoformat(period["month"] + "-01")

    group_ids = {name: db.add("5GROUP", NAME=name, SHORTNAME=f"G{i + 1}", POSITION=i + 1, HIDE=0, SUPERID=0)
                 for i, name in enumerate(GROUPS)}
    for name, parent in SUBGROUPS.items():
        group_ids[name] = db.add("5GROUP", NAME=name, SHORTNAME=f"G{len(group_ids) + 1}", POSITION=len(group_ids) + 1,
                                 HIDE=0, SUPERID=group_ids[parent])
    shift_ids = {}
    for i, s in enumerate(dataset["shifts"]):
        times = {f"STARTEND{k}": f"{s['start']}-{s['end']}" for k in range(8)}
        durations = {f"DURATION{k}": _hours(s["start"], s["end"]) for k in range(8)}
        shift_ids[s["id"]] = db.add("5SHIFT", NAME=s["name"], SHORTNAME=s["code"], POSITION=i + 1, HIDE=0,
                                    COLORBK=_bgr(s["color"]), **times, **durations)
    other_shift_ids = {}
    for short, name, start, end in OTHER_SHIFTS:
        other_shift_ids[short] = db.add(
            "5SHIFT", NAME=name, SHORTNAME=short, POSITION=len(shift_ids) + len(other_shift_ids) + 1, HIDE=0,
            COLORBK=0xEEEEEE, **{f"STARTEND{k}": f"{start}-{end}" for k in range(8)},
            **{f"DURATION{k}": _hours(start, end) for k in range(8)})
    leave_ids = {t["code"]: db.add("5LEAVT", NAME=t["name"], SHORTNAME=t["code"], POSITION=i + 1, HIDE=0,
                                   CHARGETYP=1 if t["reducesTarget"] else 0, DEDUCTACT=0,
                                   DEDUCTOVT=1 if t["chargesAccount"] else 0, COLORBK=_bgr(t["color"]))
                 for i, t in enumerate(dataset["absenceTypes"])}

    def employee(eid: int, first: str, last: str, short: str, hours: float, groups: list[str], **extra) -> None:
        db.add("5EMPL", ID=eid, NAME=last, FIRSTNAME=first, SHORTNAME=short, NUMBER=str(eid), POSITION=eid,
               HIDE=extra.get("hide", 0), WORKDAYS="1 1 1 1 1 0 0 0", CALCBASE=1, HRSDAY=round(hours / 5, 2),
               HRSWEEK=hours, HRSMONTH=0.0, EMPSTART="2015-01-01", EMPEND=extra.get("end"))
        for g in groups:
            db.add("5GRASG", EMPLOYEEID=eid, GROUPID=group_ids[g])

    emp_ids = {}
    for i, p in enumerate(dataset["people"]):
        groups = []
        for a in p["areaIds"]:
            if a == "TS" and i % 2:
                groups.append("Technik Bereitschaft")  # the area comes from the parent group
                continue
            groups.append(AREA_GROUPS[a] if a != "AN" or i % 3 else "Springer Annahme")
            if a == "AN" and i % 5 == 0:
                groups.append("Springer Annahme")
        groups += [("Schicht A", "Schicht B", "Schicht C")[i % 3], "Gesamt"]
        emp_ids[p["id"]] = int(p["externalId"])
        employee(int(p["externalId"]), p["firstName"], p["lastName"], p["shortName"], p["weeklyHours"],
                 list(dict.fromkeys(groups)))
    # Out of scope: service line, administration, volunteers, one hidden and one former employee.
    fallback = shift_ids.get("TV-F", next(iter(shift_ids.values())))
    eid = 5001
    prev_days = [first_day - timedelta(days=k) for k in range(7, 0, -1)]
    for count, groups, shorts in OTHER_PEOPLE:
        for k in range(count):
            employee(eid, f"Vorname{eid}", f"Extern{eid}", f"X{eid}", 20.0, groups)
            for j, d in enumerate(prev_days):
                if (j + k) % 3:
                    sid = other_shift_ids.get(shorts[(j + k) % len(shorts)], fallback)
                    db.add("5MASHI", EMPLOYEEID=eid, SHIFTID=sid, DATE=d.isoformat(), WORKPLACID=0, TYPE=0)
            db.add("5RESTR", EMPLOYEEID=eid, WEEKDAY=2, SHIFTID=fallback, RESTRICT=2)
            eid += 1
    employee(eid, "Vorname", "Verborgen", "XVB", 40.0, ["Team Annahme"], hide=1)
    employee(eid + 1, "Vorname", "Ehemalig", "XEH", 40.0, ["Team Disposition"], end="2025-06-30")
    for short in ("X1-T", "X1-N", "X2", "X3"):
        for idx in range(8):
            db.add("5SHDEM", GROUPID=group_ids["Servicetelefon"], WEEKDAY=idx,
                   SHIFTID=other_shift_ids[short], WORKPLACID=0, MIN=1, MAX=0)

    for r in dataset["restrictions"]:
        grade = 2 if r["kind"] == "NEVER" else 1
        for idx in (range(8) if r["weekday"] == 0 else [r["weekday"] - 1]):
            db.add("5RESTR", EMPLOYEEID=emp_ids[r["personId"]], WEEKDAY=idx, SHIFTID=shift_ids[r["shiftId"]],
                   RESTRICT=grade, RESERVED="")
    first = dataset["people"][0]
    db.add("5RESTR", EMPLOYEEID=emp_ids[first["id"]], WEEKDAY=3, SHIFTID=shift_ids[dataset["shifts"][0]["id"]],
           RESTRICT=0, RESERVED="")  # grade "none": no restriction
    for a in period["absences"]:
        db.add("5ABSEN", EMPLOYEEID=emp_ids[a["personId"]], DATE=a["date"], LEAVETYPID=leave_ids[a["typeCode"]],
               TYPE=0, INTERVAL=0)
    for h in dataset["holidays"]:
        db.add("5HOLID", DATE=h["date"], NAME=h["name"], INTERVAL=0)
    busy = set()
    for d in period["previousDuties"]:
        busy.add((d["personId"], d["date"]))
        db.add("5MASHI", EMPLOYEEID=emp_ids[d["personId"]], SHIFTID=shift_ids[d["shiftId"]], DATE=d["date"],
               WORKPLACID=0, TYPE=0)
    # Target-plan entries (TYPE 1) are not duties; the importer must skip them.
    for p in dataset["people"][:5]:
        for d in prev_days:
            if (p["id"], d.isoformat()) not in busy:
                db.add("5MASHI", EMPLOYEEID=emp_ids[p["id"]], SHIFTID=fallback, DATE=d.isoformat(), WORKPLACID=0,
                       TYPE=1)
                break
    db.add("5HOLID", DATE=(first_day - timedelta(days=1)).isoformat(), NAME="Halber Feiertag", INTERVAL=1)

    area_of = {s["id"]: s["areaId"] for s in dataset["shifts"]}
    for area, area_mixes in mixes.items():
        for day_type, indices in WEEKDAY_INDEX.items():
            counts = area_mixes.get(day_type) or area_mixes["WD"]  # SP5 always has the holiday slot
            for sid, count in counts.items():
                group = AREA_GROUPS[area_of[sid]]
                # Split larger counts over two groups, as teams in SP5 often carry part of the demand.
                parts = [(group, count)] if count < 2 or area != "AN" else [(group, count // 2),
                                                                              ("Schicht A", count - count // 2)]
                for g, k in parts:
                    for idx in indices:
                        db.add("5SHDEM", GROUPID=group_ids[g], WEEKDAY=idx, SHIFTID=shift_ids[sid], WORKPLACID=0,
                               MIN=k, MAX=k if area == "LF" else 0)
    db.add("5SHDEM", GROUPID=group_ids["Ehrenamt"], WEEKDAY=5, SHIFTID=fallback, WORKPLACID=0, MIN=2, MAX=0)
    for sp in special_demand:
        group = AREA_GROUPS[area_of[sp["shiftId"]]]
        db.add("5SPDEM", GROUPID=group_ids[group], DATE=sp["date"], SHIFTID=shift_ids[sp["shiftId"]],
               WORKPLACID=0, MIN=sp["min"], MAX=sp.get("max", 0))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a synthetic SP5 folder (never use real data).")
    parser.add_argument("--month", required=True)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--people", type=int, default=150)
    parser.add_argument("--out", required=True, help="target folder (created)")
    args = parser.parse_args(argv)
    dataset, mixes = generate_with_mixes(args.month, args.seed, args.people)
    write_sp5_folder(dataset, mixes, args.out)
    print(json.dumps({"folder": args.out, "people": len(dataset["people"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
