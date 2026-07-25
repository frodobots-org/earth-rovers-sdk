from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .geometry import as_matrix, camera_planar_axes, reference_frame_from_pose


@dataclass
class BEVObservation:
    """Projected traversability maps for one RGB-D observation."""

    name: str
    camera_pose: np.ndarray
    bev_resolution_m: float
    rgb_bev: np.ndarray
    rgb_observed: np.ndarray
    depth_bev: np.ndarray | None = None
    depth_observed: np.ndarray | None = None
    rgbd_bev: np.ndarray | None = None
    rgbd_observed: np.ndarray | None = None
    robot_pose: np.ndarray | None = None
    metadata: dict[str, Any] | None = None


def logits_to_traversability(logits: np.ndarray, transform: str = "sigmoid") -> np.ndarray:
    logits_f = np.asarray(logits, dtype=np.float32)
    if transform == "sigmoid":
        x = np.clip(logits_f, -40.0, 40.0)
        return (1.0 / (1.0 + np.exp(-x))).astype(np.float32)
    if transform == "minmax":
        finite = np.isfinite(logits_f)
        out = np.zeros_like(logits_f, dtype=np.float32)
        if np.any(finite):
            lo = float(np.min(logits_f[finite]))
            hi = float(np.max(logits_f[finite]))
            out[finite] = ((logits_f[finite] - lo) / max(1e-8, hi - lo)).astype(np.float32)
        return out
    if transform in {"none", "identity"}:
        return logits_f.astype(np.float32)
    raise ValueError(f"Unknown score transform: {transform}")


