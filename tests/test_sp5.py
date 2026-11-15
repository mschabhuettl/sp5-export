import copy
import hashlib
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

from sp5export.dbf import read_table
from sp5export.demand import median_up, start_day_windows
from sp5export.feasibility import check_feasibility, rules_for_day
from sp5export.sp5 import Sp5Error, import_sp5, load_mapping
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
        "tagestyp;spitze_historie;spitze_sp5;dienste_nicht_uebernommen"
    rows = [line.split(";") for line in lines[1:]]
    assert {r[0] for r in rows} == {"bedarf", "bereich", "schicht"}
    assert all(len(r) == 11 and r[4].isdigit() for r in rows)
    assert all(re.match(r"E\d+$", r[1]) and r[7:] == ["", "", "", ""] for r in rows if r[0] != "bedarf")
    assert all(r[3] == "" and r[5] in ("ja", "nein") and r[6] == "" for r in rows if r[0] == "bereich")
    demand = [r for r in rows if r[0] == "bedarf"]
    assert [(r[2], r[7]) for r in demand] == [(a["id"], c) for a in sorted(imported[0]["areas"], key=lambda a: a["id"])
                                              for c in ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN", "HOL")]
    assert all(r[1] == r[3] == r[5] == r[6] == "" and r[8].isdigit() and r[9].isdigit() and r[10].isdigit()
               for r in demand)
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
                                              "", ""]
    assert rows[("E9101", day["code"])][4:] == ["6", "", "", "", "", "", ""]
    assert rows[("E9102", night["code"])][6] == "sp5"
    assert rows[("E9103", night["code"])][6] == "zu_kurze_historie"
    text = report.read_text(encoding="utf-8")
    assert not any(name in text for name in ("Nur", "Tag", *_names(source[0])))


# ── demand from the history ───────────────────────────────────────

DECEMBER = [date(2026, 12, d) for d in range(1, 32)]
HOLIDAYS = {date(2026, 12, 8), date(2026, 12, 25), date(2026, 12, 26)}  # full holidays of the synthetic folder
CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
HX_SHIFTS = {"H1": ("07:00", "19:00"), "H2": ("07:00", "15:00"), "H3": ("19:00", "07:00"), "H4": ("22:00", "06:00"),
             "H5": ("12:00", "00:00")}


def _day_code(d):
    return "HOL" if d in HOLIDAYS else CODES[d.weekday()]


class _HistoryArea(_Sp5Copy):
    """The synthetic folder plus an area HX (shifts H1..H5, group 'Team Historie', people 9201..9209) whose
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
        self.mapping["shifts"].insert(0, {"match": "H[1-5]", "areaId": "HX"})
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
    # Not exported: someone in an excluded group, and a hidden employee.
    hx.add("5GRASG", EMPLOYEEID=9206, GROUPID=next(g["ID"] for g in hx.rows("5GROUP") if g["NAME"] == "Ehrenamt"))
    hx.duty(9206, "H1", DECEMBER)
    hx.update("5EMPL", 9207, HIDE=1)
    hx.duty(9207, "H2", DECEMBER[:5])
    report = tmp_path / "bericht.csv"
    result, warnings, _ = hx.export("dataset.json", "--report", str(report))

    assert _hx_rules(result) == [
        (("HOL",), "07:00", "19:00", 2, None), (("HOL",), "19:00", "07:00", 1, None),
        (("MON", "TUE", "WED", "THU", "FRI"), "07:00", "15:00", 2, None),
        (("MON", "TUE", "WED", "THU", "FRI"), "15:00", "07:00", 1, None),
        (("MON", "TUE", "WED", "THU", "FRI"), "22:00", "06:00", 1, None),
        (("SAT", "SUN"), "07:00", "07:00", 1, None), (("SAT", "SUN"), "22:00", "06:00", 1, None),
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

    left_out = 31 + 5
    counted = 31 + 31 + 28 + len(workdays) + 3
    line = next(w for w in warnings if w.startswith("Bedarf im Bereich HX, "))
    assert line == ("Bedarf im Bereich HX, Spitze je Tagestyp aus der Historie/aus SP5: MON 2/0, TUE 2/0, WED 2/0, "
                    f"THU 2/0, FRI 2/0, SAT 2/0, SUN 2/0, HOL 2/0; {counted} Dienste in den 1 Monaten davor gezählt, "
                    f"{left_out} nicht gezählt (Mitarbeiter nicht übernommen)")
    assert "Bedarf aus der Historie: Median je Bereich und Tagestyp über die 1 Monate davor" in warnings
    rows = [line.split(";") for line in report.read_text(encoding="utf-8").splitlines()[1:]]
    demand = {r[7]: r for r in rows if r[0] == "bedarf" and r[2] == "HX"}
    assert list(demand) == [*CODES, "HOL"]
    assert demand["HOL"] == ["bedarf", "", "HX", "", "9", "", "", "HOL", "2", "0", "3"]
    assert sum(int(r[4]) for r in demand.values()) == counted
    assert sum(int(r[10]) for r in demand.values()) == left_out
    # Duties on a holiday count for HOL only, not for their weekday (2026-12-08 is a Tuesday).
    assert int(demand["TUE"][10]) == 4 + 1  # 4 Tuesdays of the excluded person, 2026-12-01 of the hidden one


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
    assert rules and {r[3] for r in rules} == {1}
    line = next(w for w in warnings if w.startswith("Bedarf im Bereich HX, "))
    assert "MON 1/3, TUE 1/3" in line
    # --demand sp5 keeps the summed SP5 demand: three at once where the variants overlap.
    sp5, warnings, _ = hx.export("sp5.json", "--demand", "sp5")
    assert max(r["min"] for r in sp5["demand"] if r["areaId"] == "HX") == 3
    assert "Bedarf aus SP5 (5SHDEM, 5SPDEM); die Historie nur zum Vergleich" in warnings
    assert line in warnings


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
    assert _hx_rules(result) == [(("MON",), "07:00", "19:00", 2, None),
                                 (("WED", "FRI", "SUN"), "07:00", "19:00", 1, None)]


def test_median_rounds_half_up():
    assert [median_up(v) for v in ([0, 1], [1, 2], [0, 0, 1, 1], [3, 0, 1], [2], [0, 0, 0, 5])] == [1, 2, 1, 1, 2, 0]


@pytest.mark.parametrize("profile", [
    [(7, 15, 2), (15, 22, 1), (22, 30, 2), (30, 31, 1)],  # staggered nights
    [(12, 19, 1), (19, 24, 2), (24, 31, 1)],  # a late shift ending at midnight and a night
    [(6, 14, 1), (14, 22, 1), (22, 31, 1)],  # one person for 25 hours: no window longer than a day
    [(7, 19, 1), (19, 31, 1)],
    [(0, 12, 3), (12, 24, 3), (24, 36, 2)],
    [(8, 12, 1), (14, 18, 1)],
])
def test_windows_start_on_their_day_and_add_up_to_the_staffing(profile):
    hours = [(a * 60, b * 60, c) for a, b, c in profile]
    windows = start_day_windows(hours)
    assert all(0 <= a < DAY and a < b <= a + DAY and c > 0 for a, b, c in windows)
    for minute in range(0, 2 * DAY, 30):
        want = sum(c for a, b, c in hours if a <= minute < b)
        assert sum(c for a, b, c in windows if a <= minute < b) == want, minute
    # Neighbours with the same count are one window as long as it fits into a day.
    assert len(windows) <= len(profile)
