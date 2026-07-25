from __future__ import annotations

import math
from typing import Any

import numpy as np


def as_matrix(value: Any, shape: tuple[int, int], name: str) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64)
    if arr.shape == shape:
        return arr
    if arr.size == shape[0] * shape[1]:
        return arr.reshape(shape)
    raise ValueError(f"{name} must have shape {shape}, got {arr.shape}")


def pose_xy_yaw_to_matrix(xy_yaw: Any) -> np.ndarray:
    """Build T_world_base from [x, y, yaw_rad] using ROS base axes.

    The base frame convention is x forward, y left, z up. The resulting matrix
    maps points from base coordinates into a world/odom frame with z up.
    """
    vals = np.asarray(xy_yaw, dtype=np.float64).reshape(-1)
    if vals.size != 3:
        raise ValueError(f"robot_pose_xy_yaw must contain [x, y, yaw_rad], got {vals}")
    x, y, yaw = float(vals[0]), float(vals[1]), float(vals[2])
    c = math.cos(yaw)
    s = math.sin(yaw)
    pose = np.eye(4, dtype=np.float64)
    pose[:3, :3] = np.array(
        [
            [c, -s, 0.0],
            [s, c, 0.0],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    pose[:3, 3] = np.array([x, y, 0.0], dtype=np.float64)
    return pose


def normalize_xy(vec: np.ndarray, fallback: np.ndarray) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float64).reshape(2)
    norm = float(np.linalg.norm(v))
    if norm > 1e-8:
        return v / norm
    f = np.asarray(fallback, dtype=np.float64).reshape(2)
    f_norm = float(np.linalg.norm(f))
    if f_norm > 1e-8:
        return f / f_norm
    return np.array([1.0, 0.0], dtype=np.float64)


def camera_planar_axes(camera_pose: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return planar forward/left unit vectors for an optical camera pose."""
    pose = as_matrix(camera_pose, (4, 4), "camera_pose")
    r_world_cam = pose[:3, :3]
    forward_xy = r_world_cam[:, 2][:2].astype(np.float64)
    forward_xy = normalize_xy(forward_xy, r_world_cam[:, 0][:2].astype(np.float64))
    left_xy = np.array([-forward_xy[1], forward_xy[0]], dtype=np.float64)
    return forward_xy, left_xy


def base_planar_axes(base_pose: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return planar forward/left unit vectors for a ROS base pose."""
    pose = as_matrix(base_pose, (4, 4), "base_pose")
    r_world_base = pose[:3, :3]
    forward_xy = normalize_xy(r_world_base[:, 0][:2], np.array([1.0, 0.0], dtype=np.float64))
    left_xy = normalize_xy(r_world_base[:, 1][:2], np.array([0.0, 1.0], dtype=np.float64))
    return forward_xy, left_xy


def reference_frame_from_pose(
    pose: np.ndarray,
    frame: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return origin, forward, left for a reference pose.

    frame="base" treats pose as T_world_base with ROS base axes. frame="camera"
    treats pose as T_world_camera for an optical camera frame.
    """
    pose = as_matrix(pose, (4, 4), "reference_pose")
    origin_xy = pose[:2, 3].astype(np.float64)
    frame_l = str(frame).lower()
    if frame_l == "base":
        forward_xy, left_xy = base_planar_axes(pose)
    elif frame_l == "camera":
        forward_xy, left_xy = camera_planar_axes(pose)
    else:
        raise ValueError(f"reference frame must be 'base' or 'camera', got {frame!r}")
    return origin_xy, forward_xy, left_xy


def goal_xy_to_bev_pixel(
    goal_x_right_m: float,
    goal_y_forward_m: float,
    bev_shape: tuple[int, int],
    resolution_m: float,
) -> tuple[int, int]:
    """Convert goal [x_right, y_forward] into local BEV row/col."""
    h, w = int(bev_shape[0]), int(bev_shape[1])
    res = float(resolution_m)
    if res <= 0.0:
        raise ValueError("resolution_m must be > 0")
    row = h - 1 - int(np.floor(float(goal_y_forward_m) / res))
    col = (w // 2) + int(np.floor(float(goal_x_right_m) / res))
    return int(np.clip(row, 0, h - 1)), int(np.clip(col, 0, w - 1))


def bev_pixel_to_xy(
    rows: np.ndarray,
    cols: np.ndarray,
    bev_shape: tuple[int, int],
    resolution_m: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Convert local BEV row/col arrays into [x_right, y_forward] meters."""
    h, w = int(bev_shape[0]), int(bev_shape[1])
    r = np.asarray(rows, dtype=np.float64)
    c = np.asarray(cols, dtype=np.float64)
    y_forward = ((float(h - 1) - r) + 0.5) * float(resolution_m)
    x_right = ((c - float(w // 2)) + 0.5) * float(resolution_m)
    return x_right.astype(np.float32), y_forward.astype(np.float32)
