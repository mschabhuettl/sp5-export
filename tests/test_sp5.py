import copy
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import date

import pytest
from exportcli import MAPPING, ROOT, export, run

from sp5export.feasibility import check_feasibility, rules_for_day
from sp5export.sp5 import Sp5Error, import_sp5, load_mapping
from sp5export.timeutil import interval, month_days
from tools.generator import generate_with_mixes
from tools.sp5writer import OTHER_SHIFTS, write_sp5_folder

MONTH = "2027-01"
SPECIAL_DAY = "2027-01-20"


@pytest.fixture(scope="module")
def source():
    dataset, mixes = generate_with_mixes(MONTH, 1)
    return dataset, mixes


@pytest.fixture(scope="module")
def folder(source, tmp_path_factory):
    dataset, mixes = source
    path = tmp_path_factory.mktemp("sp5") / "sp5"
    an_day_group = mixes["AN"]["WD"]["AN-T"] // 2
    write_sp5_folder(dataset, mixes, path,
                     special_demand=[{"date": SPECIAL_DAY, "shiftId": "AN-T", "min": an_day_group + 3}])
    return path


@pytest.fixture(scope="module")
def exported(folder, tmp_path_factory):
    return export(folder, MONTH, MAPPING, tmp_path_factory.mktemp("out") / "dataset.json")


@pytest.fixture(scope="module")
def imported(exported):
    return exported[0], exported[1]


def _names(dataset):
    names = {p["lastName"] for p in dataset["people"]} | {p["firstName"] for p in dataset["people"]}
    return names | {"Extern5001", "Verborgen", "Ehemalig", "Vorname"}


def _by_ext(dataset):
    return {p["id"]: p["externalId"] for p in dataset["people"]}


def _codes(dataset):
    return {s["id"]: s["code"] for s in dataset["shifts"]}


def _profile(dataset, area, day):
    holidays = {h["date"] for h in dataset["holidays"]}
    return sorted((*interval(r["from"], r["to"]), r["min"], r["max"]) for r in rules_for_day(dataset, area, day,
                                                                                                holidays))


def test_people_and_out_of_scope(source, imported):
    dataset, (result, warnings) = source[0], imported
    assert len(result["people"]) == 150
    original = {p["externalId"]: p for p in dataset["people"]}
    for p in result["people"]:
        o = original[p["externalId"]]
        assert (p["firstName"], p["lastName"], p["shortName"], p["weeklyHours"], p["areaIds"]) == \
            (o["firstName"], o["lastName"], o["shortName"], o["weeklyHours"], o["areaIds"])
    assert "16 Mitarbeiter nicht übernommen (ausgeschlossene Gruppe)" in warnings
    assert "1 Mitarbeiter nicht übernommen (ausgeblendet)" in warnings
    assert "1 Mitarbeiter nicht übernommen (nicht beschäftigt im Monat)" in warnings
    codes = {s["code"] for s in result["shifts"]}
    assert not codes & {s[0] for s in OTHER_SHIFTS}
    assert all(int(p["externalId"]) < 5000 for p in result["people"])


def test_warnings_never_contain_names(source, exported):
    _, warnings, proc = exported
    assert warnings
    text = "\n".join(warnings) + proc.stdout + proc.stderr
    assert not [n for n in _names(source[0]) if n in text]


def test_errors_never_contain_names(source, folder, tmp_path):
    broken = tmp_path / "sp5"
    shutil.copytree(folder, broken)
    (broken / "5GRASG.DBF").unlink()
    proc = run(broken, MONTH, MAPPING, tmp_path / "a.json")
    assert proc.returncode == 1 and "Tabelle 5GRASG fehlt" in proc.stderr
    # A mapping rule without "match" is reported before anything is read.
    mapping = copy.deepcopy(load_mapping(MAPPING))
    del mapping["groups"][0]["match"]
    proc2 = run(folder, MONTH, mapping, tmp_path / "b.json")
    assert proc2.returncode == 1 and "groups, Eintrag 1: 'match' fehlt" in proc2.stderr
    proc3 = run(folder, "2027-13", MAPPING, tmp_path / "c.json")
    assert proc3.returncode == 1 and "Monat" in proc3.stderr
    text = "".join(p.stdout + p.stderr for p in (proc, proc2, proc3))
    assert not [n for n in _names(source[0]) if n in text]
    assert not (tmp_path / "a.json").exists() and not (tmp_path / "b.json").exists()


