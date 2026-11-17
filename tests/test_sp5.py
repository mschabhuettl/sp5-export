import copy
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections import Counter
from datetime import date

import pytest
from exportcli import MAPPING, ROOT, export, run

from sp5export import corrections as corr
from sp5export.dbf import read_table
from sp5export.demand import median_up, percentile_up, start_day_windows
from sp5export.feasibility import capacity_hours, check_feasibility, rules_for_day
from sp5export.sp5 import Sp5Error, _shares, import_sp5, load_mapping
from sp5export.timeutil import DAY, interval, month_days
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
    # The SP5 demand: the synthetic folder has duties in the last week before the month only.
    return export(folder, MONTH, MAPPING, tmp_path_factory.mktemp("out") / "dataset.json", "--demand", "sp5")


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
    assert "Wochenstunden nach CALCBASE: 0 (Tag) 50, 1 (Woche) 50, 2 (Monat) 50, 3 (gesamt) 0; HRSWEEK weicht bei" \
        in " ".join(warnings)
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
    # 12 months of history, an entry for every month of employment; the duties lie in the last week only.
    months = [f"2026-{m:02d}" for m in range(1, 13)]
    assert sorted({h["month"] for h in result["history"]}) == months
    assert all(sum(h["month"] == m for h in result["history"]) == len(result["people"]) for m in months)
    assert {h["month"] for h in result["history"] if h["hours"]} == {"2026-12"}


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
    result, warnings, _ = export(copy_dir, MONTH, mapping, tmp_path / "dataset.json", "--demand", "sp5")
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
    dataset, warnings = import_sp5(folder, MONTH, load_mapping(MAPPING), demand="sp5")
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
    (lambda m: m.update(ignoreShiftsAsAbsence="ja"), "'ignoreShiftsAsAbsence' muss true oder false sein"),
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
    sp5.update("5EMPL", people[0], CALCBASE=1, HRSWEEK=0.0)
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
    assert f"Mitarbeiter E{people[0]} hat keine Wochenstunden (CALCBASE 1) und wird nicht übernommen" in warnings
    assert "1 Mitarbeiter nicht übernommen (ohne Wochenstunden)" in warnings
    assert "1 Mitarbeiter nicht übernommen (Austritt vor Eintritt)" in warnings
    entry = next(p for p in result["people"] if p["externalId"] == str(people[2]))
    assert (entry["entryDate"], entry["exitDate"]) == ("2027-01-10", None)
    absences = result["periods"][0]["absences"]
    assert not [a for a in absences if a["personId"] == entry["id"] and a["date"] < "2027-01-10"]
    assert not {"LX", "LY"} & {s["code"] for s in result["shifts"]}
    assert "Schicht 'LX' (SP5-Id 901) dauert mehr als 12 Stunden (06:00-19:00) und wird nicht übernommen" in warnings
    assert "Schicht 'LY' (SP5-Id 902) dauert mehr als 12 Stunden (07:00-07:00) und wird nicht übernommen" in warnings


def test_weekly_hours_follow_the_calcbase(source, folder, tmp_path):
    sp5 = _Sp5Copy(folder, tmp_path)
    p = sorted(int(x["externalId"]) for x in source[0]["people"])
    five, three = "1 1 1 1 1 0 0 0", "1 1 1 0 0 0 0 0"
    cases = {
        p[0]: (dict(CALCBASE=0, HRSDAY=4.0, WORKDAYS=five, HRSWEEK=40.0), 20.0),  # stale HRSWEEK is ignored
        p[1]: (dict(CALCBASE=0, HRSDAY=8.0, WORKDAYS=three, HRSWEEK=40.0), 24.0),  # part time, three long days
        p[2]: (dict(CALCBASE=0, HRSDAY=7.5, WORKDAYS="1111100", HRSWEEK=37.5), 37.5),  # compact mask
        p[3]: (dict(CALCBASE=1, HRSDAY=1.0, WORKDAYS=five, HRSWEEK=38.5), 38.5),
        p[4]: (dict(CALCBASE=2, HRSMONTH=130.0, HRSWEEK=40.0), 30.0),
        p[5]: (dict(CALCBASE=3, HRSTOTAL=1500.0, EMPSTART="2027-01-01", EMPEND="2027-12-30"), 28.85),
    }
    for eid, (values, _) in cases.items():
        sp5.update("5EMPL", eid, **values)
    sp5.update("5EMPL", p[6], CALCBASE=3, HRSTOTAL=1000.0, EMPEND=None)  # open employment
    sp5.update("5EMPL", p[7], CALCBASE=0, HRSDAY=8.0, WORKDAYS="0 0 0 0 0 0 0 1")  # holiday slot only: 0 h
    result, warnings, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "dataset.json")
    hours = {int(x["externalId"]): x["weeklyHours"] for x in result["people"]}
    assert {eid: hours.get(eid) for eid in cases} == {eid: want for eid, (_, want) in cases.items()}
    assert p[6] not in hours and p[7] not in hours
    assert (f"Mitarbeiter E{p[6]} hat Gesamtstunden (CALCBASE 3) ohne abgeschlossene Beschäftigung"
            " (EMPSTART bis EMPEND) und wird nicht übernommen") in warnings
    assert f"Mitarbeiter E{p[7]} hat keine Wochenstunden (CALCBASE 0) und wird nicht übernommen" in warnings
    assert "2 Mitarbeiter nicht übernommen (ohne Wochenstunden)" in warnings
    summary = [w for w in warnings if w.startswith("Wochenstunden nach CALCBASE")]
    assert len(summary) == 1 and re.match(
        r"Wochenstunden nach CALCBASE: 0 \(Tag\) \d+, 1 \(Woche\) \d+, 2 \(Monat\) \d+, 3 \(gesamt\) \d+; "
        r"HRSWEEK weicht bei \d+ davon ab$", summary[0])


def test_weekly_hours_report_names_who_differs_and_the_value_used(source, folder, tmp_path):
    sp5 = _Sp5Copy(folder, tmp_path)
    p = sorted(int(x["externalId"]) for x in source[0]["people"])
    five = "1 1 1 1 1 0 0 0"
    sp5.update("5EMPL", p[0], CALCBASE=0, HRSDAY=4.0, WORKDAYS=five, HRSWEEK=40.0)
    sp5.update("5EMPL", p[1], CALCBASE=2, HRSMONTH=130.0, HRSWEEK=40.0)
    sp5.update("5EMPL", p[2], CALCBASE=0, HRSDAY=7.5, WORKDAYS=five, HRSWEEK=37.5)  # same value: no row
    sp5.update("5EMPL", p[3], CALCBASE=1, HRSWEEK=38.5)  # HRSWEEK is the value: no row
    sp5.update("5EMPL", p[4], CALCBASE=0, HRSDAY=8.0, WORKDAYS="0 0 0 0 0 0 0 1", HRSWEEK=20.0)  # 0 h: not exported
    report = tmp_path / "bericht.csv"
    plain, warnings, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "plain.json")
    result, warnings, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "dataset.json", "--report", str(report))
    assert (tmp_path / "dataset.json").read_bytes() == (tmp_path / "plain.json").read_bytes()
    rows = {r[1]: r for r in (line.split(";") for line in report.read_text(encoding="utf-8").splitlines())
            if r[0] == "wochenstunden"}
    assert rows[f"E{p[0]}"] == ["wochenstunden", f"E{p[0]}", *[""] * 11, "20.0", *[""] * 6, "0", "40.0", "20.0",
                                *[""] * 6]
    assert rows[f"E{p[1]}"][13:] == ["30.0", *[""] * 6, "2", "40.0", "30.0", *[""] * 6]
    assert rows[f"E{p[4]}"][13:] == ["", *[""] * 6, "0", "20.0", "0.0", *[""] * 6]
    assert f"E{p[2]}" not in rows and f"E{p[3]}" not in rows
    hours = {p["id"]: p["weeklyHours"] for p in result["people"]}
    assert all(r[13] == str(hours[pid]) for pid, r in rows.items() if pid != f"E{p[4]}")
    summary = next(w for w in warnings if w.startswith("Wochenstunden nach CALCBASE"))
    assert summary.endswith(f"; HRSWEEK weicht bei {len(rows)} davon ab")


def test_unknown_calcbase_is_an_error(source, folder, tmp_path):
    sp5 = _Sp5Copy(folder, tmp_path)
    eid = min(int(x["externalId"]) for x in source[0]["people"])
    sp5.update("5EMPL", eid, CALCBASE=7)
    with pytest.raises(Sp5Error, match=f"Mitarbeiter E{eid}: unbekannte Berechnungsgrundlage CALCBASE 7"):
        import_sp5(sp5.dir, MONTH, load_mapping(MAPPING))


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
    sp5.update("5EMPL", eid, EMPSTART="2026-12-01")  # months before the entry get no entry of their own
    result, _, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "before.json")
    assert {h["month"] for h in result["history"] if h["personId"] == pid} == {"2026-12"}
    for day in ("2026-11-10", "2026-11-11"):
        sp5.add("5ABSEN", EMPLOYEEID=eid, DATE=day, LEAVETYPID=leave["ER"], TYPE=0, INTERVAL=0)
    result, _, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "after.json")
    entry = next(h for h in result["history"] if h["personId"] == pid and h["month"] == "2026-11")
    assert (entry["compRestUsed"], entry["hours"], entry["nights"], entry["compRestEarned"]) == (2, 0, 0, 0)


def test_every_month_of_employment_gets_a_history_entry(source, imported):
    result = imported[0]
    entries = Counter(h["personId"] for h in result["history"])
    # Employed since 2015 and without duties for most of the window: still one entry per month (0 hours).
    assert set(entries.values()) == {12}
    quiet = next(h for h in result["history"] if h["month"] == "2026-05")
    assert (quiet["hours"], quiet["nights"], quiet["compRestUsed"]) == (0, 0, 0)


# ── several months, history window, areas from history ─────────────


def _shift_of_area(result, area):
    return int(next(s["id"] for s in result["shifts"] if s["areaId"] == area)[1:])


def test_several_planning_months(source, folder, tmp_path):
    sp5 = _Sp5Copy(folder, tmp_path)
    late = max(int(p["externalId"]) for p in source[0]["people"])
    sp5.update("5EMPL", late, EMPSTART="2027-03-01")  # employed only in the last month of the range
    single, _, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "one.json", "--demand", "sp5")
    result, warnings, proc = export(sp5.dir, MONTH, MAPPING, tmp_path / "three.json", "--to", "2027-03", "--demand",
                                    "sp5")
    assert [p["month"] for p in result["periods"]] == ["2027-01", "2027-02", "2027-03"]
    first, *later = result["periods"]
    others = [d for d in first["previousDuties"] if d["personId"] != f"E{late}"]
    assert others == single["periods"][0]["previousDuties"] != []
    assert all(p["previousDuties"] == [] for p in later)  # Dispofold takes them from the plan of the month before
    assert first["absences"] == single["periods"][0]["absences"]
    assert all(a["date"][:7] == p["month"] for p in result["periods"] for a in p["absences"])
    assert str(late) in {p["externalId"] for p in result["people"]}
    assert str(late) not in {p["externalId"] for p in single["people"]}
    # Easter Monday 2027 is in the range: holidays and dated demand cover every month.
    assert "2027-03-29" in {h["date"] for h in result["holidays"]}
    holidays = {h["date"] for h in result["holidays"]}
    for area in {r["areaId"] for r in result["demand"] if r["days"] == ["HOL"]}:
        rules = rules_for_day(result, area, date(2027, 3, 29), holidays)
        assert rules and all(r["days"] == ["HOL"] for r in rules)
    assert [h for h in result["history"] if h["personId"] != f"E{late}"] == single["history"]
    assert "3 Planungsmonate" in proc.stderr