def project_score_to_bev(
    score_map: np.ndarray,
    camera_k: np.ndarray,
    camera_pose: np.ndarray,
    ground_z: float,
    bev_resolution_m_per_px: float,
    bev_forward_range_m: float,
    bev_side_range_m: float,
    max_ray_distance_m: float,
) -> tuple[np.ndarray, np.ndarray, dict[str, float]]:
    """Project an image-space traversability score onto a ground-plane BEV map."""
    if score_map.ndim != 2:
        raise ValueError("score_map must be HxW")
    camera_k = as_matrix(camera_k, (3, 3), "camera_K")
    camera_pose = as_matrix(camera_pose, (4, 4), "camera_pose")
    if bev_resolution_m_per_px <= 0:
        raise ValueError("bev_resolution_m_per_px must be > 0")
    if bev_forward_range_m <= 0:
        raise ValueError("bev_forward_range_m must be > 0")
    if bev_side_range_m <= 0:
        raise ValueError("bev_side_range_m must be > 0")

    bev_h = max(1, int(np.ceil(float(bev_forward_range_m) / float(bev_resolution_m_per_px))))
    bev_w = max(1, int(np.ceil((2.0 * float(bev_side_range_m)) / float(bev_resolution_m_per_px))))

    ys, xs = np.indices(score_map.shape, dtype=np.float64)
    xs = xs.reshape(-1)
    ys = ys.reshape(-1)
    scores = np.asarray(score_map, dtype=np.float32).reshape(-1)
    finite = np.isfinite(scores)
    xs = xs[finite]
    ys = ys[finite]
    scores = scores[finite]
    if xs.size == 0:
        bev = np.full((bev_h, bev_w), -1.0, dtype=np.float32)
        observed = np.zeros((bev_h, bev_w), dtype=np.uint8)
        return bev, observed, {
            "input_pixels": float(score_map.size),
            "valid_score_pixels": 0.0,
            "projected_ground_points": 0.0,
            "bev_observed_cells": 0.0,
        }

    fx = float(camera_k[0, 0])
    fy = float(camera_k[1, 1])
    cx = float(camera_k[0, 2])
    cy = float(camera_k[1, 2])
    if fx <= 0.0 or fy <= 0.0:
        raise ValueError(f"Invalid camera intrinsics fx/fy: fx={fx}, fy={fy}")

    dirs_cam = np.empty((xs.size, 3), dtype=np.float64)
    dirs_cam[:, 0] = (xs - cx) / fx
    dirs_cam[:, 1] = (ys - cy) / fy
    dirs_cam[:, 2] = 1.0

    r_world_cam = camera_pose[:3, :3]
    t_world_cam = camera_pose[:3, 3]
    dirs_world = dirs_cam @ r_world_cam.T

    dz = dirs_world[:, 2]
    valid = np.abs(dz) > 1e-8
    ray_scale = np.zeros_like(dz, dtype=np.float64)
    ray_scale[valid] = (float(ground_z) - float(t_world_cam[2])) / dz[valid]
    valid &= ray_scale > 0.0

    points_world = t_world_cam[None, :] + dirs_world * ray_scale[:, None]
    if max_ray_distance_m > 0.0:
        dist_xy = np.linalg.norm(points_world[:, :2] - t_world_cam[None, :2], axis=1)
        valid &= dist_xy <= float(max_ray_distance_m)

    forward_xy, left_xy = camera_planar_axes(camera_pose)
    rel_xy = points_world[:, :2] - t_world_cam[None, :2]
    forward_m = rel_xy @ forward_xy
    left_m = rel_xy @ left_xy

    valid &= forward_m >= 0.0
    valid &= forward_m < float(bev_forward_range_m)
    valid &= np.abs(left_m) < float(bev_side_range_m)
    if not np.any(valid):
        bev = np.full((bev_h, bev_w), -1.0, dtype=np.float32)
        observed = np.zeros((bev_h, bev_w), dtype=np.uint8)
        return bev, observed, {
            "input_pixels": float(score_map.size),
            "valid_score_pixels": float(xs.size),
            "projected_ground_points": 0.0,
            "bev_observed_cells": 0.0,
        }

    forward_valid = forward_m[valid]
    left_valid = left_m[valid]
    score_valid = scores[valid]
    rows = bev_h - 1 - np.floor(forward_valid / float(bev_resolution_m_per_px)).astype(np.int32)
    cols = (bev_w // 2) - np.floor(left_valid / float(bev_resolution_m_per_px)).astype(np.int32)
    in_bounds = (rows >= 0) & (rows < bev_h) & (cols >= 0) & (cols < bev_w)
    rows = rows[in_bounds]
    cols = cols[in_bounds]
    score_valid = score_valid[in_bounds]

    flat_size = bev_h * bev_w
    flat_idx = rows.astype(np.int64) * bev_w + cols.astype(np.int64)
    sum_flat = np.bincount(flat_idx, weights=score_valid.astype(np.float64), minlength=flat_size)
    cnt_flat = np.bincount(flat_idx, minlength=flat_size)
    observed_flat = cnt_flat > 0

    bev_flat = np.full(flat_size, -1.0, dtype=np.float32)
    bev_flat[observed_flat] = (sum_flat[observed_flat] / cnt_flat[observed_flat]).astype(np.float32)
    observed = observed_flat.reshape(bev_h, bev_w).astype(np.uint8)
    return bev_flat.reshape(bev_h, bev_w), observed, {
        "input_pixels": float(score_map.size),
        "valid_score_pixels": float(xs.size),
        "projected_ground_points": float(np.count_nonzero(valid)),
        "bev_observed_cells": float(np.count_nonzero(observed_flat)),
    }


def depth_to_bev_height_and_traversability(
    depth_m: np.ndarray,
    camera_k: np.ndarray,
    camera_pose: np.ndarray,
    ground_z: float,
    reliable_depth_m: float,
    min_depth_m: float,
    obstacle_height_thresh_m: float,
    bev_resolution_m_per_px: float,
    bev_forward_range_m: float,
    bev_side_range_m: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, float]]:
    """Project depth points into BEV and convert obstacle height to traversability."""
    if depth_m.ndim != 2:
        raise ValueError("depth_m must be HxW")
    camera_k = as_matrix(camera_k, (3, 3), "camera_K")
    camera_pose = as_matrix(camera_pose, (4, 4), "camera_pose")
    if reliable_depth_m <= 0.0:
        raise ValueError("reliable_depth_m must be > 0")
    if obstacle_height_thresh_m <= 0.0:
        raise ValueError("obstacle_height_thresh_m must be > 0")
    if bev_resolution_m_per_px <= 0.0:
        raise ValueError("bev_resolution_m_per_px must be > 0")

    bev_h = max(1, int(np.ceil(float(bev_forward_range_m) / float(bev_resolution_m_per_px))))
    bev_w = max(1, int(np.ceil((2.0 * float(bev_side_range_m)) / float(bev_resolution_m_per_px))))

    h, w = depth_m.shape
    fx = float(camera_k[0, 0])
    fy = float(camera_k[1, 1])
    cx = float(camera_k[0, 2])
    cy = float(camera_k[1, 2])
    if fx <= 0.0 or fy <= 0.0:
        raise ValueError(f"Invalid camera intrinsics fx/fy: fx={fx}, fy={fy}")

    ys, xs = np.indices((h, w), dtype=np.float64)
    z = np.asarray(depth_m, dtype=np.float64)
    valid = np.isfinite(z)
    valid &= z >= float(min_depth_m)
    valid &= z <= float(reliable_depth_m)

    if not np.any(valid):
        height_map = np.full((bev_h, bev_w), np.nan, dtype=np.float32)
        trav_map = np.full((bev_h, bev_w), -1.0, dtype=np.float32)
        observed_mask = np.zeros((bev_h, bev_w), dtype=bool)
        return height_map, trav_map, observed_mask, {
            "input_pixels": float(h * w),
            "valid_depth_pixels": 0.0,
            "bev_observed_cells": 0.0,
            "bev_non_traversable_cells": 0.0,
        }

    xs_v = xs[valid]
    ys_v = ys[valid]
    z_v = z[valid]
    x_cam = (xs_v - cx) * z_v / fx
    y_cam = (ys_v - cy) * z_v / fy
    points_cam = np.stack([x_cam, y_cam, z_v], axis=1)

    points_world = points_cam @ camera_pose[:3, :3].T + camera_pose[:3, 3][None, :]
    height_m = np.maximum(points_world[:, 2] - float(ground_z), 0.0)

    forward_xy, left_xy = camera_planar_axes(camera_pose)
    rel_xy = points_world[:, :2] - camera_pose[:2, 3][None, :]
    forward_m = rel_xy @ forward_xy
    left_m = rel_xy @ left_xy
    valid_bev = forward_m >= 0.0
    valid_bev &= forward_m < float(bev_forward_range_m)
    valid_bev &= np.abs(left_m) < float(bev_side_range_m)

    if not np.any(valid_bev):
        height_map = np.full((bev_h, bev_w), np.nan, dtype=np.float32)
        trav_map = np.full((bev_h, bev_w), -1.0, dtype=np.float32)
        observed_mask = np.zeros((bev_h, bev_w), dtype=bool)
        return height_map, trav_map, observed_mask, {
            "input_pixels": float(h * w),
            "valid_depth_pixels": float(z_v.size),
            "bev_observed_cells": 0.0,
            "bev_non_traversable_cells": 0.0,
        }

    forward_v = forward_m[valid_bev]
    left_v = left_m[valid_bev]
    height_v = height_m[valid_bev]
    rows = bev_h - 1 - np.floor(forward_v / float(bev_resolution_m_per_px)).astype(np.int32)
    cols = (bev_w // 2) - np.floor(left_v / float(bev_resolution_m_per_px)).astype(np.int32)
    in_bounds = (rows >= 0) & (rows < bev_h) & (cols >= 0) & (cols < bev_w)
    rows = rows[in_bounds]
    cols = cols[in_bounds]
    height_v = height_v[in_bounds]

    flat_size = bev_h * bev_w
    flat_idx = rows.astype(np.int64) * bev_w + cols.astype(np.int64)
    max_height_flat = np.full(flat_size, -np.inf, dtype=np.float32)
    np.maximum.at(max_height_flat, flat_idx, height_v.astype(np.float32))

    observed_flat = max_height_flat > -np.inf
    height_map = max_height_flat.reshape(bev_h, bev_w)
    observed = observed_flat.reshape(bev_h, bev_w)
    height_map[~observed] = np.nan

    trav_map = np.full((bev_h, bev_w), -1.0, dtype=np.float32)
    h_obs = height_map[observed]
    h_clip = np.clip(h_obs, 0.0, float(obstacle_height_thresh_m))
    score = 1.0 - (h_clip / float(obstacle_height_thresh_m))
    score = np.clip(score, 0.0, 1.0)
    score[h_obs >= float(obstacle_height_thresh_m)] = 0.0
    trav_map[observed] = score.astype(np.float32)

    return height_map, trav_map, observed, {
        "input_pixels": float(h * w),
        "valid_depth_pixels": float(z_v.size),
        "bev_observed_cells": float(np.count_nonzero(observed)),
        "bev_non_traversable_cells": float(np.count_nonzero(observed & (trav_map <= 1e-6))),
    }


def blend_modalities(
    rgb_bev: np.ndarray,
    rgb_observed: np.ndarray,
    depth_bev: np.ndarray,
    depth_observed: np.ndarray,
    rgb_weight: float,
    depth_weight: float,
    require_depth: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    if rgb_bev.shape != depth_bev.shape:
        raise ValueError(f"rgb_bev and depth_bev shape mismatch: {rgb_bev.shape} vs {depth_bev.shape}")
    rgb_known = rgb_observed.astype(bool) & np.isfinite(rgb_bev) & (rgb_bev >= 0.0)
    depth_known = depth_observed.astype(bool) & np.isfinite(depth_bev) & (depth_bev >= 0.0)

    out = np.full(rgb_bev.shape, -1.0, dtype=np.float32)
    only_rgb = rgb_known & (~depth_known)
    only_depth = depth_known & (~rgb_known)
    both = rgb_known & depth_known
    if not require_depth:
        out[only_rgb] = rgb_bev[only_rgb].astype(np.float32)
    out[only_depth] = depth_bev[only_depth].astype(np.float32)
    denom = float(rgb_weight) + float(depth_weight)
    if np.any(both):
        if denom <= 1e-8:
            out[both] = (0.5 * (rgb_bev[both] + depth_bev[both])).astype(np.float32)
        else:
            out[both] = (
                (float(rgb_weight) * rgb_bev[both] + float(depth_weight) * depth_bev[both]) / denom
            ).astype(np.float32)
    observed = (np.isfinite(out) & (out >= 0.0)).astype(np.uint8)
    return out, observed


def select_observation_map(record: BEVObservation, mode: str) -> tuple[np.ndarray, np.ndarray]:
    mode_l = str(mode).lower()
    if mode_l == "rgb":
        return record.rgb_bev, record.rgb_observed
    if mode_l == "depth":
        if record.depth_bev is None or record.depth_observed is None:
            raise ValueError(f"Observation {record.name!r} has no depth BEV map")
        return record.depth_bev, record.depth_observed
    if mode_l == "rgbd":
        if record.rgbd_bev is None or record.rgbd_observed is None:
            raise ValueError(f"Observation {record.name!r} has no RGB-D BEV map")
        return record.rgbd_bev, record.rgbd_observed
    raise ValueError(f"Unsupported BEV mode: {mode}")


def observation_world_points_from_map(
    camera_pose: np.ndarray,
    bev_score: np.ndarray,
    bev_observed: np.ndarray,
    bev_resolution_m: float,
) -> tuple[np.ndarray, np.ndarray]:
    observed = bev_observed.astype(bool) & np.isfinite(bev_score) & (bev_score >= 0.0)
    rows, cols = np.nonzero(observed)
    if rows.size == 0:
        return np.empty((0, 2), dtype=np.float64), np.empty((0,), dtype=np.float32)

    bev_h, bev_w = bev_score.shape
    res = float(bev_resolution_m)
    forward = ((bev_h - 1 - rows).astype(np.float64) + 0.5) * res
    left = ((bev_w // 2 - cols).astype(np.float64) + 0.5) * res

    camera_pose = as_matrix(camera_pose, (4, 4), "camera_pose")
    cam_xy = camera_pose[:2, 3].astype(np.float64)
    forward_xy, left_xy = camera_planar_axes(camera_pose)
    world_xy = cam_xy[None, :] + forward[:, None] * forward_xy[None, :] + left[:, None] * left_xy[None, :]
    return world_xy, bev_score[rows, cols].astype(np.float32)


def fuse_bev_observations(
    records: list[BEVObservation],
    mode: str,
    reference_pose: np.ndarray,
    reference_frame: str,
    bev_resolution_m: float,
    bev_forward_range_m: float,
    bev_side_range_m: float,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Fuse observation BEVs into a fixed local frame around the reference pose."""
    if not records:
        raise ValueError("records must not be empty")
    res = float(bev_resolution_m)
    if res <= 0.0:
        raise ValueError("bev_resolution_m must be > 0")
    bev_h = max(1, int(np.ceil(float(bev_forward_range_m) / res)))
    bev_w = max(1, int(np.ceil((2.0 * float(bev_side_range_m)) / res)))
    ref_xy, ref_forward, ref_left = reference_frame_from_pose(reference_pose, reference_frame)

    all_world_xy: list[np.ndarray] = []
    all_scores: list[np.ndarray] = []
    selected_names: list[str] = []
    for rec in records:
        score_map, obs_map = select_observation_map(rec, mode)
        world_xy, scores = observation_world_points_from_map(
            camera_pose=rec.camera_pose,
            bev_score=score_map,
            bev_observed=obs_map,
            bev_resolution_m=rec.bev_resolution_m,
        )
        if world_xy.shape[0] == 0:
            continue
        all_world_xy.append(world_xy)
        all_scores.append(scores)
        selected_names.append(rec.name)

    if not all_world_xy:
        fused = np.full((bev_h, bev_w), -1.0, dtype=np.float32)
        observed = np.zeros((bev_h, bev_w), dtype=np.uint8)
        return fused, observed, {
            "mode": mode,
            "selected_observations": selected_names,
            "fusion_resolution_m": res,
            "reference_frame": reference_frame,
            "cell_count": 0,
        }

    world_xy = np.concatenate(all_world_xy, axis=0)
    scores = np.concatenate(all_scores, axis=0).astype(np.float32)
    rel = world_xy - ref_xy[None, :]
    forward = rel @ ref_forward
    left = rel @ ref_left
    valid = forward >= 0.0
    valid &= forward < float(bev_forward_range_m)
    valid &= np.abs(left) < float(bev_side_range_m)
    if not np.any(valid):
        fused = np.full((bev_h, bev_w), -1.0, dtype=np.float32)
        observed = np.zeros((bev_h, bev_w), dtype=np.uint8)
        return fused, observed, {
            "mode": mode,
            "selected_observations": selected_names,
            "fusion_resolution_m": res,
            "reference_frame": reference_frame,
            "cell_count": 0,
        }

    forward_v = forward[valid]
    left_v = left[valid]
    scores_v = scores[valid]
    rows = bev_h - 1 - np.floor(forward_v / res).astype(np.int32)
    cols = (bev_w // 2) - np.floor(left_v / res).astype(np.int32)
    in_bounds = (rows >= 0) & (rows < bev_h) & (cols >= 0) & (cols < bev_w)
    rows = rows[in_bounds]
    cols = cols[in_bounds]
    scores_v = scores_v[in_bounds]

    flat_size = bev_h * bev_w
    flat_idx = rows.astype(np.int64) * bev_w + cols.astype(np.int64)
    sum_flat = np.bincount(flat_idx, weights=scores_v.astype(np.float64), minlength=flat_size)
    cnt_flat = np.bincount(flat_idx, minlength=flat_size)
    observed_flat = cnt_flat > 0
    fused_flat = np.full(flat_size, -1.0, dtype=np.float32)
    fused_flat[observed_flat] = (sum_flat[observed_flat] / cnt_flat[observed_flat]).astype(np.float32)
    observed = observed_flat.reshape(bev_h, bev_w).astype(np.uint8)
    return fused_flat.reshape(bev_h, bev_w), observed, {
        "mode": mode,
        "selected_observations": selected_names,
        "fusion_resolution_m": res,
        "reference_frame": reference_frame,
        "cell_count": int(np.count_nonzero(observed_flat)),
        "input_projected_points": int(world_xy.shape[0]),
        "points_inside_reference_window": int(np.count_nonzero(valid)),
    }


def traversability_vis(score_map: np.ndarray, draw_robot_marker: bool = True) -> np.ndarray:
    """Render traversability: green high, red low, black unknown."""
    h, w = score_map.shape
    vis = np.zeros((h, w, 3), dtype=np.uint8)
    known = np.isfinite(score_map) & (score_map >= 0.0)
    if np.any(known):
        s = np.clip(score_map[known], 0.0, 1.0)
        vis[known] = np.stack(
            [
                ((1.0 - s) * 255.0).astype(np.uint8),
                (s * 255.0).astype(np.uint8),
                np.zeros_like(s, dtype=np.uint8),
            ],
            axis=1,
        )
    if draw_robot_marker:
        center_col = w // 2
        center_row = h - 1
        vis[max(0, center_row - 2) : min(h, center_row + 3), max(0, center_col - 2) : min(w, center_col + 3)] = (
            255,
            255,
            255,
        )
        arrow_top = max(0, center_row - 28)
        vis[arrow_top : center_row + 1, max(0, center_col - 1) : min(w, center_col + 1)] = (255, 255, 255)
    return vis
