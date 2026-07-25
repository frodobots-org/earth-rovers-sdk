"""BEV projection helpers.

Bridges the SDK's per-frame yaw (from /data.orientation) with GENIE's
`project_score_to_bev()` by composing a T_world_camera 4x4 pose from:

  T_world_camera = T_world_base(yaw) @ T_base_camera

For each perception tick we set the base at world (0, 0, 0) with the current
compass yaw — the planner is fully local, so we don't need SLAM.

Phase 5 uses PLACEHOLDER calibration constants (documented below). Phase 2
replaces them with real Mini+ intrinsics + mount transform derived from the
FrodoBots-2K helpercode notebook.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import numpy as np

from . import CALIBRATION_ROOT

# ---------------------------------------------------------------------------
# Placeholder calibration (Phase 2 replaces these).
#
# Front camera at 1024x576. Values below are Stretch-era defaults scaled to
# a 1024x576 canvas — they will produce reasonable-looking BEV but the metric
# distances will be off by 10-30%. DO NOT ship as-is for competition.
# ---------------------------------------------------------------------------
_PLACEHOLDER_K = np.array(
    [
        [750.0,   0.0, 512.0],
        [  0.0, 750.0, 288.0],
        [  0.0,   0.0,   1.0],
    ],
    dtype=np.float64,
)

# Mini+ mount: front camera roughly 15 cm above ground, pitched down ~10 deg,
# facing +y forward. Optical convention: +x image right, +y image down,
# +z forward through the image plane.
def _placeholder_T_base_camera(
    height_m: float = 0.15,
    pitch_down_deg: float = 10.0,
    forward_offset_m: float = 0.10,
) -> np.ndarray:
    """Build a plausible T_base_camera in the optical camera convention."""
    p = math.radians(float(pitch_down_deg))
    cp, sp = math.cos(p), math.sin(p)

    # Base frame: +x forward, +y left, +z up (ROS convention).
    # Optical camera frame at that mount: rotate so camera +z points forward
    # and slightly down (pitch), camera +x points to robot's right,
    # camera +y points down.
    #
    # Rotation from base -> optical:
    #   R = R_x(pitch_down) @ R_align, where R_align maps base(+x fwd, +y left, +z up)
    #   to camera(+x right, +y down, +z fwd) — that's a 90° rotation.
    #
    # Direct construction: column vectors are camera axes in base frame.
    #   camera x (right)   = base -y                       = (0, -1,  0)
    #   camera y (down)    = -base z (tilted by pitch)     = (sp,  0, -cp)
    #   camera z (forward) = base x (tilted by pitch)      = (cp,  0, -sp)
    r_base_cam = np.array(
        [
            [0.0,  sp,  cp],
            [-1.0, 0.0, 0.0],
            [0.0, -cp, -sp],
        ],
        dtype=np.float64,
    )
    t = np.array([float(forward_offset_m), 0.0, float(height_m)], dtype=np.float64)
    T = np.eye(4, dtype=np.float64)
    T[:3, :3] = r_base_cam
    T[:3, 3] = t
    return T


_PLACEHOLDER_T_BASE_CAMERA = _placeholder_T_base_camera()


def load_camera_K(path: Optional[str] = None) -> np.ndarray:
    """Load 3x3 intrinsics from calibration/mini_camera_K.npy; fall back to placeholder."""
    if path is None:
        candidate = CALIBRATION_ROOT / "mini_camera_K.npy"
    else:
        candidate = Path(path)
    if candidate.exists():
        K = np.asarray(np.load(candidate), dtype=np.float64)
        if K.shape == (3, 3):
            return K
    return _PLACEHOLDER_K.copy()


def load_T_base_camera(path: Optional[str] = None) -> np.ndarray:
    """Load 4x4 T_base_camera from calibration/; fall back to placeholder."""
    if path is None:
        candidate = CALIBRATION_ROOT / "mini_T_base_camera.npy"
    else:
        candidate = Path(path)
    if candidate.exists():
        T = np.asarray(np.load(candidate), dtype=np.float64)
        if T.shape == (4, 4):
            return T
    return _PLACEHOLDER_T_BASE_CAMERA.copy()


def build_T_world_camera(
    yaw_from_north_deg: float,
    T_base_camera: np.ndarray,
) -> np.ndarray:
    """Compose T_world_camera = T_world_base(yaw) @ T_base_camera.

    World frame: rover base placed at (0, 0, 0) with the current yaw. The
    planner is fully local; this "world" is really the instantaneous local
    frame for the current planning tick.
    """
    # ROS base: +x forward, +y left, +z up. Yaw is compass (0=north, +=east),
    # which we treat as a rotation around +z. Convert compass to math yaw
    # so that yaw=0 aligns base +x with world +x (north).
    yaw_rad = math.radians(float(yaw_from_north_deg) % 360.0)
    c, s = math.cos(yaw_rad), math.sin(yaw_rad)
    T_world_base = np.array(
        [
            [c, -s, 0.0, 0.0],
            [s,  c, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    return T_world_base @ np.asarray(T_base_camera, dtype=np.float64)


def project_frame_to_bev(
    trav_hw: np.ndarray,
    yaw_from_north_deg: float,
    K: np.ndarray,
    T_base_camera: np.ndarray,
    ground_z: float,
    bev_resolution_m_per_px: float,
    bev_forward_range_m: float,
    bev_side_range_m: float,
    max_ray_distance_m: float,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Wrapper around GENIE's project_score_to_bev with our per-frame pose."""
    from genie_path_planner.projection import project_score_to_bev

    T_world_camera = build_T_world_camera(yaw_from_north_deg, T_base_camera)
    bev, observed, stats = project_score_to_bev(
        score_map=trav_hw,
        camera_k=K,
        camera_pose=T_world_camera,
        ground_z=float(ground_z),
        bev_resolution_m_per_px=float(bev_resolution_m_per_px),
        bev_forward_range_m=float(bev_forward_range_m),
        bev_side_range_m=float(bev_side_range_m),
        max_ray_distance_m=float(max_ray_distance_m),
    )
    return bev, observed, stats