@pytest.mark.parametrize("options, message", [
    (("--to", "2026-12"), "Ende muss im selben Monat oder bis zu 12 Monate danach liegen"),
    (("--to", "2028-02"), "Ende muss im selben Monat oder bis zu 12 Monate danach liegen"),
    (("--to", "2027-13"), "ungültiger Monat"),
    (("--history-months", "25"), "erlaubt sind 0 bis 24"),
    (("--areas-from-history", "0"), "Mindestzahl ab 1"),
    (("--history-months", "0", "--areas-from-history", "2"), "eine Historie"),
    (("--shifts-from-history", "0"), "Mindestzahl ab 1"),
    (("--history-months", "0", "--shifts-from-history", "2"), "eine Historie"),
    (("--history-months", "0"), "Bedarf aus der Historie braucht eine Historie (--history-months ab 1)"),
    (("--history-months", "0", "--demand", "history"), "ohne Historie --demand sp5 angeben"),
])
def test_bad_range_options(folder, tmp_path, options, message):
    proc = run(folder, MONTH, MAPPING, tmp_path / "dataset.json", *options)
    assert proc.returncode == 1 and message in proc.stderr
    assert not (tmp_path / "dataset.json").exists()


def test_history_length_is_configurable(folder, imported, tmp_path):
    none, _, _ = export(folder, MONTH, MAPPING, tmp_path / "none.json", "--history-months", "0", "--demand", "sp5")
    assert none["history"] == []
    long, _, _ = export(folder, MONTH, MAPPING, tmp_path / "long.json", "--history-months", "24")
    assert min(h["month"] for h in long["history"]) == "2025-01"
    # The default 12 months cover the 11 months before the planning month that Dispofold's 52-week average
    # needs to be a hard rule (RosterBuilder.average): every person has an entry in each of them.
    needed = {f"2026-{m:02d}" for m in range(2, 13)}
    result = imported[0]
    for p in result["people"]:
        assert needed <= {h["month"] for h in result["history"] if h["personId"] == p["id"]}


def test_areas_from_history(source, folder, imported, tmp_path):
    result = imported[0]
    sp5 = _Sp5Copy(folder, tmp_path)
    person = next(p for p in result["people"] if p["areaIds"] == ["AN"])
    other = next(a["id"] for a in result["areas"] if a["id"] != "AN")
    shift = _shift_of_area(result, other)
    for day in ("2026-10-05", "2026-10-06", "2026-10-07"):
        sp5.add("5MASHI", EMPLOYEEID=int(person["externalId"]), SHIFTID=shift, DATE=day, WORKPLACID=0, TYPE=0)
    # Someone in no mapped group who works shifts of an area.
    sp5.add("5EMPL", ID=9001, NAME="Ohne", FIRSTNAME="Gruppe", SHORTNAME="XOG", NUMBER="9001", POSITION=9001,
            HIDE=0, WORKDAYS="1 1 1 1 1 0 0 0", CALCBASE=1, HRSDAY=4.0, HRSWEEK=20.0, HRSMONTH=0.0, HRSTOTAL=0.0,
            EMPSTART="2015-01-01")
    for day in ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"):
        sp5.add("5MASHI", EMPLOYEEID=9001, SHIFTID=shift, DATE=day, WORKPLACID=0, TYPE=0)

    plain, warnings, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "plain.json")
    assert next(p for p in plain["people"] if p["id"] == person["id"])["areaIds"] == ["AN"]
    assert "9001" not in {p["externalId"] for p in plain["people"]}
    report = [w for w in warnings if w.startswith(f"Bereich {other}: ")]
    assert len(report) == 1 and report[0].startswith(f"Bereich {other}: 1 Mitarbeiter mit Diensten in den 12 Monaten")
    assert "1 Mitarbeiter ohne Bereich aus Gruppen haben in den 12 Monaten davor zugeordnete Schichten gearbeitet" \
        in warnings

    three, warnings, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "three.json", "--areas-from-history", "3")
    people = {p["id"]: p for p in three["people"]}
    assert set(people[person["id"]]["areaIds"]) == {"AN", other}
    assert people["E9001"]["areaIds"] == [other]
    assert "Bereiche aus Diensten (mindestens 3): 2 Bereiche ergänzt, davon für 1 Mitarbeiter ohne Bereich aus " \
        "Gruppen, die damit übernommen werden" in warnings

    five, _, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "five.json", "--areas-from-history", "5")
    people = {p["id"]: p for p in five["people"]}
    assert people[person["id"]]["areaIds"] == ["AN"] and "E9001" not in people


def test_report_names_ids_only(source, folder, imported, tmp_path):
    report = tmp_path / "bericht.csv"
    _, _, proc = export(folder, MONTH, MAPPING, tmp_path / "dataset.json", "--report", str(report))
    lines = report.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "pruefung;person;bereich;schicht;dienste_historie;bereich_aus_gruppen;einschraenkung;" \
        "tagestyp;spitze_historie;spitze_sp5;dienste_nicht_uebernommen;stunden_historie;spitze_historie_max;" \
        "wochenstunden;bereiche;mix_kategorien;mix_bereiche;auf_anfrage;grund;sp5_schicht;calcbase;hrsweek;" \
        "wochenstunden_calcbase;feld;status;wert_export;wert_korrektur;dienste_nicht_uebernommen_gezaehlt;letzter_dienst"
    rows = [line.split(";") for line in lines[1:]]
    assert {r[0] for r in rows} == {"bedarf", "bereich", "person", "schicht", "verworfen", "wochenstunden"}
    assert all(len(r) == 29 for r in rows)
    assert all(r[4].isdigit() for r in rows if r[0] != "wochenstunden")
    assert all(r[13:] == [""] * 16 for r in rows if r[0] not in ("person", "verworfen", "wochenstunden", "bedarf"))
    assert all(r[13:27] == [""] * 14 and r[27].isdigit() for r in rows if r[0] == "bedarf")
    assert all(r[18:] == [""] * 11 for r in rows if r[0] == "person")
    # Weekly hours: one row per person counted in the summary line, the value written is the CALCBASE value.
    hours = {p["id"]: p["weeklyHours"] for p in imported[0]["people"]}
    weekly = [r for r in rows if r[0] == "wochenstunden"]
    assert weekly and all(r[2:13] == [""] * 11 and r[14:20] == [""] * 6 and r[13] == r[22] == str(hours[r[1]])
                          and r[20] in ("0", "2") and r[21] != r[22] for r in weekly)
    stale = next(w for w in imported[1] if w.startswith("Wochenstunden nach CALCBASE"))
    assert stale.endswith(f"HRSWEEK weicht bei {len(weekly)} davon ab")
    dropped = [r for r in rows if r[0] == "verworfen"]
    assert dropped and all(r[1] == r[2] == "" and r[18].startswith("mitarbeiter_") for r in dropped)
    rows = [r for r in rows if r[0] not in ("verworfen", "wochenstunden")]
    assert all(re.match(r"E\d+$", r[1]) and r[7:13] == ["", "", "", "", "", ""] for r in rows if r[0] != "bedarf")
    assert all(r[3] == "" and r[5] in ("ja", "nein") and r[6] == "" for r in rows if r[0] == "bereich")
    demand = [r for r in rows if r[0] == "bedarf"]
    assert [(r[2], r[7]) for r in demand] == [(a["id"], c) for a in sorted(imported[0]["areas"], key=lambda a: a["id"])
                                              for c in ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN", "HOL")]
    assert all(r[1] == r[3] == r[5] == r[6] == "" and r[8].isdigit() and r[9].isdigit() and r[10].isdigit()
               and r[11] == "" and r[12].isdigit() for r in demand)
    codes = {s["code"] for s in imported[0]["shifts"]}
    assert all(r[3] in codes and r[5] == "" and r[6] in ("", "sp5", "zu_kurze_historie")
               for r in rows if r[0] == "schicht")
    text = report.read_text(encoding="utf-8")
    assert not any(name in text for name in _names(source[0]))
    held = {(r[1], r[2]) for r in rows if r[0] == "bereich" and r[5] == "ja"}
    assert held == {(p["id"], a) for p in imported[0]["people"] for a in p["areaIds"]}
    inside = run(folder, MONTH, MAPPING, tmp_path / "x.json", "--report", str(folder / "bericht.csv"))
    assert inside.returncode == 1 and "nicht im SP5-Ordner" in inside.stderr
    same = run(folder, MONTH, MAPPING, tmp_path / "y.json", "--report", str(tmp_path / "y.json"))
    assert same.returncode == 1 and "müssen verschieden sein" in same.stderr
    broken = run(folder, MONTH, MAPPING, tmp_path / "z.json", "--report", str(tmp_path / "missing" / "b.csv"))
    assert broken.returncode == 1 and "Berichtsdatei" in broken.stderr
    assert not (tmp_path / "x.json").exists() and not (tmp_path / "y.json").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bericht.csv", "dataset.json"]


def test_holiday_before_the_first_month_counts_without_history(folder, imported, tmp_path):
    none, _, _ = export(folder, MONTH, MAPPING, tmp_path / "none.json", "--history-months", "0", "--demand", "sp5")
    assert "2026-12-25" in {h["date"] for h in none["holidays"]}
    assert none["demand"] == imported[0]["demand"]


def test_shifts_from_history(source, folder, imported, tmp_path):
    result = imported[0]
    sp5 = _Sp5Copy(folder, tmp_path)
    person = next(p for p in result["people"] if p["areaIds"] == ["AN"])
    group = next(r["GROUPID"] for r in read_table(folder / "5GRASG.DBF")
                 if r["EMPLOYEEID"] == int(person["externalId"]))
    shifts = {s["category"]: s for s in result["shifts"] if s["areaId"] == "AN"}
    day, night = shifts["DAY"], shifts["NIGHT"]
    days = ("2026-11-02", "2026-11-03", "2026-11-04", "2026-12-07", "2026-12-08", "2026-12-09")
    # Only day duties: 9101 long employed, 9102 also with an SP5 restriction on the night shift, 9103 a new entrant.
    for eid, start in ((9101, "2015-01-01"), (9102, "2015-01-01"), (9103, "2026-10-15")):
        sp5.add("5EMPL", ID=eid, NAME="Nur", FIRSTNAME="Tag", SHORTNAME=f"T{eid}", NUMBER=str(eid), POSITION=eid,
                HIDE=0, WORKDAYS="1 1 1 1 1 0 0 0", CALCBASE=1, HRSDAY=8.0, HRSWEEK=40.0, HRSMONTH=0.0,
                HRSTOTAL=0.0, EMPSTART=start)
        sp5.add("5GRASG", EMPLOYEEID=eid, GROUPID=group)
        for d in days:
            sp5.add("5MASHI", EMPLOYEEID=eid, SHIFTID=int(day["id"][1:]), DATE=d, WORKPLACID=0, TYPE=0)
    sp5.add("5RESTR", EMPLOYEEID=9102, WEEKDAY=3, SHIFTID=int(night["id"][1:]), RESTRICT=2)

    def restrictions(dataset, pid):
        return [(r["shiftId"], r["weekday"], r["kind"]) for r in dataset["restrictions"] if r["personId"] == pid]

    plain, warnings, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "plain.json")
    assert restrictions(plain, "E9101") == [] and restrictions(plain, "E9103") == []
    lines = [w for w in warnings if w.startswith("Schichten im Bereich AN: ")]
    assert len(lines) == 1 and "mit keinem Dienst in mindestens einer Schicht" in lines[0]

    report = tmp_path / "bericht.csv"
    three, warnings, _ = export(sp5.dir, MONTH, MAPPING, tmp_path / "three.json", "--shifts-from-history", "3",
                                "--report", str(report))
    assert (night["id"], 0, "ON_REQUEST") in restrictions(three, "E9101")
    assert {s for s, *_ in restrictions(three, "E9101")} == {s["id"] for s in shifts.values()} - {day["id"]}
    assert [r for r in restrictions(three, "E9102") if r[0] == night["id"]] == [(night["id"], 4, "NEVER")]
    assert restrictions(three, "E9103") == []
    added = [r for r in three["restrictions"] if r not in plain["restrictions"]]
    assert added and all((r["weekday"], r["kind"]) == (0, "ON_REQUEST") for r in added)
    assert [r for r in plain["restrictions"] if r not in three["restrictions"]] == []
    assert f"Schichten aus Diensten (mindestens 3): {len(added)} Einschränkungen 'nur auf Anfrage' ergänzt" \
        in warnings
    assert any(w.startswith("Schichten aus Diensten: ") and "Eintritt nach 2026-10-01" in w for w in warnings)

    rows = {(r[1], r[3]): r for r in (line.split(";") for line in report.read_text(encoding="utf-8").splitlines())
            if r[0] == "schicht"}
    assert rows[("E9101", night["code"])] == ["schicht", "E9101", "AN", night["code"], "0", "", "auf_anfrage", "", "",
                                              "", "", "", ""] + [""] * 16
    assert rows[("E9101", day["code"])][4:] == ["6", "", "", "", "", "", "", "", ""] + [""] * 16
    assert rows[("E9102", night["code"])][6] == "sp5"
    assert rows[("E9103", night["code"])][6] == "zu_kurze_historie"
    text = report.read_text(encoding="utf-8")
    assert not any(name in text for name in ("Nur", "Tag", *_names(source[0])))


