"""autonav_urban — GENIE-SAMTP-based autonomous urban navigation for Earth Rover Mini+.

Phase 1 skeleton: package imports, third_party path setup. Nothing runs yet.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_AUTONAV_URBAN_ROOT = _HERE.parent
_THIRD_PARTY = _AUTONAV_URBAN_ROOT / "third_party"

if _THIRD_PARTY.is_dir() and str(_THIRD_PARTY) not in sys.path:
    sys.path.insert(0, str(_THIRD_PARTY))

# Enable CPU fallback on macOS for MPS ops not yet implemented (e.g. bicubic
# upsampling used by the SAM2 HieraDet backbone). Must be set BEFORE torch is
# imported, so we set it here at package import time.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

AUTONAV_URBAN_ROOT: Path = _AUTONAV_URBAN_ROOT
THIRD_PARTY_ROOT: Path = _THIRD_PARTY
CONFIGS_ROOT: Path = _AUTONAV_URBAN_ROOT / "configs"
CALIBRATION_ROOT: Path = _AUTONAV_URBAN_ROOT / "calibration"
LOGS_ROOT: Path = _AUTONAV_URBAN_ROOT / "autonav_logs"

__all__ = [
    "AUTONAV_URBAN_ROOT",
    "THIRD_PARTY_ROOT",
    "CONFIGS_ROOT",
    "CALIBRATION_ROOT",
    "LOGS_ROOT",
]
