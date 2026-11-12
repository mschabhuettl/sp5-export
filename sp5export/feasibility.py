"""Rough feasibility check of a dataset month: can the staff cover the demand at all?

Capacity of a person is the expected working time of the month: weekly hours / 5 per working day
(Monday to Friday, no public holiday) without absence days, minus open compensatory rest days and a
quarter of the hours account. Demand is the sum of minimum staffing times window length.
"""

from datetime import date
from itertools import combinations

from .timeutil import DAY, WEEKDAY_CODES, interval, month_days

ACCOUNT_CARRY_SHARE = 0.25


def rules_for_day(dataset: dict, area_id: str, day: date, holiday_dates: set[str]) -> list[dict]:
    """Demand rules of an area that apply on a day (date rules, then HOL rules, then weekday rules)."""
    rules = [r for r in dataset["demand"] if r["areaId"] == area_id]
    dated = [r for r in rules if r.get("date") == day.isoformat()]
    if dated:
        return dated
    general = [r for r in rules if not r.get("date")]
    if day.isoformat() in holiday_dates:
        hol = [r for r in general if "HOL" in r["days"]]
        if hol:
            return hol
    code = WEEKDAY_CODES[day.weekday()]
    return [r for r in general if code in r["days"]]


def demand_hours(dataset: dict, month: str) -> dict[str, float]:
    holiday_dates = {h["date"] for h in dataset["holidays"]}
    result = {a["id"]: 0.0 for a in dataset["areas"]}
    for day in month_days(month):
        for area_id in result:
            for r in rules_for_day(dataset, area_id, day, holiday_dates):
                s, e = interval(r["from"], r["to"])
                result[area_id] += r["min"] * (e - s) / 60
    return result


def _period(dataset: dict, month: str) -> dict:
    for p in dataset["periods"]:
        if p["month"] == month:
            return p
    raise ValueError(f"no period for {month}")


def capacity_hours(dataset: dict, month: str) -> dict[str, float]:
    holiday_dates = {h["date"] for h in dataset["holidays"]}
    absent: dict[str, set[str]] = {}
    for a in _period(dataset, month)["absences"]:
        absent.setdefault(a["personId"], set()).add(a["date"])
    workdays = [d.isoformat() for d in month_days(month)
                if d.weekday() < 5 and d.isoformat() not in holiday_dates]
    result = {}
    for p in dataset["people"]:
        daily = p["weeklyHours"] / 5
        days = sum(1 for d in workdays if d not in absent.get(p["id"], set()))
        hours = daily * (days - p["compRestBalance"]) - ACCOUNT_CARRY_SHARE * p["hoursAccount"]
        result[p["id"]] = max(0.0, hours)
    return result


def usable_areas(dataset: dict) -> dict[str, set[str]]:
    """Areas per person, without areas whose shifts are all excluded by an every-day NEVER restriction."""
    never = {(r["personId"], r["shiftId"]) for r in dataset["restrictions"]
             if r["kind"] == "NEVER" and r["weekday"] == 0}
    shifts_by_area: dict[str, list[str]] = {}
    for s in dataset["shifts"]:
        shifts_by_area.setdefault(s["areaId"], []).append(s["id"])
    return {p["id"]: {a for a in p["areaIds"]
                      if any((p["id"], s) not in never for s in shifts_by_area.get(a, []))}
            for p in dataset["people"]}


def _covered(window: tuple[int, int], intervals: list[tuple[int, int]]) -> bool:
    pos, end = window
    for s, e in sorted(intervals):
        if s <= pos < e:
            pos = e
        if pos >= end:
            return True
    return pos >= end


def check_feasibility(dataset: dict, month: str) -> dict:
    """Return demand and capacity figures and a list of problems (empty when the month looks coverable)."""
    problems = []
    shifts_by_area: dict[str, list[tuple[int, int]]] = {}
    for s in dataset["shifts"]:
        s0, e0 = interval(s["start"], s["end"])
        shifts_by_area.setdefault(s["areaId"], []).extend(
            (s0 + k * DAY, e0 + k * DAY) for k in (-1, 0, 1))
    for r in dataset["demand"]:
        if not _covered(interval(r["from"], r["to"]), shifts_by_area.get(r["areaId"], [])):
            problems.append(f"window {r['areaId']} {r['from']}-{r['to']} not covered by its shifts")
        if r.get("max") is not None and r["max"] < r["min"]:
            problems.append(f"window {r['areaId']} {r['from']}-{r['to']} has max below min")

    demand = demand_hours(dataset, month)
    capacity = capacity_hours(dataset, month)
    areas = usable_areas(dataset)
    area_ids = [a["id"] for a in dataset["areas"]]
    for size in range(1, len(area_ids) + 1):
        for subset in combinations(area_ids, size):
            need = sum(demand[a] for a in subset)
            have = sum(capacity[pid] for pid, own in areas.items() if own & set(subset))
            if need > have:
                problems.append(f"areas {'+'.join(subset)}: demand {need:.0f} h > capacity {have:.0f} h")

    holiday_dates = {h["date"] for h in dataset["holidays"]}
    absent = {(a["personId"], a["date"]) for a in _period(dataset, month)["absences"]}
    for day in month_days(month):
        for area_id in area_ids:
            peak = max((r["min"] for r in rules_for_day(dataset, area_id, day, holiday_dates)), default=0)
            present = sum(1 for pid, own in areas.items()
                          if area_id in own and (pid, day.isoformat()) not in absent)
            if peak > present:
                problems.append(f"{day} {area_id}: {peak} needed at once, {present} present")

    total_demand = sum(demand.values())
    total_capacity = sum(capacity.values())
    return {
        "demandHours": {a: round(h, 1) for a, h in demand.items()},
        "totalDemandHours": round(total_demand, 1),
        "totalCapacityHours": round(total_capacity, 1),
        "ratio": round(total_demand / total_capacity, 3) if total_capacity else None,
        "problems": problems,
    }