# ── demand from the history ───────────────────────────────────────

DECEMBER = [date(2026, 12, d) for d in range(1, 32)]
HOLIDAYS = {date(2026, 12, 8), date(2026, 12, 25), date(2026, 12, 26)}  # full holidays of the synthetic folder
CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
HX_SHIFTS = {"H1": ("07:00", "19:00"), "H2": ("07:00", "15:00"), "H3": ("19:00", "07:00"), "H4": ("22:00", "06:00"),
             "H5": ("12:00", "00:00"), "H6": ("06:00", "14:00")}


def _day_code(d):
    return "HOL" if d in HOLIDAYS else CODES[d.weekday()]


class _HistoryArea(_Sp5Copy):
    """The synthetic folder plus an area HX (shifts H1..H6, group 'Team Historie', people 9201..9209) whose
    only duties are the ones a test adds. Demand from one month of history (December 2026)."""

    def __init__(self, folder, tmp_path):
        super().__init__(folder, tmp_path)
        self.out = tmp_path
        self.shift = {}
        for i, (code, (start, end)) in enumerate(HX_SHIFTS.items()):
            self.shift[code] = 950 + i
            self.add("5SHIFT", ID=950 + i, NAME=f"Historie {code}", SHORTNAME=code, POSITION=950 + i, HIDE=0,
                     COLORBK=0, **{f"STARTEND{k}": f"{start}-{end}" for k in range(8)},
                     **{f"DURATION{k}": 8.0 for k in range(8)})
        self.add("5GROUP", ID=950, NAME="Team Historie", SHORTNAME="GH", POSITION=950, HIDE=0, SUPERID=0)
        for eid in range(9201, 9210):
            self.add("5EMPL", ID=eid, NAME="Historie", FIRSTNAME="Person", SHORTNAME=f"H{eid}", NUMBER=str(eid),
                     POSITION=eid, HIDE=0, WORKDAYS="1 1 1 1 1 0 0 0", CALCBASE=1, HRSDAY=8.0, HRSWEEK=40.0,
                     HRSMONTH=0.0, HRSTOTAL=0.0, EMPSTART="2015-01-01")
            self.add("5GRASG", EMPLOYEEID=eid, GROUPID=950)
        self.mapping = copy.deepcopy(load_mapping(MAPPING))
        self.mapping["areas"].append({"id": "HX", "name": "Historie", "color": "#888888"})
        self.mapping["shifts"].insert(0, {"match": "H[1-6]", "areaId": "HX"})
        self.mapping["groups"].append({"match": "Team Historie", "areaIds": ["HX"]})

    def duty(self, eid, code, days):
        for d in days:
            self.add("5MASHI", EMPLOYEEID=eid, SHIFTID=self.shift[code], DATE=d.isoformat(), WORKPLACID=0, TYPE=0)

    def export(self, name, *options):
        return export(self.dir, MONTH, self.mapping, self.out / name, "--history-months", "1", *options)


def _hx_rules(dataset):
    return sorted((tuple(r["days"]), r["from"], r["to"], r["min"], r["max"]) for r in dataset["demand"]
                  if r["areaId"] == "HX")


def _on_duty(dataset, area, code, minute):
    """People the rules of a day type ask for at `minute` after that day's midnight (0 .. 48 h), counting only
    windows of that day type: a window belongs to the day it starts, and overlapping windows add up."""
    total = 0
    for r in dataset["demand"]:
        if r["areaId"] == area and not r["date"] and code in r["days"]:
            s, e = interval(r["from"], r["to"])
            total += r["min"] if s <= minute < e else 0
    return total


def test_demand_from_history_is_the_known_staffing(folder, tmp_path):
    hx = _HistoryArea(folder, tmp_path)
    workdays = [d for d in DECEMBER if d.weekday() < 5 and d not in HOLIDAYS]
    hx.duty(9201, "H1", DECEMBER)  # 07:00-19:00 every day
    hx.duty(9202, "H3", DECEMBER)  # 19:00-07:00 every day, crosses midnight
    hx.duty(9203, "H4", [d for d in DECEMBER if d not in HOLIDAYS])  # a second night 22:00-06:00
    hx.duty(9204, "H2", workdays)  # 07:00-15:00 Monday to Friday
    hx.duty(9205, "H1", sorted(HOLIDAYS))  # on holidays only
    # Someone in an excluded group does not count (people not exported otherwise do: see
    # test_history_demand_counts_people_not_exported).
    hx.add("5GRASG", EMPLOYEEID=9206, GROUPID=next(g["ID"] for g in hx.rows("5GROUP") if g["NAME"] == "Ehrenamt"))
    hx.duty(9206, "H1", DECEMBER)
    report = tmp_path / "bericht.csv"
    result, warnings, _ = hx.export("dataset.json", "--report", str(report))

    assert _hx_rules(result) == [
        (("HOL",), "07:00", "19:00", 2, 2), (("HOL",), "19:00", "07:00", 1, 1),
        (("MON", "TUE", "WED", "THU", "FRI"), "07:00", "15:00", 2, 2),
        (("MON", "TUE", "WED", "THU", "FRI"), "15:00", "07:00", 1, 1),
        (("MON", "TUE", "WED", "THU", "FRI"), "22:00", "06:00", 1, 1),
        (("SAT", "SUN"), "07:00", "07:00", 1, 1), (("SAT", "SUN"), "22:00", "06:00", 1, 1),
    ]
    # The rules give back the staffing of a day hour by hour, the night until the next morning.
    staffing = {
        "MON": {6: 0, 7: 2, 14: 2, 15: 1, 21: 1, 22: 2, 29: 2, 30: 1, 31: 0},
        "SAT": {6: 0, 7: 1, 18: 1, 21: 1, 22: 2, 29: 2, 30: 1, 31: 0},
        "HOL": {6: 0, 7: 2, 18: 2, 19: 1, 22: 1, 30: 1, 31: 0},
    }
    for code, hours in staffing.items():
        assert {h: _on_duty(result, "HX", code, h * 60) for h in hours} == hours, code
    assert all(r["date"] is None for r in result["demand"])

    left_out = 31
    counted = 31 + 31 + 28 + len(workdays) + 3
    line = next(w for w in warnings if w.startswith("Bedarf im Bereich HX, "))
    assert line == ("Bedarf im Bereich HX, Spitze je Tagestyp aus der Historie/aus SP5: MON 2-2/0, TUE 2-2/0, "
                    "WED 2-2/0, THU 2-2/0, FRI 2-2/0, SAT 2-2/0, SUN 2-2/0, HOL 2-2/0; "
                    f"{counted} Dienste in den 1 Monaten davor gezählt, davon 0 von Mitarbeitern, die nicht "
                    f"übernommen werden; {left_out} nicht gezählt (ausgeschlossene Gruppe)")
    assert "Bedarf aus der Historie: Median je Bereich und Tagestyp über die 1 Monate davor" in warnings
    rows = [line.split(";") for line in report.read_text(encoding="utf-8").splitlines()[1:]]
    demand = {r[7]: r for r in rows if r[0] == "bedarf" and r[2] == "HX"}
    assert list(demand) == [*CODES, "HOL"]
    assert demand["HOL"] == ["bedarf", "", "HX", "", "9", "", "", "HOL", "2", "0", "3", "", "2"] + [""] * 14 + ["0", ""]
    assert sum(int(r[4]) for r in demand.values()) == counted
    assert sum(int(r[10]) for r in demand.values()) == left_out
    # Duties on a holiday count for HOL only, not for their weekday (2026-12-08 is a Tuesday).
    assert int(demand["TUE"][10]) == 4  # the 4 Tuesdays of the excluded person


def test_shifts_without_area_are_reported_with_duties_and_hours(folder, tmp_path):
    hx = _HistoryArea(folder, tmp_path)
    hx.mapping["shifts"][0]["match"] = "H[1-4]"  # H5 and H6 get no area
    hx.mapping["ignoreShifts"].append("H6")
    hx.duty(9201, "H1", DECEMBER)
    hx.duty(9202, "H1", DECEMBER[10:11])  # a mapped duty is not reported
    hx.add("5GRASG", EMPLOYEEID=9206, GROUPID=next(g["ID"] for g in hx.rows("5GROUP") if g["NAME"] == "Ehrenamt"))
    before, _, _ = hx.export("before.json")
    hx.duty(9203, "H5", DECEMBER[:3])  # unmapped, 12 hours each
    hx.duty(9202, "H6", DECEMBER[:10])  # ignored, 8 hours each
    hx.duty(9206, "H5", DECEMBER[:4])  # not exported: not counted
    hx.duty(9203, "H6", [date(2026, 11, 30)])  # before the history window: not counted
    report = tmp_path / "bericht.csv"
    result, warnings, proc = hx.export("after.json", "--report", str(report))

    lines = [w for w in warnings if w.startswith(("Schicht 'H", "Schichten ohne Bereich"))]
    assert lines == [
        "Schicht 'H6' ignoriert: 10 Dienste, 80.0 Stunden in den 1 Monaten davor",
        "Schicht 'H5' nicht zugeordnet: 3 Dienste, 36.0 Stunden in den 1 Monaten davor",
        "Schichten ohne Bereich: 2 Schichten, 13 Dienste, 116.0 Stunden in den 1 Monaten davor"]
    rows = [line.split(";") for line in report.read_text(encoding="utf-8").splitlines()]
    assert [r for r in rows if r[0] in ("ignoriert", "nicht_zugeordnet")] == [
        ["ignoriert", "", "", "H6", "10", "", "", "", "", "", "", "80.0", ""] + [""] * 16,
        ["nicht_zugeordnet", "", "", "H5", "3", "", "", "", "", "", "", "36.0", ""] + [""] * 16]
    assert all(len(r) == 29 for r in rows) and rows[0][11:13] == ["stunden_historie", "spitze_historie_max"]
    assert result == before  # the dataset does not know these duties

    none, warnings, _ = hx.export("none.json", "--history-months", "0", "--demand", "sp5")
    assert not [w for w in warnings if w.startswith(("Schicht 'H", "Schichten ohne Bereich"))]


