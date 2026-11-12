import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest

from sp5export.feasibility import check_feasibility, rules_for_day
from sp5export.holidays import easter_sunday
from sp5export.timeutil import DAY, interval, minutes
from tools.generator import dumps, generate

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TIME = re.compile(r"^\d\d:\d\d$")


@pytest.fixture(scope="module")
def demo():
    return generate("2027-01", 1)


@pytest.mark.parametrize("name,kwargs", [
    ("demo-dataset-2027-01.synthetic.json", {}),
    ("small-dataset-2027-01.synthetic.json", {"people": 20, "areas": ["AN", "DR", "TV"]}),
])
def test_committed_fixtures_are_reproducible(name, kwargs):
    assert (FIXTURES / name).read_text(encoding="utf-8") == dumps(generate("2027-01", 1, **kwargs))


@pytest.mark.parametrize("month,seed", [("2027-01", 1), ("2027-01", 2), ("2026-12", 3), ("2027-04", 4),
                                        ("2027-08", 5), ("2028-02", 6)])
def test_demo_months_are_coverable(month, seed):
    report = check_feasibility(generate(month, seed), month)
    assert report["problems"] == []
    assert 0.80 <= report["ratio"] <= 0.85


def test_small_dataset_is_coverable():
    report = check_feasibility(generate("2027-01", 1, people=20, areas=["AN", "DR", "TV"]), "2027-01")
    assert report["problems"] == []
    assert 0.75 <= report["ratio"] <= 0.9


def test_demand_factor_makes_month_impossible():
    report = check_feasibility(generate("2027-01", 1, demand_factor=3), "2027-01")
    assert report["ratio"] > 2
    assert any("capacity" in p for p in report["problems"])


def test_feasibility_detects_uncovered_window(demo):
    broken = json.loads(json.dumps(demo))
    broken["demand"].append({"areaId": "TV", "days": ["MON"], "date": None, "from": "23:00", "to": "02:00",
                             "min": 1, "max": None})
    assert any("not covered" in p for p in check_feasibility(broken, "2027-01")["problems"])


def test_easter():
    assert easter_sunday(2027) == date(2027, 3, 28)
    assert easter_sunday(2026) == date(2026, 4, 5)