def test_output_must_not_go_into_the_folder(folder):
    proc = run(folder, MONTH, MAPPING, folder / "dataset.json")
    assert proc.returncode == 1 and not (folder / "dataset.json").exists()


def test_cli_does_not_load_the_writer():
    code = "import sys, sp5_export; assert not [m for m in sys.modules if 'dbf_writer' in m or m.startswith('tools')]"
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)


def test_shifts(source, imported):
    original = {s["code"]: s for s in source[0]["shifts"]}
    result = {s["code"]: s for s in imported[0]["shifts"]}
    assert result.keys() == original.keys()
    for code, s in result.items():
        o = original[code]
        assert (s["areaId"], s["start"], s["end"], s["category"], s["unpopular"], s["color"].upper()) == \
            (o["areaId"], o["start"], o["end"], o["category"], o["unpopular"], o["color"].upper()), code


def test_restrictions_absences_previous_duties_holidays(source, imported):
    dataset, result = source[0], imported[0]
    src_codes, dst_codes = _codes(dataset), _codes(result)
    src_ext, dst_ext = _by_ext(dataset), _by_ext(result)

    def restr(ds, codes, ext):
        return {(ext[r["personId"]], codes[r["shiftId"]], r["weekday"], r["kind"]) for r in ds["restrictions"]}

    assert restr(result, dst_codes, dst_ext) == restr(dataset, src_codes, src_ext)
    assert len(result["restrictions"]) == len(dataset["restrictions"])

    def absences(ds, ext):
        return {(ext[a["personId"]], a["date"], a["typeCode"]) for a in ds["periods"][0]["absences"]}

    assert absences(result, dst_ext) == absences(dataset, src_ext)
    types = {t["code"]: t for t in result["absenceTypes"]}
    keys = ("reducesTarget", "chargesAccount", "compensatoryRest")
    for t in dataset["absenceTypes"]:
        assert [types[t["code"]][k] for k in keys] == [t[k] for k in keys], t["code"]

    def previous(ds, codes, ext):
        return {(ext[d["personId"]], d["date"], codes[d["shiftId"]]) for d in ds["periods"][0]["previousDuties"]}

    assert previous(result, dst_codes, dst_ext) == previous(dataset, src_codes, src_ext)
    assert result["holidays"] == dataset["holidays"]
    assert {h["month"] for h in result["history"]} == {"2026-12"}


def test_demand_coverage_matches_every_day(source, imported):
    dataset, mixes = source
    result = imported[0]
    for area in [a["id"] for a in dataset["areas"]]:
        for day in month_days(MONTH):
            if day.isoformat() == SPECIAL_DAY and area == "AN":
                continue
            assert _profile(result, area, day) == _profile(dataset, area, day), (area, day)
    # 5SPDEM adds three call takers from 07:00 to 19:00 on that day (it replaces the group's regular row)
    special = date.fromisoformat(SPECIAL_DAY)
    before = {(a, b): c for a, b, c, _ in _profile(dataset, "AN", special)}
    after = {(a, b): c for a, b, c, _ in _profile(result, "AN", special)}
    assert after[(7 * 60, 14 * 60)] == before[(7 * 60, 14 * 60)] + 3
    assert after[(14 * 60 + 30, 19 * 60)] == before[(14 * 60 + 30, 19 * 60)] + 3
    assert after[(6 * 60 + 30, 7 * 60)] == before[(6 * 60 + 30, 7 * 60)]
    assert all(r["date"] == SPECIAL_DAY for r in result["demand"] if r["date"])


def test_imported_month_is_coverable(imported):
    assert check_feasibility(imported[0], MONTH)["problems"] == []