def test_dropped_history_duties_are_counted_by_reason(folder, tmp_path):
    hx = _HistoryArea(folder, tmp_path)
    hx.mapping["shifts"][0]["match"] = "H[1-4]"  # H5 not mapped
    hx.mapping["ignoreShifts"].append("H6")
    hx.update("5SHIFT", hx.shift["H4"], HIDE=1)  # 22:00-06:00, 8 hours
    for sid, (code, times) in enumerate((("LZ", "06:00-19:00"), ("TZ", "")), 960):
        hx.add("5SHIFT", ID=sid, NAME="Historie", SHORTNAME=code, POSITION=sid, HIDE=0, COLORBK=0,
               **{f"STARTEND{k}": times for k in range(8)}, **{f"DURATION{k}": 8.0 for k in range(8)})
        hx.mapping["shifts"].insert(0, {"match": code, "areaId": "HX"})
    hx.add("5GRASG", EMPLOYEEID=9206, GROUPID=next(g["ID"] for g in hx.rows("5GROUP") if g["NAME"] == "Ehrenamt"))
    leave = {r["SHORTNAME"]: r["ID"] for r in hx.rows("5LEAVT")}
    hx.duty(9201, "H1", DECEMBER)  # taken: not dropped
    hx.duty(9202, "H1", DECEMBER[20:21])
    before, before_warnings, _ = hx.export("before.json", "--hidden-shifts-months", "0")
    hx.duty(9202, "H5", DECEMBER[:3])  # not mapped: 3 duties, 36 hours
    hx.duty(9203, "H6", DECEMBER[:4])  # ignored: 4 duties, 32 hours
    hx.duty(9204, "H4", DECEMBER[:2])  # hidden: 2 duties, 16 hours
    hx.add("5MASHI", EMPLOYEEID=9205, SHIFTID=960, DATE="2026-12-01", WORKPLACID=0, TYPE=0)  # 13 hours
    hx.add("5MASHI", EMPLOYEEID=9205, SHIFTID=960, DATE="2026-12-02", WORKPLACID=0, TYPE=0)
    hx.add("5MASHI", EMPLOYEEID=9205, SHIFTID=961, DATE="2026-12-03", WORKPLACID=0, TYPE=0)  # no times: 0 hours
    # Other: a shift id that is not in 5SHIFT, a day of full-day absence, a second row of a day.
    hx.add("5MASHI", EMPLOYEEID=9202, SHIFTID=999, DATE="2026-12-06", WORKPLACID=0, TYPE=0)
    hx.duty(9203, "H6", DECEMBER[10:11])
    hx.add("5ABSEN", EMPLOYEEID=9203, DATE="2026-12-11", LEAVETYPID=leave["K"], TYPE=0, INTERVAL=0)
    hx.duty(9202, "H5", DECEMBER[20:21])  # a second row: the duty of the day is the H1 one
    hx.duty(9203, "H6", DECEMBER[:1])  # a second row on a day already counted
    hx.duty(9202, "H4", DECEMBER[:1])  # a second row on a hidden shift: the day counts for the unmapped H5
    # Not exported: 3 duties on a shift taken, 1 on a shift not mapped.
    hx.duty(9206, "H1", DECEMBER[:3])
    hx.duty(9206, "H5", DECEMBER[3:4])
    report = tmp_path / "bericht.csv"
    result, warnings, _ = hx.export("after.json", "--report", str(report), "--hidden-shifts-months", "0")
    assert result == before  # the dataset does not know these duties

    head = "Verworfene Dienste 2026-12-01..2026-12-31"
    assert [w for w in warnings if w.startswith(head)][:-1] == [
        f"{head}, Schicht nicht zugeordnet: 3 Dienste, 36.0 Stunden",
        f"{head}, Schicht ignoriert (ignoreShifts): 4 Dienste, 32.0 Stunden",
        f"{head}, Schicht ausgeblendet (HIDE): 2 Dienste, 16.0 Stunden",
        f"{head}, Schicht länger als 12 Stunden oder ohne Dienstzeit: 3 Dienste, 26.0 Stunden",
        f"{head}, sonstige: 5 Dienste, 36.0 Stunden (1 ohne Schicht in 5SHIFT, 1 an Tagen mit ganztägiger "
        "Abwesenheit, 3 neben einem anderen Eintrag desselben Tags)",
        f"{head}: zusammen 17 Dienste übernommener Mitarbeiter auf Schichten, die nicht übernommen werden (wie die "
        "Warnung '... Dienste 2026-12-01..2026-12-31 mit nicht zugeordneter Schicht ignoriert')"]

    def not_exported(lines):
        """Duties and hours of people not exported on shifts taken and on other shifts (the synthetic folder has
        some of its own in the last week)."""
        line = next(w for w in lines if w.startswith(f"{head}, Mitarbeiter nicht übernommen: "))
        m = re.match(rf"{head}, Mitarbeiter nicht übernommen: (\d+) Dienste, ([\d.]+) Stunden auf übernommenen "
                     r"Schichten \(im Bedarf gezählt außer ausgeschlossene Gruppe\), dazu (\d+) Dienste, "
                     r"([\d.]+) Stunden auf Schichten, die nicht übernommen werden$", line)
        return int(m[1]), float(m[2]), int(m[3]), float(m[4])

    was, now = not_exported(before_warnings), not_exported(warnings)
    assert (now[0] - was[0], now[1] - was[1], now[2] - was[2], now[3] - was[3]) == (3, 36.0, 1, 12.0)
    # The sums are the existing warnings: the rows on shifts not taken, the shifts without area, the duties of
    # people not exported in the demand lines.
    assert "17 Dienste 2026-12-01..2026-12-31 mit nicht zugeordneter Schicht ignoriert" in warnings
    assert "Schicht 'H6' ignoriert: 4 Dienste, 32.0 Stunden in den 1 Monaten davor" in warnings
    assert "Schicht 'H5' nicht zugeordnet: 3 Dienste, 36.0 Stunden in den 1 Monaten davor" in warnings
    left_out = [int(re.search(r"(\d+) nicht gezählt", w)[1]) + int(re.search(r"davon (\d+) von", w)[1])
                for w in warnings if w.startswith("Bedarf im Bereich")]
    assert sum(left_out) == now[0]

    rows = [r for r in (line.split(";") for line in report.read_text(encoding="utf-8").splitlines())
            if r[0] == "verworfen"]
    assert sum(int(r[4]) for r in rows if r[18] == "mitarbeiter_nicht_uebernommen") == now[0]
    assert sum(int(r[4]) for r in rows if not r[18].startswith("mitarbeiter_")) == 17
    rows = [r for r in rows if r[3] in ("", "LZ", "TZ", *HX_SHIFTS)]
    assert all(len(r) == 29 and r[1] == r[2] == "" and r[5:11] == [""] * 6 and r[12:18] == [""] * 6
               and r[20:] == [""] * 9 for r in rows)
    assert sorted((r[3], r[18], r[19], r[4], r[11]) for r in rows) == [
        ("", "unbekannte_schicht", "999", "1", "0.0"),
        ("H1", "mitarbeiter_nicht_uebernommen", str(hx.shift["H1"]), "3", "36.0"),
        ("H4", "anderer_eintrag", str(hx.shift["H4"]), "1", "8.0"),
        ("H4", "ausgeblendet", str(hx.shift["H4"]), "2", "16.0"),
        ("H5", "anderer_eintrag", str(hx.shift["H5"]), "1", "12.0"),
        ("H5", "mitarbeiter_und_schicht_nicht_uebernommen", str(hx.shift["H5"]), "1", "12.0"),
        ("H5", "nicht_zugeordnet", str(hx.shift["H5"]), "3", "36.0"),
        ("H6", "abwesenheit", str(hx.shift["H6"]), "1", "8.0"),
        ("H6", "anderer_eintrag", str(hx.shift["H6"]), "1", "8.0"),
        ("H6", "ignoriert", str(hx.shift["H6"]), "4", "32.0"),
        ("LZ", "zu_lang", "960", "2", "26.0"),
        ("TZ", "ohne_dienstzeit", "961", "1", "0.0")]

    none, warnings, _ = hx.export("none.json", "--history-months", "0", "--demand", "sp5")
    assert not [w for w in warnings if w.startswith("Verworfene Dienste")]


def _limits(dataset, area, code, minute):
    """Sum of min and of max of the windows of a day type at `minute` after that day's midnight (0 .. 48 h)."""
    inside = [r for r in dataset["demand"] if r["areaId"] == area and not r["date"] and code in r["days"]
              and (lambda s, e: s <= minute < e)(*interval(r["from"], r["to"]))]
    return sum(r["min"] for r in inside), sum(r["max"] for r in inside)


def test_history_max_is_one_for_exactly_one_person_every_day(folder, tmp_path):
    hx = _HistoryArea(folder, tmp_path)
    hx.duty(9201, "H1", DECEMBER)
    result, warnings, _ = hx.export("dataset.json")
    assert _hx_rules(result) == [(("HOL",), "07:00", "19:00", 1, 1), (CODES, "07:00", "19:00", 1, 1)]
    assert "MON 1-1/0, TUE 1-1/0" in next(w for w in warnings if w.startswith("Bedarf im Bereich HX, "))


def test_history_max_is_the_90th_percentile_of_every_day(folder, tmp_path):
    hx = _HistoryArea(folder, tmp_path)
    mondays = [d for d in DECEMBER if d.weekday() == 0]  # 4 Mondays: 1, 1, 2, 3 at once
    hx.duty(9201, "H1", mondays)
    hx.duty(9202, "H1", mondays[2:])
    hx.duty(9203, "H1", mondays[3:])
    report = tmp_path / "bericht.csv"
    result, warnings, _ = hx.export("dataset.json", "--report", str(report))
    # Median 1.5 rounds up to 2; the 90th percentile of 4 counts is the one at rank 4: 3.
    assert [r for r in _hx_rules(result) if r[0] == ("MON",)] == [(("MON",), "07:00", "19:00", 2, 3)]
    assert "MON 2-3/0" in next(w for w in warnings if w.startswith("Bedarf im Bereich HX, "))
    rows = [line.split(";") for line in report.read_text(encoding="utf-8").splitlines()[1:]]
    mon = next(r for r in rows if r[0] == "bedarf" and r[2] == "HX" and r[7] == "MON")
    assert (mon[8], mon[12]) == ("2", "3")


def test_neighbouring_segments_with_equal_min_but_different_max_stay_apart(folder, tmp_path):
    hx = _HistoryArea(folder, tmp_path)
    mondays = [d for d in DECEMBER if d.weekday() == 0]
    hx.duty(9201, "H1", mondays)  # 07:00-19:00
    hx.duty(9202, "H2", mondays[3:])  # 07:00-15:00, on one Monday of four: 07:00-15:00 has max 2, min 1
    result, _, _ = hx.export("dataset.json")
    assert [r for r in _hx_rules(result) if r[0] == ("MON",)] == [(("MON",), "07:00", "15:00", 1, 2),
                                                                  (("MON",), "15:00", "19:00", 1, 1)]


def test_history_max_of_a_night_area_adds_up_over_the_stacked_windows(folder, tmp_path):
    hx = _HistoryArea(folder, tmp_path)
    mondays = [d for d in DECEMBER if d.weekday() == 0]
    hx.duty(9201, "H3", DECEMBER)  # 19:00-07:00 every day
    hx.duty(9202, "H4", mondays[3:])  # 22:00-06:00, on one Monday of four: min 1, max 2 from 22:00 to 06:00
    result, _, _ = hx.export("dataset.json")
    # The night reaches into the next day, so the windows from 22:00 stack (min and max both): minute 0 is the
    # Monday midnight, 24 h the Tuesday midnight.
    want = {19: (1, 1), 21: (1, 1), 22: (1, 2), 23: (1, 2), 24: (1, 2), 29: (1, 2), 30: (1, 1), 30.5: (1, 1),
            31: (0, 0)}
    assert {h: _limits(result, "HX", "MON", int(h * 60)) for h in want} == want


