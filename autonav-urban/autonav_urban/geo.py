"""GPS + IMU utilities. Bridges the SDK's lat/lon/orientation with GENIE's
local (goal_x_right_m, goal_y_forward_m) body-frame convention.

Phase 3 fills these in with real implementations + tests. This is the
signature-only Phase 1 skeleton so the package imports cleanly.
"""

from __future__ import annotations

import math
from typing import Optional

_EARTH_RADIUS_M = 6_371_000.0


def gps_bearing_and_distance(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> tuple[float, float]:
    """Return (bearing_from_north_rad, distance_m) between two WGS84 points.

    Equirectangular approximation — accurate to < 0.5% at sidewalk scale.
    Bearing convention: 0 = north, +east positive, range (-pi, pi].
    """
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    mean_lat = math.radians((lat1 + lat2) / 2.0)
    dx_east = dlon * math.cos(mean_lat) * _EARTH_RADIUS_M
    dy_north = dlat * _EARTH_RADIUS_M
    bearing = math.atan2(dx_east, dy_north)
    distance = math.hypot(dx_east, dy_north)
    return bearing, distance


def compass_to_math_yaw(compass_deg: float) -> float:
    """Convert compass heading (0-360, 0=north, clockwise) to math yaw in radians.

    Math yaw convention here matches the rover's body frame used by GENIE:
    the returned angle is measured from north, positive to the east. Callers
    that need a math-standard yaw (from east, CCW) should transform further.
    """
    d = float(compass_deg) % 360.0
    return math.radians(d)


def gps_to_local_goal(
    current_lat: float,
    current_lon: float,
    current_yaw_deg: float,
    target_lat: float,
    target_lon: float,
    virtual_range_m: float = 10.0,
) -> tuple[float, float, float]:
    """Convert a GPS target into the rover's local body frame.

    Returns (goal_x_right_m, goal_y_forward_m, distance_to_target_m).
    Distance is the true Great-Circle distance; the (x, y) goal is clipped
    to `virtual_range_m` because GENIE's planner only uses direction.
    """
    bearing_from_north, distance_m = gps_bearing_and_distance(
        current_lat, current_lon, target_lat, target_lon
    )
    yaw_from_north = compass_to_math_yaw(current_yaw_deg)
    theta_body = bearing_from_north - yaw_from_north

    r = min(distance_m, float(virtual_range_m))
    goal_x_right = r * math.sin(theta_body)
    goal_y_forward = r * math.cos(theta_body)
    return goal_x_right, goal_y_forward, distance_m


def fuse_yaw(
    compass_deg: Optional[float],
    gyro_yaw_rad: Optional[float],
    gps_track_deg: Optional[float],
    speed_ms: Optional[float],
    trust_gps_speed_min: float = 0.5,
) -> Optional[float]:
    """Return best-guess yaw in degrees, fusing compass + gyro + GPS track.

    Rules of thumb:
    - Moving > 0.5 m/s → trust GPS track heading (most reliable outdoors)
    - Otherwise → trust compass (magnetometer)
    - Gyro provides continuity over short intervals

    Phase 3 will flesh this out with a proper complementary filter. For
    Phase 1 we just pass through compass if available.
    """
    if speed_ms is not None and speed_ms >= float(trust_gps_speed_min) and gps_track_deg is not None:
        return float(gps_track_deg) % 360.0
    if compass_deg is not None:
        return float(compass_deg) % 360.0
    if gyro_yaw_rad is not None:
        return math.degrees(float(gyro_yaw_rad)) % 360.0
    return None