def test_folder_is_not_modified(folder, tmp_path):
    def snapshot():
        return {f: (hashlib.sha256((folder / f).read_bytes()).hexdigest(), os.stat(folder / f).st_mtime_ns)
                for f in sorted(os.listdir(folder))}

    before = snapshot()
    modes = {f: (folder / f).stat().st_mode for f in before}
    try:
        for f in before:
            os.chmod(folder / f, stat.S_IRUSR)
        os.chmod(folder, stat.S_IRUSR | stat.S_IXUSR)
        export(folder, MONTH, MAPPING, tmp_path / "dataset.json")
    finally:
        os.chmod(folder, stat.S_IRWXU)
        for f, mode in modes.items():
            os.chmod(folder / f, mode)
    assert snapshot() == before


def test_unmapped_items_are_reported(folder, tmp_path):
    mapping = copy.deepcopy(load_mapping(MAPPING))
    mapping["shifts"] = [r for r in mapping["shifts"] if r["areaId"] != "TS"]
    mapping["ignoreGroups"] = []
    result, warnings, _ = export(folder, MONTH, mapping, tmp_path / "dataset.json")
    assert any("Kürzel 'S1'" in w and "Schicht ohne Bereich" in w for w in warnings)
    assert any(w.startswith("Gruppe ohne Zuordnung") for w in warnings)
    assert not [r for r in result["demand"] if r["areaId"] == "TS"]


def test_missing_folder(tmp_path):
    with pytest.raises(Sp5Error):
        import_sp5(tmp_path / "missing", MONTH, load_mapping(MAPPING))
    proc = run(tmp_path / "missing", MONTH, MAPPING, tmp_path / "dataset.json")
    assert proc.returncode == 1 and "SP5-Ordner nicht gefunden" in proc.stderr


def test_cycle_duties_count_as_previous_duties(source, folder, tmp_path):
    from sp5lib.dbf_reader import read_dbf
    from sp5lib.dbf_writer import append_record, get_table_fields

    copy_dir = tmp_path / "sp5"
    shutil.copytree(folder, copy_dir)
    dataset = source[0]
    busy = {(d["personId"], d["date"]) for d in dataset["periods"][0]["previousDuties"]}
    karenz = {a["personId"] for a in dataset["periods"][0]["absences"] if a["typeCode"] == "KA"}
    person = next(p for p in dataset["people"] if p["id"] not in karenz
                  and (p["id"], "2026-12-30") not in busy and "AN" in p["areaIds"])
    shift_id = next(r["ID"] for r in read_dbf(str(copy_dir / "5SHIFT.DBF")) if r["SHORTNAME"] == "A2")

    def add(table, **values):
        path = str(copy_dir / f"{table}.DBF")
        append_record(path, get_table_fields(path), values)

    add("5CYCLE", ID=1, NAME="Tagdienst", POSITION=1, SIZE=1, UNIT=0, HIDE=0)
    add("5CYENT", ID=1, CYCLEEID=1, INDEX=0, SHIFTID=shift_id, WORKPLACID=0)
    add("5CYASS", ID=1, EMPLOYEEID=int(person["externalId"]), CYCLEID=1, START="2026-12-30", END="2026-12-30",
        ENTRANCE=0)
    result, warnings, _ = export(copy_dir, MONTH, MAPPING, tmp_path / "dataset.json")
    pid = f"E{person['externalId']}"
    codes = _codes(result)
    added = [d for d in result["periods"][0]["previousDuties"] if d["personId"] == pid and d["date"] == "2026-12-30"]
    assert [codes[d["shiftId"]] for d in added] == ["A2"]
    assert "1 halbe Feiertage (5HOLID INTERVAL) nicht als Feiertag übernommen" in warnings