def test_demand_from_sp5_has_no_history_max(folder, tmp_path):
    hx = _HistoryArea(folder, tmp_path)
    hx.duty(9201, "H1", DECEMBER)
    result, _, _ = hx.export("sp5.json", "--demand", "sp5")
    assert all(r["max"] is None for r in result["demand"] if r["areaId"] == "HX")


def test_replaced_duty_on_a_shift_without_area_is_not_reported(folder, tmp_path):
    hx = _HistoryArea(folder, tmp_path)
    hx.mapping["shifts"][0]["match"] = "H[1-4]"
    hx.mapping["ignoreShifts"].append("H6")
    hx.update("5SHIFT", hx.shift["H5"], HIDE=1)
    hx.duty(9201, "H1", DECEMBER)
    hx.duty(9202, "H6", DECEMBER[:2])  # planned, ignored
    hx.add("5SPSHI", EMPLOYEEID=9202, SHIFTID=hx.shift["H5"], DATE=DECEMBER[0].isoformat(), TYPE=0)  # replaces day 1
    _, warnings, _ = hx.export("dataset.json", "--hidden-shifts-months", "0")
    assert "Schicht 'H6' ignoriert: 1 Dienste, 8.0 Stunden in den 1 Monaten davor" in warnings
    assert "Schichten ohne Bereich: 1 Schichten, 1 Dienste, 8.0 Stunden in den 1 Monaten davor" in warnings


def _ignored_in_january(folder, tmp_path):
    """HX with H6 in ignoreShifts and H5 unmapped; ignored duties in the history and in the planning month."""
    hx = _HistoryArea(folder, tmp_path)
    hx.mapping["shifts"][0]["match"] = "H[1-4]"
    hx.mapping["ignoreShifts"].append("H6")
    hx.update("5EMPL", 9203, HRSWEEK=20.0)
    hx.duty(9201, "H1", DECEMBER)
    hx.duty(9202, "H6", DECEMBER[:5])  # history: ignored duties do nothing
    hx.add("5GRASG", EMPLOYEEID=9206, GROUPID=next(g["ID"] for g in hx.rows("5GROUP") if g["NAME"] == "Ehrenamt"))
    before, _, _ = hx.export("before.json")
    leave = {r["SHORTNAME"]: r["ID"] for r in hx.rows("5LEAVT")}
    jan = {d: date(2027, 1, d) for d in range(1, 32)}
    hx.duty(9202, "H6", [jan[4], jan[5], jan[9], jan[11], jan[12], jan[13]])  # Mon, Tue, Sat, Mon, Tue, Wed
    hx.duty(9203, "H6", [jan[7]])  # Thu, 20 h a week
    hx.add("5ABSEN", EMPLOYEEID=9202, DATE="2027-01-11", LEAVETYPID=leave["K"], TYPE=0, INTERVAL=0)
    hx.duty(9202, "H1", [jan[12]])  # a mapped duty on the same day
    hx.add("5ABSEN", EMPLOYEEID=9202, DATE="2027-01-13", LEAVETYPID=leave["U"], TYPE=0, INTERVAL=1)
    hx.duty(9204, "H5", [jan[4]])  # unmapped, not ignored: dropped as before
    hx.duty(9206, "H6", [jan[4]])  # not exported
    return hx, before


def test_ignored_duties_in_the_planning_month_become_absences(folder, tmp_path):
    hx, before = _ignored_in_january(folder, tmp_path)
    result, warnings, _ = hx.export("after.json")
    ext = {"code": "EXT", "name": "Sonstiger Dienst (ignorierte Schicht)", "reducesTarget": True,
           "chargesAccount": False, "compensatoryRest": False, "color": "#CCCCCC"}
    assert result["absenceTypes"] == before["absenceTypes"] + [ext]
    absences = result["periods"][0]["absences"]
    assert [(a["personId"], a["date"]) for a in absences if a["typeCode"] == "EXT"] == [
        ("E9202", "2027-01-04"), ("E9202", "2027-01-05"), ("E9202", "2027-01-09"), ("E9203", "2027-01-07")]
    assert {(a["date"], a["typeCode"]) for a in absences if a["personId"] == "E9202"} == {
        ("2027-01-04", "EXT"), ("2027-01-05", "EXT"), ("2027-01-09", "EXT"), ("2027-01-11", "K"),
        ("2027-01-13", "U")}  # the full-day absence wins, the mapped duty keeps its day, no second absence
    assert absences == sorted(absences, key=lambda a: (a["personId"], a["date"]))
    assert "Dienste auf ignorierten Schichten im 2027-01: 4 Tage, 32.0 Stunden als Abwesenheit 'EXT' übernommen" \
        in warnings
    assert result["history"] == before["history"]
    assert {k: v for k, v in result.items() if k not in ("absenceTypes", "periods")} == \
        {k: v for k, v in before.items() if k not in ("absenceTypes", "periods")}
    # As Dispofold credits it: weekly hours / 5 on a workday, nothing on a weekend.
    plain = json.loads(json.dumps(result))
    plain["periods"][0]["absences"] = [a for a in absences if a["typeCode"] != "EXT"]
    with_ext, without = capacity_hours(result, MONTH), capacity_hours(plain, MONTH)
    assert without["E9202"] - with_ext["E9202"] == 2 * 8.0
    assert without["E9203"] - with_ext["E9203"] == 4.0


def test_ignored_duties_as_absences_can_be_switched_off(folder, tmp_path):
    hx, before = _ignored_in_january(folder, tmp_path)
    hx.mapping["ignoreShiftsAsAbsence"] = False
    result, warnings, _ = hx.export("off.json")
    assert result["absenceTypes"] == before["absenceTypes"]
    assert {(a["date"], a["typeCode"]) for a in result["periods"][0]["absences"] if a["personId"] == "E9202"} == {
        ("2027-01-11", "K"), ("2027-01-13", "U")}
    assert not [a for a in result["periods"][0]["absences"] if a["personId"] == "E9203"]
    assert not [w for w in warnings if w.startswith("Dienste auf ignorierten Schichten")]


def test_absence_code_of_ignored_duties_must_be_free(folder, tmp_path):
    hx, _ = _ignored_in_january(folder, tmp_path)
    hx.add("5LEAVT", ID=890, NAME="Sonstiges", SHORTNAME="EXT", POSITION=890, HIDE=0, CHARGETYP=0, DEDUCTACT=0,
           DEDUCTOVT=0, COLORBK=0)
    with pytest.raises(Sp5Error, match="Abwesenheitsart 'EXT' gibt es schon in SP5"):
        import_sp5(hx.dir, MONTH, hx.mapping, history_months=1)
    hx.mapping["ignoreAbsences"].append("EXT")
    result, _, _ = hx.export("free.json")
    assert sum(a["typeCode"] == "EXT" for a in result["periods"][0]["absences"]) == 4


def test_alternative_shifts_of_one_post_give_one(folder, tmp_path):
    hx = _HistoryArea(folder, tmp_path)
    # One post, three shift variants: one person a day on one of them. SP5 carries MIN 1 for every variant.
    for i, d in enumerate(DECEMBER):
        hx.duty(9201 + i % 3, ("H1", "H2", "H5")[i % 3], [d])
    for code in ("H1", "H2", "H5"):
        for idx in range(8):
            hx.add("5SHDEM", GROUPID=950, WEEKDAY=idx, SHIFTID=hx.shift[code], WORKPLACID=0, MIN=1, MAX=0)
    result, warnings, _ = hx.export("history.json")
    rules = _hx_rules(result)
    assert rules and {r[3] for r in rules} == {0, 1} and {r[4] for r in rules} == {1}
    line = next(w for w in warnings if w.startswith("Bedarf im Bereich HX, "))
    assert "MON 1-1/3, TUE 1-1/3" in line
    # --demand sp5 keeps the summed SP5 demand: three at once where the variants overlap.
    sp5, warnings, _ = hx.export("sp5.json", "--demand", "sp5")
    assert max(r["min"] for r in sp5["demand"] if r["areaId"] == "HX") == 3
    assert "Bedarf aus SP5 (5SHDEM, 5SPDEM); die Historie nur zum Vergleich" in warnings
    assert line in warnings


def test_history_peak_counts_the_night_of_the_day_before(folder, tmp_path):
    hx = _HistoryArea(folder, tmp_path)
    # The night until 07:00 meets the early shift from 06:00; SP5 has exactly this staffing as its demand.
    hx.duty(9201, "H3", DECEMBER)
    hx.duty(9202, "H6", DECEMBER)
    for code in ("H3", "H6"):
        for idx in range(8):
            hx.add("5SHDEM", GROUPID=950, WEEKDAY=idx, SHIFTID=hx.shift[code], WORKPLACID=0, MIN=1, MAX=0)
    result, warnings, _ = hx.export("dataset.json")
    assert _hx_rules(result) == [(("HOL",), "06:00", "14:00", 1, 1), (("HOL",), "19:00", "07:00", 1, 1),
                                 (CODES, "06:00", "14:00", 1, 1), (CODES, "19:00", "07:00", 1, 1)]
    line = next(w for w in warnings if w.startswith("Bedarf im Bereich HX, "))
    assert ": MON 2-2/2, TUE 2-2/2, WED 2-2/2, THU 2-2/2, FRI 2-2/2, SAT 2-2/2, SUN 2-2/2, HOL 2-2/2; " in line


def test_history_demand_takes_the_median_of_every_day(folder, tmp_path):
    hx = _HistoryArea(folder, tmp_path)

    def days(code, *nth):
        return [d for d in DECEMBER if _day_code(d) == code][slice(None) if not nth else slice(*nth)]

    hx.duty(9201, "H1", days("MON"))  # 4 Mondays: 1, 1, 2, 2 at once -> 1.5 -> 2
    hx.duty(9202, "H1", days("MON", 2))
    hx.duty(9201, "H1", days("WED", 3))  # 3 of 5 Wednesdays -> 1
    hx.duty(9201, "H1", days("THU", 2))  # 2 of 5 Thursdays: the days without a duty count -> 0
    hx.duty(9201, "H1", days("FRI", 2))  # 2 of 3 Fridays -> 1
    hx.duty(9201, "H1", days("SAT", 1))  # 1 of 3 Saturdays -> 0
    hx.duty(9201, "H1", days("SUN", 2))  # 2 of 4 Sundays: 0, 0, 1, 1 -> 0.5 -> 1
    result, _, _ = hx.export("dataset.json")
    # Nobody on the holidays: a HOL rule with 0, otherwise Dispofold would take the weekday rules there.
    # THU and SAT: median 0, but one day in five (three) with a duty, so the 90th percentile is 1.
    assert _hx_rules(result) == [(("HOL",), "07:00", "19:00", 0, None), (("MON",), "07:00", "19:00", 2, 2),
                                 (("THU", "SAT"), "07:00", "19:00", 0, 1),
                                 (("WED", "FRI", "SUN"), "07:00", "19:00", 1, 1)]
    holidays = {h["date"] for h in result["holidays"]}
    assert [r["min"] for r in rules_for_day(result, "HX", date(2027, 1, 6), holidays)] == [0]  # a Wednesday


def test_median_rounds_half_up():
    assert [median_up(v) for v in ([0, 1], [1, 2], [0, 0, 1, 1], [3, 0, 1], [2], [0, 0, 0, 5])] == [1, 2, 1, 1, 2, 0]


