"""Run sp5_export.py as a separate process, the way the owner runs it."""

import json
import subprocess
import sys
from pathlib import Path

from contract import problems

ROOT = Path(__file__).resolve().parents[1]
MAPPING = ROOT / "mapping.example.json"
WARNING = "Warnung: "


def run(folder, month: str, mapping, out) -> subprocess.CompletedProcess:
    if isinstance(mapping, dict):
        path = Path(out).with_suffix(".mapping.json")
        path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        mapping = path
    return subprocess.run([sys.executable, str(ROOT / "sp5_export.py"), "--data", str(folder), "--month", month,
                           "--mapping", str(mapping), "--out", str(out)],
                          capture_output=True, text=True, encoding="utf-8", cwd=ROOT)


def export(folder, month: str, mapping, out) -> tuple[dict, list[str], subprocess.CompletedProcess]:
    """Dataset and warnings of a successful export; the dataset must pass Dispofold's import (contract.py)."""
    proc = run(folder, month, mapping, out)
    assert proc.returncode == 0, proc.stderr
    warnings = [line[len(WARNING):] for line in proc.stderr.splitlines() if line.startswith(WARNING)]
    dataset = json.loads(Path(out).read_text(encoding="utf-8"))
    assert problems(dataset) == []
    return dataset, warnings, proc