def test_deviation_employment_and_max_boundaries(source, folder, tmp_path):
    from sp5lib.dbf_reader import read_dbf
    from sp5lib.dbf_writer import append_record, get_table_fields

    copy_dir = tmp_path / "sp5"
    shutil.copytree(folder, copy_dir)

    def add(table, **values):
        path = str(copy_dir / f"{table}.DBF")
        append_record(path, get_table_fields(path), values)

    def update_empend(eid, value):
        from sp5lib.dbf_writer import find_all_records, update_record
        path = str(copy_dir / "5EMPL.DBF")
        fields = get_table_fields(path)
        idx, _ = find_all_records(path, fields, ID=eid)[0]
        update_record(path, fields, idx, {"EMPEND": value})

    shifts = {r["SHORTNAME"]: r["ID"] for r in read_dbf(str(copy_dir / "5SHIFT.DBF"))}
    groups = {r["NAME"]: r["ID"] for r in read_dbf(str(copy_dir / "5GROUP.DBF"))}
    duty = source[0]["periods"][0]["previousDuties"][0]
    eid = int(duty["personId"][1:]) + 1000  # P0001 -> externalId 1001
    assert _codes(source[0])[duty["shiftId"]] != "R1"
    add("5SPSHI", EMPLOYEEID=eid, SHIFTID=shifts["R1"], DATE=duty["date"], NAME="", SHORTNAME="",
        STARTEND="", DURATION=0.0, TYPE=0, WORKPLACID=0)
    update_empend(eid, "2027-01-20")
    times = {f"STARTEND{k}": "12:00-14:00" for k in range(8)}
    xid = add("5SHIFT", ID=900, NAME="Lageführung Mittag", SHORTNAME="LX", POSITION=900, HIDE=0, COLORBK=0, **times,
              **{f"DURATION{k}": 2.0 for k in range(8)}) or 900
    for idx in range(8):
        add("5SHDEM", GROUPID=groups["Lageführung"], WEEKDAY=idx, SHIFTID=xid, WORKPLACID=0, MIN=0, MAX=2)
    mapping = copy.deepcopy(load_mapping(MAPPING))
    mapping["shifts"].insert(0, {"match": "LX", "areaId": "LF"})
    result, warnings, _ = export(copy_dir, MONTH, mapping, tmp_path / "dataset.json")
    pid = f"E{eid}"
    codes = _codes(result)
    prev = [d for d in result["periods"][0]["previousDuties"] if d["personId"] == pid and d["date"] == duty["date"]]
    assert [codes[d["shiftId"]] for d in prev] == ["R1"]  # the deviation replaces the duty
    person = next(p for p in result["people"] if p["id"] == pid)
    assert (person["entryDate"], person["exitDate"]) == ("2015-01-01", "2027-01-20")
    assert not [a for a in result["periods"][0]["absences"] if a["personId"] == pid and a["date"] > "2027-01-20"]
    assert "AUS" not in {t["code"] for t in result["absenceTypes"]}
    assert result["periods"][0]["fixed"] == []
    assert "1 Mitarbeiter nur teilweise im Monat beschäftigt; Ein- und Austritt sind übernommen" in warnings
    lead = sorted((r["from"], r["to"], r["min"], r["max"]) for r in result["demand"]
                  if r["areaId"] == "LF" and r["days"] and "TUE" in r["days"])
    assert ("12:00", "14:00", 1, 3) in lead and ("07:00", "12:00", 1, 1) in lead


def test_cli_writes_the_dataset_of_the_reader(folder, exported):
    dataset, warnings = import_sp5(folder, MONTH, load_mapping(MAPPING))
    assert exported[0] == dataset and exported[1] == warnings


def _broken_copy(folder, tmp_path, table, change):
    broken = tmp_path / "sp5"
    shutil.copytree(folder, broken)
    change(broken / f"{table}.DBF")
    return broken