@pytest.mark.parametrize("profile", [
    [(7, 15, 2, 2), (15, 22, 1, 1), (22, 30, 2, 2), (30, 31, 1, 1)],  # staggered nights
    [(12, 19, 1, 1), (19, 24, 2, 2), (24, 31, 1, 1)],  # a late shift ending at midnight and a night
    [(6, 14, 1, 1), (14, 22, 1, 1), (22, 31, 1, 1)],  # one person for 25 hours: no window longer than a day
    [(7, 19, 1, 1), (19, 31, 1, 1)],
    [(0, 12, 3, 3), (12, 24, 3, 3), (24, 36, 2, 2)],
    [(8, 12, 1, 1), (14, 18, 1, 1)],
    [(7, 19, 1, 2), (19, 24, 1, 3), (24, 31, 1, 2)],  # a night area with a larger max
    [(7, 19, 1, 1), (19, 24, 0, 2), (24, 31, 0, 1)],  # no min, only a limit
])
def test_windows_start_on_their_day_and_add_up_to_the_staffing(profile):
    hours = [(a * 60, b * 60, lo, hi) for a, b, lo, hi in profile]
    windows = start_day_windows(hours)
    assert all(0 <= a < DAY and a < b <= a + DAY and 0 <= lo <= hi and hi > 0 for a, b, lo, hi in windows)
    for minute in range(0, 2 * DAY, 30):
        want_min = sum(lo for a, b, lo, _ in hours if a <= minute < b)
        want_max = sum(hi for a, b, _, hi in hours if a <= minute < b)
        assert sum(hi for a, b, _, hi in windows if a <= minute < b) == want_max, minute
        assert sum(lo for a, b, lo, _ in windows if a <= minute < b) == want_min, minute
    # Neighbours with the same min and max are one window as long as it fits into a day.
    assert len(windows) <= len(profile)


def test_windows_lower_the_min_where_max_falls_less_than_min():
    # 19:00-24:00 two to two, after midnight one to two: a window with min 1 and max 0 does not exist.
    hours = [(19 * 60, 24 * 60, 2, 2), (24 * 60, 30 * 60, 1, 2)]
    windows = start_day_windows(hours)
    assert windows == [(19 * 60, 30 * 60, 1, 2)]


def test_percentile_is_nearest_rank():
    counts = list(range(10))
    assert percentile_up(counts, 90) == 8  # rank 9 of 10
    assert percentile_up([0, 0, 0, 0, 0, 0, 0, 0, 0, 1], 90) == 0
    assert percentile_up([0, 0, 0, 0, 0, 0, 0, 0, 1, 1], 90) == 1
    assert percentile_up([5], 90) == 5 and percentile_up([0, 3], 90) == 3
    assert percentile_up(list(range(31)), 90) == 27  # rank ceil(27.9) = 28


# ── shift mix ─────────────────────────────────────────────────────

def _mix_area(folder, tmp_path):
    """_HistoryArea with a second area HY: H6 (06:00-14:00, EARLY) belongs to HY, 9203 also holds HY."""
    hx = _HistoryArea(folder, tmp_path)
    hx.mapping["areas"].append({"id": "HY", "name": "Zweiter Bereich", "color": "#777777"})
    hx.mapping["shifts"].insert(0, {"match": "H6", "areaId": "HY"})
    hx.add("5GROUP", ID=951, NAME="Team Zwei", SHORTNAME="GZ", POSITION=951, HIDE=0, SUPERID=0)
    hx.mapping["groups"].append({"match": "Team Zwei", "areaIds": ["HX", "HY"]})
    hx.add("5GRASG", EMPLOYEEID=9203, GROUPID=951)
    hx.duty(9201, "H2", DECEMBER[:9])  # 07:00-15:00, DAY
    hx.duty(9201, "H3", DECEMBER[9:10])  # 19:00-07:00, NIGHT
    hx.duty(9202, "H1", DECEMBER[:7])
    hx.duty(9202, "H6", DECEMBER[7:10])  # HY is not an area of 9202
    hx.duty(9203, "H1", DECEMBER[:6])
    hx.duty(9203, "H6", DECEMBER[6:10])
    hx.duty(9204, "H1", DECEMBER[:9])  # below the minimum
    hx.duty(9205, "H1", DECEMBER[:10])  # one of them on a day of full-day absence
    leave = {r["SHORTNAME"]: r["ID"] for r in hx.rows("5LEAVT")}
    hx.add("5ABSEN", EMPLOYEEID=9205, DATE=DECEMBER[0].isoformat(), LEAVETYPID=leave["K"], TYPE=0, INTERVAL=0)
    hx.add("5RESTR", EMPLOYEEID=9201, WEEKDAY=2, SHIFTID=hx.shift["H4"], RESTRICT=1)
    return hx


def test_shift_mix_is_the_share_of_the_counted_duties(folder, tmp_path):
    hx = _mix_area(folder, tmp_path)
    report = tmp_path / "bericht.csv"
    result, warnings, _ = hx.export("mix.json", "--report", str(report))
    people = {p["id"]: p for p in result["people"]}
    assert people["E9201"]["shiftMix"] == {"categories": {"DAY": 0.9, "NIGHT": 0.1}, "areas": {"HX": 1.0}}
    # Categories over all counted duties, areas only over the person's areas.
    assert people["E9202"]["areaIds"] == ["HX"]
    assert people["E9202"]["shiftMix"] == {"categories": {"EARLY": 0.3, "DAY": 0.7}, "areas": {"HX": 1.0}}
    assert people["E9203"]["shiftMix"] == {"categories": {"EARLY": 0.4, "DAY": 0.6}, "areas": {"HX": 0.6, "HY": 0.4}}
    assert "shiftMix" not in people["E9204"] and "shiftMix" not in people["E9205"]
    assert "shiftMix" not in people["E9206"]  # no duties at all
    with_mix = sum("shiftMix" in p for p in result["people"])
    assert f"Schichtmix aus der Historie: {with_mix} Mitarbeiter mit Mix, {len(people) - with_mix} mit weniger als " \
        "10 Diensten in den 1 Monaten davor ohne Mix" in warnings

    rows = {r[1]: r for r in (line.split(";") for line in report.read_text(encoding="utf-8").splitlines())
            if r[0] == "person"}
    assert set(rows) == set(people)
    assert rows["E9201"] == ["person", "E9201", "", "", "10", *[""] * 8, "40.0", "HX", "DAY 0.900, NIGHT 0.100",
                             "HX 1.000", "1", *[""] * 11]
    assert rows["E9203"][13:18] == ["40.0", "HX HY", "EARLY 0.400, DAY 0.600", "HX 0.600, HY 0.400", "0"]
    assert rows["E9204"][4] == "9" and rows["E9204"][15:17] == ["", ""]
    assert rows["E9205"][4] == "9"  # the duty on the day of absence does not count

    lower, _, _ = hx.export("lower.json", "--mix-min-duties", "9")
    assert {p["id"]: p["shiftMix"] for p in lower["people"] if "shiftMix" in p}["E9204"] == \
        {"categories": {"DAY": 1.0}, "areas": {"HX": 1.0}}


def test_without_shift_mix_the_dataset_is_unchanged(folder, tmp_path):
    hx = _mix_area(folder, tmp_path)
    report = tmp_path / "bericht.csv"
    with_mix, _, _ = hx.export("mix.json")
    _, warnings, _ = hx.export("plain.json", "--no-shift-mix", "--report", str(report))
    for p in with_mix["people"]:
        p.pop("shiftMix", None)
    expected = json.dumps(with_mix, ensure_ascii=False, indent=1) + "\n"
    assert (tmp_path / "plain.json").read_text(encoding="utf-8") == expected
    assert not [w for w in warnings if w.startswith("Schichtmix")]
    rows = [line.split(";") for line in report.read_text(encoding="utf-8").splitlines() if line.startswith("person;")]
    assert rows and all(r[15:17] == ["", ""] for r in rows)


def test_shift_mix_needs_a_minimum_of_one(folder, tmp_path):
    proc = run(folder, MONTH, MAPPING, tmp_path / "x.json", "--mix-min-duties", "0")
    assert proc.returncode == 1 and "Schichtmix" in proc.stderr and not (tmp_path / "x.json").exists()


def test_shift_mix_shares_are_rounded_to_sum_to_one():
    assert _shares(Counter(A=1, B=1, C=1), ["A", "B", "C"]) == {"A": 0.334, "B": 0.333, "C": 0.333}
    assert _shares(Counter(A=1, B=2000), ["A", "B"]) == {"B": 1.0}  # a share rounded to 0 is left out
    assert _shares(Counter(A=2, B=1, C=1, D=1, E=1, F=1), list("ABCDEF")) == \
        {"A": 0.285, "B": 0.143, "C": 0.143, "D": 0.143, "E": 0.143, "F": 0.143}
    assert _shares(Counter(X=5), ["A"]) == {}


def _corrections(tmp_path, people, header=None):
    path = tmp_path / "korrekturen.json"
    path.write_text(json.dumps({"format": "dispofold/corrections", "version": 1, "people": people, **(header or {})}),
                    encoding="utf-8")
    return path


def _korrektur_rows(report):
    return [r for r in (line.split(";") for line in report.read_text(encoding="utf-8").splitlines())
            if r[0] == "korrektur"]


def test_corrections_replace_present_fields_and_leave_absent_ones(folder, tmp_path):
    hx = _mix_area(folder, tmp_path)
    plain, _, _ = hx.export("plain.json")
    h1, h4 = f"S{hx.shift['H1']}", f"S{hx.shift['H4']}"
    rules = [{"weekdays": [1, 2], "latestEnd": "16:00", "earliestStart": None, "noNights": True, "note": "Kind"}]
    path = _corrections(tmp_path, {
        "E9201": {"note": "frei", "weeklyHours": 30, "hoursAccount": -4.5, "compRestBalance": 2,
                  "familyRules": rules, "entryDate": "2020-02-01", "exitDate": "2030-12-31", "shiftMix": None,
                  "restrictions": [{"shiftId": h1, "category": None, "weekday": 0, "kind": "NEVER"},
                                   {"category": "NIGHT", "weekday": 6, "kind": "ON_REQUEST",
                                    "validFrom": "2027-01-01", "validTo": None}]},
        "E9202": {"areaIds": ["HX", "HY"], "shiftMix": {"categories": {"DAY": 0.5, "LATE": 0.5},
                                                         "areas": {"HX": 0.75, "HY": 0.25}}},
        "E9203": {"restrictions": []},
    })
    report = tmp_path / "bericht.csv"
    result, warnings, _ = hx.export("dataset.json", "--corrections", str(path), "--report", str(report))
    people, before = {p["id"]: p for p in result["people"]}, {p["id"]: p for p in plain["people"]}
    mixed = before["E9201"].pop("shiftMix")
    assert mixed and people["E9201"] == {**before["E9201"], "weeklyHours": 30.0, "hoursAccount": -4.5,
                                         "compRestBalance": 2, "familyRules": rules, "entryDate": "2020-02-01",
                                         "exitDate": "2030-12-31"}
    assert people["E9202"] == {**before["E9202"], "areaIds": ["HX", "HY"],
                               "shiftMix": {"categories": {"DAY": 0.5, "LATE": 0.5}, "areas": {"HX": 0.75, "HY": 0.25}}}
    assert all(people[p] == before[p] for p in people if p not in ("E9201", "E9202"))
    assert {"personId": "E9201", "shiftId": h4, "weekday": 3, "kind": "ON_REQUEST"} in plain["restrictions"]
    assert [r for r in result["restrictions"] if r["personId"] == "E9201"] == [
        {"personId": "E9201", "shiftId": h1, "category": None, "weekday": 0, "kind": "NEVER"},
        {"personId": "E9201", "category": "NIGHT", "weekday": 6, "kind": "ON_REQUEST", "validFrom": "2027-01-01",
         "validTo": None}]
    assert not [r for r in result["restrictions"] if r["personId"] == "E9203"]
    assert [r for r in result["restrictions"] if r["personId"] not in ("E9201", "E9203")] == \
        [r for r in plain["restrictions"] if r["personId"] not in ("E9201", "E9203")]
    assert {k: v for k, v in result.items() if k not in ("people", "restrictions")} == \
        {k: v for k, v in plain.items() if k not in ("people", "restrictions")}
    assert "Korrekturen: 3 Mitarbeiter korrigiert, 11 Felder angewendet, 0 Felder übersprungen" in warnings

    rows = _korrektur_rows(report)
    assert all(len(r) == 29 and r[2:18] == [""] * 16 and r[18] == "" and r[19:23] == [""] * 4 for r in rows)
    assert [(r[1], *r[23:27]) for r in rows] == [
        ("E9201", "weeklyHours", "angewendet", "40.0", "30"),
        ("E9201", "hoursAccount", "angewendet", "0.0", "-4.5"),
        ("E9201", "compRestBalance", "angewendet", "0", "2"),
        ("E9201", "familyRules", "angewendet", "0 Regeln", "1 Regeln"),
        ("E9201", "entryDate", "angewendet", "2015-01-01", "2020-02-01"),
        ("E9201", "exitDate", "angewendet", "null", "2030-12-31"),
        ("E9201", "shiftMix", "angewendet", "categories DAY 0.900, NIGHT 0.100 | areas HX 1.000", "null"),
        ("E9201", "restrictions", "angewendet", "1 Einschränkungen", "2 Einschränkungen"),
        ("E9202", "areaIds", "angewendet", "HX", "HX HY"),
        ("E9202", "shiftMix", "angewendet", "categories EARLY 0.300, DAY 0.700 | areas HX 1.000",
         "categories DAY 0.500, LATE 0.500 | areas HX 0.750, HY 0.250"),
        ("E9203", "restrictions", "angewendet", "0 Einschränkungen", "0 Einschränkungen")]
    assert "Kind" not in report.read_text(encoding="utf-8") and "frei" not in report.read_text(encoding="utf-8")


