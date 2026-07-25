"""Offline end-to-end smoke test.

Loads a saved test frame + fake telemetry, runs one perception tick, and
checks that a BEV comes out. Requires the SAM-TP checkpoint to be present.
Slow (12 s cold) so tagged 'slow' — skip in normal CI:

    pytest tests/test_perception_smoke.py -v --run-slow
"""

from __future__ import annotations

import base64
import io
import sys
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import autonav_urban  # noqa: E402  (initialises sys.path for third_party)
from autonav_urban.config import UrbanRuntimeConfig  # noqa: E402
from autonav_urban.runtime import build_runtime  # noqa: E402


TEST_FRAME = Path(
    "/Users/dev/Documents/GENIE-SAMTP-master/stretch_example/stretch_obs/rgb.png"
)
CHECKPOINT = _ROOT / "third_party" / "sam2_ckpt" / "checkpoint_2.pt"


def _frame_b64() -> str:
    im = Image.open(TEST_FRAME).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii")


@pytest.mark.skipif(not CHECKPOINT.exists(), reason="checkpoint_2.pt not downloaded")
@pytest.mark.skipif(not TEST_FRAME.exists(), reason="test frame not present")
def test_perception_produces_bev(tmp_path):
    import asyncio

    frame_b64_str = _frame_b64()

    async def fake_frame(view: str) -> str:
        return frame_b64_str

    async def fake_data() -> dict:
        # Stationary, facing due north.
        return {
            "latitude": 40.4406,
            "longitude": -79.9959,
            "orientation": 0.0,
            "speed": 0.0,
            "battery": 90,
            "gps_signal": 25.0,
            "timestamp": time.time(),
        }

    cfg = UrbanRuntimeConfig(
        telemetry_hz=5.0,
        perception_target_hz=1.0,      # single pass is enough
        max_error_streak=1,
    )

    async def run_once():
        rt = build_runtime(cfg, get_frame_base64=fake_frame, get_data=fake_data)
        await rt.start()
        # Wait until a BEV is cached or timeout
        for _ in range(30):
            if rt.state.last_bev is not None:
                break
            await asyncio.sleep(0.5)
        assert rt.state.last_bev is not None, "no BEV was produced"
        png_bytes = rt.latest_bev_png()
        assert png_bytes is not None
        assert len(png_bytes) > 1000
        # Also save to a known location so the human can eyeball it
        out = _ROOT / "autonav_logs" / "perception_smoke_bev.png"
        out.write_bytes(png_bytes)
        await rt.stop("test_done")
        status = rt.status_dict()          # capture AFTER stop
        return status, out

    status, out = asyncio.run(run_once())
    assert status["running"] is False
    assert status["device"] in {"mps", "cuda", "cpu"}
    print(f"BEV saved -> {out}")
    print(f"status: {status}")