def _truncate(path):
    data = path.read_bytes()
    path.write_bytes(data[:len(data) * 2 // 3])


def _empty(path):
    data = bytearray(path.read_bytes())
    header_size = int.from_bytes(data[8:10], "little")
    data[4:8] = bytes(4)
    path.write_bytes(bytes(data[:header_size]) + b"\x1a")


def _unreadable(path):
    path.unlink()
    path.mkdir()


@pytest.mark.parametrize("table,change,message", [
    ("5EMPL", _unreadable, "Tabelle 5EMPL: nicht lesbar"),
    ("5EMPL", _truncate, "Tabelle 5EMPL: unvollständig: laut Dateikopf 168 Datensätze, vorhanden "),
    ("5EMPL", _empty, "Tabelle 5EMPL ist leer"),
    ("5SHIFT", _empty, "Tabelle 5SHIFT ist leer"),
    ("5ABSEN", _truncate, "Tabelle 5ABSEN: unvollständig"),
    ("5RESTR", _unreadable, "Tabelle 5RESTR: nicht lesbar"),
])
def test_damaged_tables_fail_loudly(folder, tmp_path, table, change, message):
    broken = _broken_copy(folder, tmp_path, table, change)
    proc = run(broken, MONTH, MAPPING, tmp_path / "dataset.json")
    assert proc.returncode == 1 and message in proc.stderr, proc.stderr
    assert "geschrieben" not in proc.stderr and not (tmp_path / "dataset.json").exists()


def test_export_without_people_or_shifts_fails(folder, tmp_path):
    mapping = copy.deepcopy(load_mapping(MAPPING))
    mapping["groups"] = []
    proc = run(folder, MONTH, mapping, tmp_path / "a.json")
    assert proc.returncode == 1 and "kein Mitarbeiter übernommen" in proc.stderr
    mapping = copy.deepcopy(load_mapping(MAPPING))
    mapping["shifts"] = []
    proc = run(folder, MONTH, mapping, tmp_path / "b.json")
    assert proc.returncode == 1 and "keine Schicht übernommen" in proc.stderr


def _mapping(change):
    mapping = copy.deepcopy(load_mapping(MAPPING))
    change(mapping)
    return mapping


@pytest.mark.parametrize("change,message", [
    (lambda m: m["shifts"][0].update(match="re:^(A|R"), "shifts, Eintrag 1: Muster 're:^(A|R' ist kein gültiger "
                                                          "regulärer Ausdruck"),
    (lambda m: m["ignoreGroups"].append("re:*x"), "ignoreGroups, Eintrag 3: Muster 're:*x' ist kein gültiger"),
    (lambda m: m.pop("areas"), "Pflichtschlüssel 'areas' fehlt"),
    (lambda m: m.pop("groups"), "Pflichtschlüssel 'groups' fehlt"),
    (lambda m: m.update(shifts={"match": "A*"}), "'shifts' muss eine Liste sein"),
    (lambda m: m.update(excludeGroups="Verwaltung*"), "'excludeGroups' muss eine Liste sein"),
    (lambda m: m.update(areas=[]), "'areas' ist leer"),
    (lambda m: m.update(ignoreShift=["X2"]), "unbekannter Schlüssel 'ignoreShift'"),
    (lambda m: m["areas"][0].pop("id"), "areas, Eintrag 1: 'id' fehlt oder ist kein Text"),
    (lambda m: m["areas"].append(dict(m["areas"][0])), "areas, Eintrag 6: Bereich 'AN' doppelt"),
    (lambda m: m["areas"][1].update(color=3), "areas, Eintrag 2: 'color' fehlt oder ist kein Text"),
    (lambda m: m["shifts"][1].update(areaId="XX"), "shifts, Eintrag 2: 'areaId' 'XX' ist kein Bereich"),
    (lambda m: m["shifts"][2].update(category="EVENING"), "shifts, Eintrag 3: 'category' muss EARLY"),
    (lambda m: m["shifts"][0].update(unpopular="ja"), "shifts, Eintrag 1: 'unpopular' muss true oder false"),
    (lambda m: m["shifts"][0].update(match=5), "shifts, Eintrag 1: Muster 5 ist kein Text"),
    (lambda m: m["groups"][0].update(areaIds="AN"), "groups, Eintrag 1: 'areaIds' muss eine nicht leere Liste"),
    (lambda m: m["groups"][0].update(areaIds=["AN", "XX"]), "groups, Eintrag 1: 'areaIds' enthält 'XX'"),
    (lambda m: m["groups"].__setitem__(0, "Team"), "groups, Eintrag 1: muss ein Objekt sein"),
    (lambda m: m["shifts"][0].update(areaId=["AN"]), "shifts, Eintrag 1: 'areaId' ['AN'] ist kein Bereich"),
    (lambda m: m["groups"][0].update(areaIds=[["AN"]]), "groups, Eintrag 1: 'areaIds' enthält ['AN']"),
])
def test_mapping_is_validated_up_front(tmp_path, change, message):
    # The folder does not exist: the mapping is checked before the folder is touched.
    proc = run(tmp_path / "missing", MONTH, _mapping(change), tmp_path / "dataset.json")
    assert proc.returncode == 1 and message in proc.stderr, proc.stderr
    assert "SP5-Ordner" not in proc.stderr and "unerwarteter Fehler" not in proc.stderr
    with pytest.raises(Sp5Error, match=re.escape(message)):
        import_sp5(tmp_path / "missing", MONTH, _mapping(change))


def test_mapping_reports_all_problems_at_once(tmp_path):
    def change(m):
        m["shifts"][0]["areaId"] = "XX"
        m["groups"][0]["match"] = "re:("
    proc = run(tmp_path / "missing", MONTH, _mapping(change), tmp_path / "dataset.json")
    assert "shifts, Eintrag 1" in proc.stderr and "groups, Eintrag 1" in proc.stderr


def test_mapping_that_is_not_an_object(tmp_path):
    path = tmp_path / "mapping.json"
    path.write_text("[]", encoding="utf-8")
    proc = run(tmp_path / "missing", MONTH, path, tmp_path / "dataset.json")
    assert proc.returncode == 1 and "muss ein JSON-Objekt sein" in proc.stderr


class _Sp5Copy:
    """A writable copy of the synthetic SP5 folder."""

    def __init__(self, folder, tmp_path):
        self.dir = tmp_path / "sp5"
        shutil.copytree(folder, self.dir)

    def rows(self, table):
        from sp5lib.dbf_reader import read_dbf
        return read_dbf(str(self.dir / f"{table}.DBF"))

    def add(self, table, **values):
        from sp5lib.dbf_writer import append_record, get_table_fields
        path = str(self.dir / f"{table}.DBF")
        append_record(path, get_table_fields(path), values)

    def update(self, table, row_id, **values):
        from sp5lib.dbf_writer import find_all_records, get_table_fields, update_record
        path = str(self.dir / f"{table}.DBF")
        fields = get_table_fields(path)
        idx, _ = find_all_records(path, fields, ID=row_id)[0]
        update_record(path, fields, idx, values)


def _duty_person(source, nth=0):
    """External id, date and shift id of the nth previous duty of the source dataset."""
    duty = source[0]["periods"][0]["previousDuties"][nth]
    return int(duty["personId"][1:]) + 1000, duty["date"], duty["shiftId"]


def test_people_and_shifts_dispofold_would_reject_are_not_exported(source, folder, tmp_path):
    sp5 = _Sp5Copy(folder, tmp_path)
    people = sorted(int(p["externalId"]) for p in source[0]["people"])
    sp5.update("5EMPL", people[0], HRSWEEK=0.0)
    sp5.update("5EMPL", people[1], EMPSTART="2027-01-15", EMPEND="2027-01-10")
    sp5.update("5EMPL", people[2], EMPSTART="2027-01-10")
    long_times = {"LX": "06:00-19:00", "LY": "07:00-07:00"}
    for i, (short, times) in enumerate(long_times.items()):
        sp5.add("5SHIFT", ID=901 + i, NAME="Lang", SHORTNAME=short, POSITION=901 + i, HIDE=0, COLORBK=0,
                **{f"STARTEND{k}": times for k in range(8)}, **{f"DURATION{k}": 13.0 for k in range(8)})
    mapping = copy.deepcopy(load_mapping(MAPPING))
    mapping["shifts"].insert(0, {"match": "L[XY]", "areaId": "LF"})
    result, warnings, _ = export(sp5.dir, MONTH, mapping, tmp_path / "dataset.json")
    ids = {p["externalId"] for p in result["people"]}
    assert str(people[0]) not in ids and str(people[1]) not in ids
    assert f"Mitarbeiter E{people[0]} hat keine Wochenstunden (HRSWEEK) und wird nicht übernommen" in warnings
    assert "1 Mitarbeiter nicht übernommen (ohne Wochenstunden)" in warnings
    assert "1 Mitarbeiter nicht übernommen (Austritt vor Eintritt)" in warnings
    entry = next(p for p in result["people"] if p["externalId"] == str(people[2]))
    assert (entry["entryDate"], entry["exitDate"]) == ("2027-01-10", None)
    absences = result["periods"][0]["absences"]
    assert not [a for a in absences if a["personId"] == entry["id"] and a["date"] < "2027-01-10"]
    assert not {"LX", "LY"} & {s["code"] for s in result["shifts"]}
    assert "Schicht 'LX' (SP5-Id 901) dauert mehr als 12 Stunden (06:00-19:00) und wird nicht übernommen" in warnings
    assert "Schicht 'LY' (SP5-Id 902) dauert mehr als 12 Stunden (07:00-07:00) und wird nicht übernommen" in warnings


def test_absence_switches_follow_the_sp5_leave_type(folder, tmp_path):
    sp5 = _Sp5Copy(folder, tmp_path)
    kinds = {"Q1": (1, 0, 0), "Q2": (1, 1, 0), "Q3": (1, 0, 1), "Q4": (0, 0, 1), "Q5": (0, 0, 0)}
    for i, (code, (charge, actual, overtime)) in enumerate(kinds.items()):
        sp5.add("5LEAVT", ID=801 + i, NAME=code, SHORTNAME=code, POSITION=801 + i, HIDE=0, CHARGETYP=charge,
                DEDUCTACT=actual, DEDUCTOVT=overtime, COLORBK=0)
    result, _, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "dataset.json")
    types = {t["code"]: (t["reducesTarget"], t["chargesAccount"]) for t in result["absenceTypes"]}
    assert [types[c] for c in kinds] == [(True, False), (True, True), (True, True), (False, False), (False, False)]


def test_full_day_absence_wins_over_a_duty(source, folder, tmp_path):
    sp5 = _Sp5Copy(folder, tmp_path)
    leave = {r["SHORTNAME"]: r["ID"] for r in sp5.rows("5LEAVT")}
    full, day_full, _ = _duty_person(source, 0)
    half, day_half, _ = next(d for d in (_duty_person(source, i) for i in range(1, 50)) if d[0] != full)
    sp5.add("5ABSEN", EMPLOYEEID=full, DATE=day_full, LEAVETYPID=leave["K"], TYPE=0, INTERVAL=0)
    sp5.add("5ABSEN", EMPLOYEEID=half, DATE=day_half, LEAVETYPID=leave["K"], TYPE=0, INTERVAL=1)
    result, warnings, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "dataset.json")
    previous = {(d["personId"], d["date"]) for d in result["periods"][0]["previousDuties"]}
    assert (f"E{full}", day_full) not in previous
    assert (f"E{half}", day_half) in previous  # a half-day absence leaves the duty
    assert any(w.startswith("1 Dienste 2026-12-25..2026-12-31 an Tagen mit ganztägiger Abwesenheit") for w in warnings)


