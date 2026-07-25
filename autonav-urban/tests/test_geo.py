"""Unit tests for autonav_urban.geo. Phase 3 fleshes these out."""

from __future__ import annotations

import math
import sys
from pathlib import Path

# Add autonav-urban to sys.path so `import autonav_urban` works when running
# `pytest tests/` from inside the autonav-urban/ folder.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from autonav_urban.geo import (  # noqa: E402
    compass_to_math_yaw,
    fuse_yaw,
    gps_bearing_and_distance,
    gps_to_local_goal,
)


def test_bearing_northward_is_zero():
    b, d = gps_bearing_and_distance(30.0, 114.0, 30.001, 114.0)
    assert abs(b) < math.radians(1.0)
    assert 100.0 < d < 120.0


def test_bearing_eastward_is_pi_over_2():
    b, d = gps_bearing_and_distance(30.0, 114.0, 30.0, 114.001)
    assert abs(b - math.pi / 2) < math.radians(1.0)
    assert 90.0 < d < 110.0


def test_compass_to_math_yaw_normalizes():
    assert abs(compass_to_math_yaw(0.0) - 0.0) < 1e-9
    assert abs(compass_to_math_yaw(360.0) - 0.0) < 1e-9
    assert abs(compass_to_math_yaw(90.0) - math.pi / 2) < 1e-9


def test_local_goal_forward_when_target_ahead_and_facing_it():
    # Rover at (30, 114) facing north (yaw=0). Target 100 m due north.
    gx, gy, dist = gps_to_local_goal(30.0, 114.0, 0.0, 30.001, 114.0)
    assert abs(gx) < 0.5              # no lateral component
    assert gy > 0.5                    # positive forward
    assert dist > 100.0


def test_local_goal_right_when_target_east_of_north_facing_rover():
    # Rover at (30, 114) facing north. Target 100 m due east → hard right.
    gx, gy, _ = gps_to_local_goal(30.0, 114.0, 0.0, 30.0, 114.001)
    assert gx > 0.5
    assert abs(gy) < 0.5


def test_fuse_yaw_trusts_gps_track_when_moving():
    assert fuse_yaw(compass_deg=10.0, gyro_yaw_rad=None, gps_track_deg=45.0, speed_ms=1.0) == 45.0


def test_fuse_yaw_falls_back_to_compass_when_stationary():
    assert fuse_yaw(compass_deg=10.0, gyro_yaw_rad=None, gps_track_deg=45.0, speed_ms=0.0) == 10.0