def test_corrections_with_problems_are_skipped_and_the_export_goes_on(folder, tmp_path):
    hx = _mix_area(folder, tmp_path)
    plain, _, _ = hx.export("plain.json")
    path = _corrections(tmp_path, {
        "E1": {"weeklyHours": 20.0, "note": "Notiz Quokka"},
        "Maier": {"weeklyHours": 20.0},
        "E9201": {"weeklyHours": 0, "lastName": "Neu", "colour": "rot", "compRestBalance": 1.5,
                  "hoursAccount": True},
        "E9202": {"restrictions": [{"shiftId": "S99999", "weekday": 0, "kind": "NEVER"}],
                  "entryDate": "2027-02-30"},
        "E9203": {"areaIds": ["HX"]},  # the exported mix has a share in HY
        "E9204": {"areaIds": ["HX", "QQ"], "shiftMix": {"categories": {"DAY": 0.5}},
                  "exitDate": "2010-01-01"},
        "E9205": {"restrictions": [{"personId": "E9205", "shiftId": f"S{hx.shift['H1']}", "weekday": 0,
                                    "kind": "NEVER"}]},
        "E9206": {"restrictions": [{"shiftId": f"S{hx.shift['H1']}", "category": "DAY", "weekday": 0,
                                    "kind": "NEVER"}], "shiftMix": {"areas": {"HY": 1.0}}},
        "E9207": {"restrictions": [{"shiftId": [], "weekday": 0, "kind": "NEVER"}]},
    })
    report = tmp_path / "bericht.csv"
    result, warnings, proc = hx.export("dataset.json", "--corrections", str(path), "--report", str(report))
    assert result == plain
    rows = [(r[1], r[23], r[24], r[18]) for r in _korrektur_rows(report)]
    assert rows == [
        ("", "weeklyHours", "person_fehlt", "keine Mitarbeiter-Id (E und SP5-Id)"),
        ("E1", "weeklyHours", "person_fehlt", "Mitarbeiter nicht exportiert"),
        ("E9201", "weeklyHours", "ungueltig", "muss eine Zahl größer 0 sein"),
        ("E9201", "hoursAccount", "ungueltig", "muss eine Zahl sein"),
        ("E9201", "compRestBalance", "ungueltig", "muss eine ganze Zahl sein"),
        ("E9201", "lastName", "ungueltig", "nicht korrigierbar"),
        ("E9201", "", "ungueltig", "unbekanntes Feld"),
        ("E9202", "entryDate", "ungueltig", "Datum nicht im Format JJJJ-MM-TT"),
        ("E9202", "restrictions", "ungueltig", "unbekannte Schicht-Id"),
        ("E9203", "areaIds", "ungueltig",
         "Schichtmix hat Anteile in einem Bereich, der nicht mehr in areaIds steht (shiftMix mitkorrigieren)"),
        ("E9204", "areaIds", "ungueltig", "unbekannte Bereichs-Id"),
        ("E9204", "exitDate", "ungueltig", "Austritt vor Eintritt"),
        ("E9204", "shiftMix", "ungueltig", "Anteile ergeben nicht 1"),
        ("E9205", "restrictions", "ungueltig",
         "Einschränkung mit unbekanntem Schlüssel (personId wird nicht angegeben)"),
        ("E9206", "shiftMix", "ungueltig", "Bereichsanteil für einen Bereich, der nicht in areaIds steht"),
        ("E9206", "restrictions", "ungueltig", "Einschränkung braucht genau eines von shiftId und category"),
        ("E9207", "restrictions", "ungueltig", "unbekannte Schicht-Id")]
    assert all(r[25] != "" and r[26] == "" for r in _korrektur_rows(report)
               if r[24] == "ungueltig" and r[23] not in ("", "lastName"))
    assert "Korrekturen: 0 Mitarbeiter korrigiert, 0 Felder angewendet, 17 Felder übersprungen" in warnings
    assert "Korrektur für E1 übersprungen: Mitarbeiter nicht exportiert" in warnings
    assert "Korrektur E9202 restrictions übersprungen: unbekannte Schicht-Id" in warnings
    for text in ("Maier", "Neu", "rot", "colour", "Quokka"):
        assert text not in proc.stderr and text not in report.read_text(encoding="utf-8")


def test_both_dates_and_areas_with_mix_are_checked_against_the_corrected_values(folder, tmp_path):
    hx = _mix_area(folder, tmp_path)
    path = _corrections(tmp_path, {
        "E9201": {"entryDate": "2031-01-01", "exitDate": "2032-01-01"},  # entry after the exported exit (none)
        "E9202": {"exitDate": "2028-01-01"},
        "E9203": {"areaIds": ["HX"], "shiftMix": {"categories": {"DAY": 1.0}, "areas": {"HX": 1.0}}},
    })
    hx.add("5EMPL", ID=9299, NAME="X", FIRSTNAME="Y", SHORTNAME="H9299", NUMBER="9299", POSITION=9299, HIDE=0,
           WORKDAYS="1 1 1 1 1 0 0 0", CALCBASE=1, HRSDAY=8.0, HRSWEEK=40.0, HRSMONTH=0.0, HRSTOTAL=0.0)
    result, warnings, _ = hx.export("dataset.json", "--corrections", str(path))
    people = {p["id"]: p for p in result["people"]}
    assert (people["E9201"]["entryDate"], people["E9201"]["exitDate"]) == ("2031-01-01", "2032-01-01")
    assert people["E9202"]["exitDate"] == "2028-01-01"
    assert people["E9203"]["areaIds"] == ["HX"] and people["E9203"]["shiftMix"]["areas"] == {"HX": 1.0}
    assert "Korrekturen: 3 Mitarbeiter korrigiert, 5 Felder angewendet, 0 Felder übersprungen" in warnings


@pytest.mark.parametrize("header, message", [
    ({"format": "dispofold/dataset"}, "unbekanntes Format"),
    ({"version": 3}, "Version nicht unterstützt"),
    ({"version": "1"}, "Version nicht unterstützt"),
    ({"people": []}, "'people' muss ein Objekt sein"),
])
def test_corrections_file_with_unknown_format_or_version_stops(folder, tmp_path, header, message):
    path = _corrections(tmp_path, {}, header)
    proc = run(folder, MONTH, MAPPING, tmp_path / "x.json", "--corrections", str(path))
    assert proc.returncode == 1 and message in proc.stderr and not (tmp_path / "x.json").exists()
    path.write_text("{", encoding="utf-8")
    proc = run(folder, MONTH, MAPPING, tmp_path / "x.json", "--corrections", str(path))
    assert proc.returncode == 1 and "nicht lesbar" in proc.stderr
    # Not the output or the report, nor their temporary files (name + ".tmp").
    tmp = tmp_path / "y.json.tmp"
    tmp.write_text("{", encoding="utf-8")
    for corrections, out, options in ((path, path, ()), (tmp, tmp_path / "y.json", ()),
                                      (path, tmp_path / "z.json", ("--report", str(path))),
                                      (tmp, tmp_path / "z.json", ("--report", str(tmp_path / "y.json")))):
        same = run(folder, MONTH, MAPPING, out, "--corrections", str(corrections), *options)
        assert same.returncode == 1 and "nicht die Korrekturdatei" in same.stderr
        assert corrections.read_text(encoding="utf-8") == "{"


def test_empty_corrections_leave_the_dataset_unchanged(folder, exported, tmp_path):
    path = _corrections(tmp_path, {})
    _, warnings, proc = export(folder, MONTH, MAPPING, tmp_path / "dataset.json", "--demand", "sp5",
                               "--corrections", str(path))
    assert (tmp_path / "dataset.json").read_bytes() == json.dumps(exported[0], ensure_ascii=False, indent=1) \
        .encode("utf-8") + b"\n"
    assert "Korrekturen: 0 Mitarbeiter korrigiert, 0 Felder angewendet, 0 Felder übersprungen" in warnings
    assert not [w for w in exported[1] if w.startswith("Korrektur")]


def test_history_demand_counts_people_not_exported(folder, tmp_path):
    hx = _HistoryArea(folder, tmp_path)
    hx.duty(9201, "H1", DECEMBER)  # 07:00-19:00, exported
    hx.update("5EMPL", 9202, EMPEND="2026-12-31")  # left before the planning month
    hx.duty(9202, "H3", DECEMBER)  # 19:00-07:00
    hx.update("5EMPL", 9203, HIDE=1)
    hx.duty(9203, "H4", DECEMBER)  # 22:00-06:00
    hx.add("5EMPL", ID=9210, NAME="Historie", FIRSTNAME="Person", SHORTNAME="H9210", NUMBER="9210", POSITION=9210,
           HIDE=0, WORKDAYS="1 1 1 1 1 0 0 0", CALCBASE=1, HRSDAY=8.0, HRSWEEK=40.0, HRSMONTH=0.0, HRSTOTAL=0.0,
           EMPSTART="2015-01-01")  # in no group: no area
    hx.duty(9210, "H2", DECEMBER)  # 07:00-15:00
    hx.add("5GRASG", EMPLOYEEID=9204, GROUPID=next(g["ID"] for g in hx.rows("5GROUP") if g["NAME"] == "Ehrenamt"))
    hx.duty(9204, "H2", DECEMBER)  # excluded group: does not count
    hx.duty(9205, "H2", DECEMBER)  # exported, then excluded by the corrections file: counts
    report = tmp_path / "bericht.csv"
    result, warnings, _ = hx.export("dataset.json", "--report", str(report))
    ids = {p["id"] for p in result["people"]}
    assert "E9205" in ids and not {"E9202", "E9203", "E9204", "E9210"} & ids
    for code in ("MON", "SAT", "HOL"):
        assert {h: _on_duty(result, "HX", code, h * 60) for h in (6, 10, 16, 20, 23, 30, 31)} == \
            {6: 0, 10: 3, 16: 1, 20: 1, 23: 2, 30: 1, 31: 0}, code
    line = next(w for w in warnings if w.startswith("Bedarf im Bereich HX, "))
    assert line.endswith("; 155 Dienste in den 1 Monaten davor gezählt, davon 93 von Mitarbeitern, die nicht "
                         "übernommen werden; 31 nicht gezählt (ausgeschlossene Gruppe)")
    demand = [r for r in (line.split(";") for line in report.read_text(encoding="utf-8").splitlines())
              if r[0] == "bedarf" and r[2] == "HX"]
    assert sum(int(r[4]) for r in demand) == 155 and sum(int(r[27]) for r in demand) == 93
    assert sum(int(r[10]) for r in demand) == 31

    path = _corrections(tmp_path, {"E9205": {"exclude": True}}, {"version": 2})
    excluded, _, _ = hx.export("excluded.json", "--corrections", str(path))
    assert excluded["demand"] == result["demand"] and "E9205" not in {p["id"] for p in excluded["people"]}