def test_special_duties_of_every_type_replace_the_duty(source, folder, tmp_path):
    sp5 = _Sp5Copy(folder, tmp_path)
    shifts = {r["SHORTNAME"]: r["ID"] for r in sp5.rows("5SHIFT")}
    eid, day, planned = _duty_person(source, 0)
    other, other_day, _ = next(d for d in (_duty_person(source, i) for i in range(1, 50)) if d[0] != eid)
    assert _codes(source[0])[planned] != "R1"
    sp5.add("5SPSHI", EMPLOYEEID=eid, SHIFTID=shifts["R1"], DATE=day, NAME="", SHORTNAME="", STARTEND="",
            DURATION=8.0, TYPE=1, WORKPLACID=0)
    sp5.add("5SPSHI", EMPLOYEEID=other, SHIFTID=0, DATE=other_day, NAME="Schulung", SHORTNAME="SCH",
            STARTEND="08:00-12:00", DURATION=4.0, TYPE=0, WORKPLACID=0)
    result, warnings, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "dataset.json")
    codes = _codes(result)
    previous = {(d["personId"], d["date"]): codes[d["shiftId"]] for d in result["periods"][0]["previousDuties"]}
    assert previous[(f"E{eid}", day)] == "R1"
    assert (f"E{other}", other_day) in previous  # a special duty without a shift leaves the duty
    assert "1 Sonderdienste 2026-12-25..2026-12-31 (5SPSHI) mit TYPE ungleich 0 als Dienst übernommen" in warnings
    assert "1 Sonderdienste 2026-12-25..2026-12-31 (5SPSHI) ohne Schicht nicht übernommen" in warnings


