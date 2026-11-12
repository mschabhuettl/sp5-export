"""Import the vendor's SP5 sample database (30 invented people, no duties).

The installer is downloaded at test time and extracted with innoextract into a temp dir; nothing
is kept. Skipped when offline or when innoextract is missing.
"""

import hashlib
import os
import shutil
import subprocess
import urllib.request

import pytest
from contract import problems

from sp5export.sp5 import import_sp5

URL = "https://www.schichtplaner.de/archive/software/sp5setup.exe"

MAPPING = {
    "areas": [{"id": "PROD", "name": "Produktion", "color": "#2E7D32"}],
    "shifts": [{"match": "re:^[FSNT]$", "areaId": "PROD"}],
    "ignoreShifts": [],
    "groups": [{"match": "Produktion", "areaIds": ["PROD"]}],
    "ignoreGroups": ["Alle Mitarbeiter", "Verwaltung"],
    "excludeGroups": [],
    "compensatoryRestAbsences": [],
}


@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    if shutil.which("innoextract") is None:
        pytest.skip("innoextract not installed")
    work = tmp_path_factory.mktemp("vendor")
    setup = work / "sp5setup.exe"
    try:
        with urllib.request.urlopen(URL, timeout=60) as r:
            setup.write_bytes(r.read())
    except OSError as e:
        pytest.skip(f"download not possible: {e}")
    out = work / "x"
    run = subprocess.run(["innoextract", "-s", "-d", str(out), str(setup)], capture_output=True)
    folders = [p.parent for p in out.rglob("*") if p.name.upper() == "5EMPL.DBF"]
    if run.returncode or not folders:
        pytest.skip("extraction not possible")
    return folders[0]


def test_sample_database_imports(sample):
    before = {f: hashlib.sha256((sample / f).read_bytes()).hexdigest() for f in os.listdir(sample)}
    dataset, warnings = import_sp5(sample, "2027-01", MAPPING)
    after = {f: hashlib.sha256((sample / f).read_bytes()).hexdigest() for f in os.listdir(sample)}
    assert before == after
    assert problems(dataset) == []
    # Team A/B/C and Schichtleitung are subgroups of Produktion and inherit its area.
    assert 0 < len(dataset["people"]) <= 30
    assert all(p["areaIds"] == ["PROD"] and p["weeklyHours"] > 0 for p in dataset["people"])
    shifts = {s["code"]: s for s in dataset["shifts"]}
    assert set(shifts) == {"F", "S", "N", "T"}  # "B" is hidden
    assert (shifts["N"]["start"], shifts["N"]["end"], shifts["N"]["category"]) == ("22:00", "06:00", "NIGHT")
    assert {t["code"] for t in dataset["absenceTypes"]} >= {"Ur", "Kr"}
    assert dataset["periods"][0]["previousDuties"] == []
    assert not [w for w in warnings if w.startswith(("Schicht ohne", "Gruppe ohne"))]
    names = {p["lastName"] for p in dataset["people"]}
    assert not [n for n in names if n and n in "\n".join(warnings)]