def test_shape_and_formats(demo):
    assert demo["format"] == "dispofold/dataset" and demo["version"] == 2
    assert [a["id"] for a in demo["areas"]] == ["AN", "DR", "TV", "LF", "TS"]
    ids = [s["id"] for s in demo["shifts"]]
    assert len(ids) == len(set(ids))
    for s in demo["shifts"]:
        assert TIME.match(s["start"]) and TIME.match(s["end"])
        start, end = interval(s["start"], s["end"])
        assert end - start <= 12 * 60
        assert s["category"] in {"EARLY", "DAY", "LATE", "NIGHT"}
        assert s["unpopular"] == (s["category"] == "NIGHT")
    for r in demo["demand"]:
        assert TIME.match(r["from"]) and TIME.match(r["to"])
        assert set(r["days"]) <= {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN", "HOL"}
    assert len(demo["people"]) == 150
    assert len({p["shortName"] for p in demo["people"]}) == 150
    assert 200 <= len(demo["restrictions"]) <= 500
    assert {h["date"][:4] for h in demo["holidays"]} == {"2026", "2027"}
    assert sorted({h["month"] for h in demo["history"]}) == ["2026-10", "2026-11", "2026-12"]
    assert all(t["reducesTarget"] for t in demo["absenceTypes"])
    assert [t["code"] for t in demo["absenceTypes"] if t["chargesAccount"]] == ["ZA"]
    assert [t["code"] for t in demo["absenceTypes"] if t["compensatoryRest"]] == ["ER"]


def test_lead_exactly_one_around_the_clock(demo):
    lead = [r for r in demo["demand"] if r["areaId"] == "LF"]
    assert all(r["min"] == 1 and r["max"] == 1 for r in lead)
    for day in (date(2027, 1, 4), date(2027, 1, 9)):
        covered = sum(-(interval(r["from"], r["to"])[0] - interval(r["from"], r["to"])[1])
                      for r in rules_for_day(demo, "LF", day, set()))
        assert covered == DAY


def test_hard_inputs_are_consistent(demo):
    period = demo["periods"][0]
    people = {p["id"]: p for p in demo["people"]}
    shifts = {s["id"]: s for s in demo["shifts"]}
    absent = {(a["personId"], a["date"]) for a in period["absences"]}
    never = {(r["personId"], r["shiftId"], r["weekday"]) for r in demo["restrictions"] if r["kind"] == "NEVER"}
    karenz = {pid for pid in people if sum(1 for a in period["absences"]
                                           if a["personId"] == pid and a["typeCode"] == "KA") == 31}
    assert karenz

    def allowed(pid, sid, day):
        weekday = date.fromisoformat(day).isoweekday()
        return (shifts[sid]["areaId"] in people[pid]["areaIds"] and (pid, sid, 0) not in never
                and (pid, sid, weekday) not in never)

    for f in period["fixed"]:
        assert (f["personId"], f["date"]) not in absent and f["personId"] not in karenz
        assert f["date"] >= "2027-01-03"
        assert f["shiftId"] is None or allowed(f["personId"], f["shiftId"], f["date"])
    per_person = defaultdict(list)
    for w in period["wishes"]:
        assert (w["personId"], w["date"]) not in absent
        assert (w["shiftId"] is not None) == (w["kind"] in ("SHIFT", "NOT_SHIFT"))
        per_person[w["personId"]].append(w["date"])
    assert all(len(v) <= 6 and len(v) == len(set(v)) for v in per_person.values())
    assert not karenz & set(per_person)

    duties = defaultdict(list)
    for d in period["previousDuties"]:
        assert "2026-12-25" <= d["date"] <= "2026-12-31"
        assert allowed(d["personId"], d["shiftId"], d["date"])
        duties[d["personId"]].append(d)
    assert not karenz & set(duties)
    for items in duties.values():
        spans = []
        for d in sorted(items, key=lambda x: x["date"]):
            offset = (date.fromisoformat(d["date"]) - date(2026, 12, 25)).days * DAY
            s, e = interval(shifts[d["shiftId"]]["start"], shifts[d["shiftId"]]["end"])
            spans.append((offset + s, offset + e))
        for (_, e1), (s2, _) in zip(spans, spans[1:], strict=False):
            assert s2 - e1 >= 11 * 60
        dates = sorted(date.fromisoformat(d["date"]).toordinal() for d in items)
        run = best = 1
        for a, b in zip(dates, dates[1:], strict=False):
            run = run + 1 if b == a + 1 else 1
            best = max(best, run)
        assert best <= 4


def test_previous_nights_cover_first_morning(demo):
    shifts = {s["id"]: s for s in demo["shifts"]}
    nights = defaultdict(int)
    for d in demo["periods"][0]["previousDuties"]:
        s = shifts[d["shiftId"]]
        if d["date"] == "2026-12-31" and minutes(s["end"]) <= minutes(s["start"]):
            nights[s["areaId"]] += 1
    holidays = {h["date"] for h in demo["holidays"]}
    for area in nights.keys() | {"LF", "DR", "AN"}:
        first = [r for r in rules_for_day(demo, area, date(2027, 1, 1), holidays) if r["from"] == "00:00"]
        assert first, area
        assert nights[area] >= first[0]["min"], area


@pytest.mark.parametrize("seed", [7, 23])
def test_sweep_over_a_year(seed):
    from sp5export.timeutil import add_months

    for k in range(12):
        month = add_months("2026-12", k)
        report = check_feasibility(generate(month, seed), month)
        assert report["problems"] == [], (month, seed)
        assert 0.80 <= report["ratio"] <= 0.85, (month, seed)