def test_history_is_imported_actual_values(source, folder, tmp_path):
    sp5 = _Sp5Copy(folder, tmp_path)
    leave = {r["SHORTNAME"]: r["ID"] for r in sp5.rows("5LEAVT")}
    eid, day, _ = _duty_person(source, 0)
    busy = {d["date"] for d in source[0]["periods"][0]["previousDuties"] if d["personId"] == f"P{eid - 1000:04d}"}
    free = next(f"2026-12-{d}" for d in range(25, 32) if f"2026-12-{d}" not in busy)
    sp5.add("5ABSEN", EMPLOYEEID=eid, DATE=free, LEAVETYPID=leave["ER"], TYPE=0, INTERVAL=0)
    result, _, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "dataset.json")
    assert result["history"]
    for h in result["history"]:
        assert (h["source"], h["hoursAccountAfter"], h["compRestBalanceAfter"]) == ("import", None, None)
        assert h["compRestEarned"] <= h["holidays"]
    used = {h["personId"]: h["compRestUsed"] for h in result["history"]}
    assert used.pop(f"E{eid}") == 1 and set(used.values()) == {0}


def test_deviation_to_an_unexported_shift_replaces_the_duty(source, folder, tmp_path):
    sp5 = _Sp5Copy(folder, tmp_path)
    shifts = {r["SHORTNAME"]: r["ID"] for r in sp5.rows("5SHIFT")}
    eid, day, _ = _duty_person(source, 0)
    sp5.add("5SPSHI", EMPLOYEEID=eid, SHIFTID=shifts["X2"], DATE=day, NAME="", SHORTNAME="", STARTEND="",
            DURATION=8.0, TYPE=0, WORKPLACID=0)  # X2 is in ignoreShifts
    result, _, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "dataset.json")
    assert (f"E{eid}", day) not in {(d["personId"], d["date"]) for d in result["periods"][0]["previousDuties"]}