def _person_with_everything(dataset):
    """An exported person with a restriction, history, an absence and a previous duty."""
    period = dataset["periods"][0]
    sets = [{r["personId"] for r in items} for items in (dataset["restrictions"], dataset["history"],
                                                         period["absences"], period["previousDuties"])]
    return sorted(set.intersection(*sets))[0]


def test_exclude_drops_the_person_and_everything_that_refers_to_them(folder, exported, tmp_path):
    plain = exported[0]
    pid = _person_with_everything(plain)
    other = next(p["id"] for p in plain["people"] if p["id"] != pid)
    path = _corrections(tmp_path, {pid: {"exclude": True, "weeklyHours": 1.0, "lastName": "X", "note": "weg"},
                                   other: {"exclude": False, "weeklyHours": 12.5},
                                   "E99999": {"exclude": True}}, {"version": 2})
    report = tmp_path / "bericht.csv"
    result, warnings, _ = export(folder, MONTH, MAPPING, tmp_path / "dataset.json", "--demand", "sp5",
                                 "--corrections", str(path), "--report", str(report))  # passes the contract
    assert f'"{pid}"' not in json.dumps(result)

    def without(d):
        d = copy.deepcopy(d)
        corr._drop(d, pid)
        return d
    expected = without(plain)
    next(p for p in expected["people"] if p["id"] == other)["weeklyHours"] = 12.5
    assert result == expected
    assert len(result["people"]) == len(plain["people"]) - 1
    assert [(r[1], *r[23:27], r[18]) for r in _korrektur_rows(report)] == [
        (pid, "exclude", "ausgeschlossen", "", "true", ""),
        (other, "weeklyHours", "angewendet", str(next(p["weeklyHours"] for p in plain["people"] if p["id"] == other)),
         "12.5", ""),
        ("E99999", "exclude", "person_fehlt", "", "", "Mitarbeiter nicht exportiert")]
    assert "Korrekturen: 2 Mitarbeiter korrigiert, 2 Felder angewendet, 1 Felder übersprungen" in warnings
    assert "Korrekturen Version 2: 1 Mitarbeiter ausgeschlossen, Bedarf in 0 Bereichen ersetzt, 0 Bereiche " \
        "übersprungen" in warnings


def test_exclude_must_be_true_or_false(folder, exported, tmp_path):
    pid = exported[0]["people"][0]["id"]
    path = _corrections(tmp_path, {pid: {"exclude": "ja", "compRestBalance": 3}}, {"version": 2})
    report = tmp_path / "bericht.csv"
    result, warnings, _ = export(folder, MONTH, MAPPING, tmp_path / "dataset.json", "--demand", "sp5",
                                 "--corrections", str(path), "--report", str(report))
    assert result["people"][0]["compRestBalance"] == 3
    assert [(r[23], r[24], r[18]) for r in _korrektur_rows(report)] == [
        ("exclude", "ungueltig", "muss true oder false sein"), ("compRestBalance", "angewendet", "")]
    assert f"Korrektur {pid} exclude übersprungen: muss true oder false sein" in warnings


def test_demand_corrections_replace_the_rules_of_an_area(folder, exported, tmp_path):
    plain = exported[0]
    rule = {"days": ["MON", "TUE"], "date": None, "from": "05:20", "to": "05:20", "min": 1, "max": 1}
    dated = {"date": "2027-01-20", "from": "08:00", "to": "12:00", "min": 2}
    path = _corrections(tmp_path, {}, {"version": 2, "demand": {
        "DR": [rule, dated], "TV": [],
        "AN": [{**rule, "max": 0}],  # max below min
        "LF": [{**rule, "areaId": "LF"}],
        "TS": [{**rule, "days": ["MO"]}],
        "QQ": [rule],
    }})
    report = tmp_path / "bericht.csv"
    result, warnings, _ = export(folder, MONTH, MAPPING, tmp_path / "dataset.json", "--demand", "sp5",
                                 "--corrections", str(path), "--report", str(report))
    count = Counter(r["areaId"] for r in plain["demand"])
    assert [r for r in result["demand"] if r["areaId"] == "DR"] == [{"areaId": "DR", **rule}, {"areaId": "DR", **dated}]
    assert not [r for r in result["demand"] if r["areaId"] == "TV"]
    assert [r for r in result["demand"] if r["areaId"] not in ("DR", "TV")] == \
        [r for r in plain["demand"] if r["areaId"] not in ("DR", "TV")]
    # The replaced rules stand where the first rule of the area stood.
    first = next(i for i, r in enumerate(plain["demand"]) if r["areaId"] == "DR")
    assert result["demand"][first:first + 2] == [{"areaId": "DR", **rule}, {"areaId": "DR", **dated}]
    assert [(r[1], *r[23:27], r[18]) for r in _korrektur_rows(report)] == [
        ("", "demand:AN", "ungueltig", f"{count['AN']} Regeln", "", "max muss null oder eine ganze Zahl ab min sein"),
        ("", "demand:DR", "angewendet", f"{count['DR']} Regeln", "2 Regeln", ""),
        ("", "demand:TV", "angewendet", f"{count['TV']} Regeln", "0 Regeln", ""),
        ("", "demand:LF", "ungueltig", f"{count['LF']} Regeln", "",
         "Regel mit unbekanntem Schlüssel (areaId wird nicht angegeben)"),
        ("", "demand:TS", "ungueltig", f"{count['TS']} Regeln", "",
         "days muss eine Liste aus MON bis SUN und HOL sein"),
        ("", "demand", "ungueltig", "", "", "unbekannte Bereichs-Id")]
    assert "Korrekturen Version 2: 0 Mitarbeiter ausgeschlossen, Bedarf in 2 Bereichen ersetzt, 4 Bereiche " \
        "übersprungen" in warnings
    assert "Korrektur demand übersprungen: unbekannte Bereichs-Id" in warnings
    assert not any("QQ" in w for w in warnings)


def test_version_1_files_know_neither_exclude_nor_demand(folder, exported, tmp_path):
    pid = exported[0]["people"][0]["id"]
    path = _corrections(tmp_path, {pid: {"exclude": True}}, {"demand": {"DR": []}})
    report = tmp_path / "bericht.csv"
    result, warnings, _ = export(folder, MONTH, MAPPING, tmp_path / "dataset.json", "--demand", "sp5",
                                 "--corrections", str(path), "--report", str(report))
    assert result == exported[0]
    assert [(r[1], *r[23:25], r[18]) for r in _korrektur_rows(report)] == [(pid, "", "ungueltig", "unbekanntes Feld")]
    assert not [w for w in warnings if w.startswith("Korrekturen Version 2")]


def test_demand_must_be_an_object_in_version_2(folder, tmp_path):
    path = _corrections(tmp_path, {}, {"version": 2, "demand": []})
    proc = run(folder, MONTH, MAPPING, tmp_path / "x.json", "--corrections", str(path))
    assert proc.returncode == 1 and "'demand' muss ein Objekt sein" in proc.stderr


def test_hidden_shifts_in_use_are_taken(folder, tmp_path):
    hx = _HistoryArea(folder, tmp_path)
    hx.mapping["ignoreShifts"].append("H6")
    hx.mapping["shifts"][0]["match"] = "H[1-6]"
    for code in ("H2", "H3", "H4", "H5", "H6"):
        hx.update("5SHIFT", hx.shift[code], HIDE=1)
    hx.duty(9201, "H1", DECEMBER)
    hx.duty(9202, "H3", DECEMBER[:10])  # used recently: taken
    hx.duty(9203, "H4", [date(2026, 8, d) for d in range(3, 8)])  # used only long ago: dropped
    hx.duty(9204, "H5", [date(2027, 1, 12)])  # used only in the planning month: taken
    hx.duty(9205, "H6", DECEMBER[:2])  # in use, but ignored: stays ignored
    hx.duty(9206, "H2", DECEMBER[:1])  # replaced by a deviation: not in use
    hx.add("5SPSHI", EMPLOYEEID=9206, SHIFTID=hx.shift["H1"], DATE=DECEMBER[0].isoformat(), TYPE=0)
    # H2 has no duty that counts: dropped, no row.
    report = tmp_path / "bericht.csv"
    result, warnings, _ = hx.export("dataset.json", "--history-months", "6", "--report", str(report))
    codes = {s["code"]: s for s in result["shifts"]}
    assert {"H1", "H3", "H5"} <= set(codes) and not {"H2", "H4", "H6"} & set(codes)
    assert "Ausgeblendete Schichten: 2 übernommen, weil in Gebrauch (Dienste in den Planungsmonaten oder den 3 " \
        "Monaten davor), 3 nicht übernommen" in warnings
    # Treated like a visible shift: its duties are history and demand.
    december = {h["personId"]: h["hours"] for h in result["history"] if h["month"] == "2026-12"}
    assert december["E9202"] == 120.0
    assert any(r["from"] == "19:00" and r["max"] for r in result["demand"] if r["areaId"] == "HX")  # H3's night
    rows = [r for r in (line.split(";") for line in report.read_text(encoding="utf-8").splitlines())
            if r[0] == "ausgeblendet"]
    assert all(len(r) == 29 and r[1] == r[2] == "" for r in rows)
    assert [(r[3], r[4], r[11], r[18], r[19], r[24], r[28]) for r in rows] == [
        ("H3", "10", "120.0", "", str(hx.shift["H3"]), "uebernommen", "2026-12-10"),
        ("H4", "5", "40.0", "nicht_in_gebrauch", str(hx.shift["H4"]), "verworfen", "2026-08-07"),
        ("H5", "1", "12.0", "", str(hx.shift["H5"]), "uebernommen", "2027-01-12"),
        ("H6", "2", "16.0", "ignoriert", str(hx.shift["H6"]), "verworfen", "2026-12-02")]
    dropped = [r for r in (line.split(";") for line in report.read_text(encoding="utf-8").splitlines())
               if r[0] == "verworfen" and r[18] == "ausgeblendet"]
    assert {r[3] for r in dropped} == {"H4"}

    longer, warnings, _ = hx.export("longer.json", "--history-months", "6", "--hidden-shifts-months", "5")
    assert "H4" in {s["code"] for s in longer["shifts"]}
    none, warnings, _ = hx.export("none.json", "--history-months", "6", "--hidden-shifts-months", "0")
    assert not {"H2", "H3", "H4", "H5", "H6"} & {s["code"] for s in none["shifts"]}
    assert "Ausgeblendete Schichten: 0 übernommen, weil in Gebrauch (Dienste in den Planungsmonaten oder den 0 " \
        "Monaten davor), 5 nicht übernommen" in warnings
    bad = run(folder, MONTH, MAPPING, tmp_path / "x.json", "--hidden-shifts-months", "-1")
    assert bad.returncode == 1 and "Ausgeblendete Schichten" in bad.stderr
