"""Exports and fixtures pass Dispofold's import: its JSON schema and its validator (ported in contract.py).

The schema, the shared accept/reject corpus and the format description are copies from Dispofold (see
tests/dispofold/SOURCE.md). The port of the validator is checked against the expected codes of every reject
file, so it cannot drift from the corpus unnoticed.
"""

import json
import re
from pathlib import Path

import pytest
from contract import DISPOFOLD, problems, schema_errors, validate
from exportcli import MAPPING, export

from tools.generator import generate_with_mixes
from tools.sp5writer import write_sp5_folder

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ACCEPT = sorted((DISPOFOLD / "accept").glob("*.synthetic.json"))
REJECT = sorted((DISPOFOLD / "reject").glob("*.synthetic.json"))

# Reject files the schema accepts: their defects are rules the schema cannot express (references between
# entries, unique ids and entries, dates against the planning month, employment and validity order, demand
# max against min, shift length against maxDutyHours). Only the validator (contract.py) finds them. The
# exporter's own tests cover the ones it could produce (test_sp5.py: weekly hours, shift length,
# employment order, absences and previous duties against the month, references).
SCHEMA_CANNOT_EXPRESS = {
    "absence-outside-month", "bad-demand-max", "bad-employment", "bad-validity", "duplicate-absence-type",
    "duplicate-area-id", "duplicate-fixed", "duplicate-history", "duplicate-period", "duplicate-person-id",
    "duplicate-shift-id", "fixed-outside-month", "previous-duty-in-month", "several-errors", "shift-24-hours",
    "shift-too-long", "unknown-absence-type", "unknown-area-demand", "unknown-area-person", "unknown-area-shift",
    "unknown-person-absence", "unknown-person-history", "unknown-person-restriction", "unknown-shift-fixed",
    "unknown-shift-previous-duty", "unknown-shift-restriction", "wish-outside-month",
}


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _name(path: Path) -> str:
    return path.name.removesuffix(".synthetic.json")


def test_corpus_is_complete_and_its_source_is_recorded():
    assert len(ACCEPT) >= 4 and len(REJECT) >= 40
    for path in REJECT:
        assert path.with_name(f"{_name(path)}.synthetic.expected.json").is_file(), path.name
    assert re.search(r"Dispofold commit: `[0-9a-f]{40}`", (DISPOFOLD / "SOURCE.md").read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", ACCEPT, ids=_name)
def test_accept_files_pass(path):
    assert problems(_load(path)) == []


@pytest.mark.parametrize("path", REJECT, ids=_name)
def test_validator_port_finds_the_expected_codes(path):
    expected = _load(path.with_name(f"{_name(path)}.synthetic.expected.json"))["codes"]
    errors, _ = validate(_load(path))
    assert sorted({code for code, _ in errors}) == sorted(set(expected))


@pytest.mark.parametrize("path", REJECT, ids=_name)
def test_schema_rejects_what_it_can_express(path):
    assert (schema_errors(_load(path)) == []) == (_name(path) in SCHEMA_CANNOT_EXPRESS)


def test_list_of_schema_gaps_is_current():
    assert SCHEMA_CANNOT_EXPRESS <= {_name(p) for p in REJECT}


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.synthetic.json")), ids=lambda p: p.name)
def test_synthetic_fixtures_pass(path):
    assert problems(_load(path)) == []


def test_export_passes(tmp_path):
    dataset, mixes = generate_with_mixes("2027-01", 3)
    write_sp5_folder(dataset, mixes, tmp_path / "sp5")
    # export() itself asserts that the file passes; checked here once more to keep the contract visible.
    result, _, _ = export(tmp_path / "sp5", "2027-01", MAPPING, tmp_path / "dataset.json", "--demand", "sp5")
    assert result["people"] and result["shifts"] and result["demand"] and result["history"]
    assert result["version"] == 2 and problems(result) == []
    assert {h["source"] for h in result["history"]} == {"import"}


def _with_mix(mix):
    d = _load(ACCEPT[0])
    person = next(p for p in d["people"] if p["areaIds"])
    person["shiftMix"] = mix(person["areaIds"])
    return d, person["id"]


@pytest.mark.parametrize("mix", [
    lambda areas: {"categories": {"NIGHT": 0.1, "DAY": 0.9}, "areas": {areas[0]: 1.0}},
    lambda areas: {"categories": {"DAY": 0.333, "LATE": 0.333, "NIGHT": 0.333}, "areas": {}},
    lambda areas: {"categories": {}},
    lambda areas: None,
], ids=["full", "sum-within-tolerance", "empty", "null"])
def test_shift_mix_is_accepted(mix):
    d, _ = _with_mix(mix)
    assert problems(d) == []


@pytest.mark.parametrize("mix, in_schema", [
    (lambda areas: {"categories": {"NIGHT": 0.5, "DAY": 0.4}}, False),
    (lambda areas: {"categories": {"DAY": 1.0}, "areas": {areas[0]: 0.6, "ZZ": 0.4}}, False),
    (lambda areas: {"categories": {"DAY": 1.2, "NIGHT": -0.2}}, True),
    (lambda areas: {"categories": {"DAY": 1.0}, "areas": {areas[0]: 0.98}}, False),
], ids=["sum-below-one", "area-not-of-the-person", "share-outside-0-1", "areas-sum-off"])
def test_bad_shift_mix_is_rejected(mix, in_schema):
    d, pid = _with_mix(mix)
    errors, _ = validate(d)
    assert ("import.badShiftMix", {"personId": pid}) in errors
    assert bool(schema_errors(d)) == in_schema


def test_shift_mix_schema_rejects_unknown_categories_and_keys():
    d, _ = _with_mix(lambda areas: {"categories": {"MIDDAY": 1.0}})
    assert schema_errors(d)
    d, _ = _with_mix(lambda areas: {"categories": {"DAY": 1.0}, "extra": {}})
    assert schema_errors(d)