def test_holiday_duties_on_a_sunday_earn_no_compensatory_rest(source, folder, tmp_path):
    sp5 = _Sp5Copy(folder, tmp_path)
    shifts = {r["SHORTNAME"]: r["ID"] for r in sp5.rows("5SHIFT")}
    eid, _, _ = _duty_person(source, 0)
    # 2026-11-01 (a Sunday) and 2026-12-08 (a Tuesday) are public holidays.
    for day in ("2026-11-01", "2026-12-08"):
        sp5.add("5MASHI", EMPLOYEEID=eid, SHIFTID=shifts["R1"], DATE=day, WORKPLACID=0, TYPE=0)
    result, _, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "dataset.json")
    history = {h["month"]: h for h in result["history"] if h["personId"] == f"E{eid}"}
    assert (history["2026-11"]["holidays"], history["2026-11"]["compRestEarned"]) == (1, 0)
    assert history["2026-12"]["holidays"] >= 1
    assert history["2026-12"]["compRestEarned"] == history["2026-12"]["holidays"]


def test_month_with_only_compensatory_rest_gets_a_history_entry(source, folder, tmp_path):
    sp5 = _Sp5Copy(folder, tmp_path)
    leave = {r["SHORTNAME"]: r["ID"] for r in sp5.rows("5LEAVT")}
    eid, _, _ = _duty_person(source, 0)
    pid = f"E{eid}"
    result, _, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "before.json")
    assert "2026-11" not in {h["month"] for h in result["history"] if h["personId"] == pid}
    for day in ("2026-11-10", "2026-11-11"):
        sp5.add("5ABSEN", EMPLOYEEID=eid, DATE=day, LEAVETYPID=leave["ER"], TYPE=0, INTERVAL=0)
    result, _, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "after.json")
    entry = next(h for h in result["history"] if h["personId"] == pid and h["month"] == "2026-11")
    assert (entry["compRestUsed"], entry["hours"], entry["nights"], entry["compRestEarned"]) == (2, 0, 0, 0)
    assert len([h for h in result["history"] if h["month"] == "2026-11"]) == 1
