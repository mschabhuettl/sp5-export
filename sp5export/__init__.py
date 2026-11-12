"""Read-only export of a Schichtplaner5 folder into a Dispofold dataset."""

import sys
from pathlib import Path

# The vendored sp5lib lives next to this package (vendor/sp5lib).
_VENDOR = Path(__file__).resolve().parent.parent / "vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.append(str(_VENDOR))
