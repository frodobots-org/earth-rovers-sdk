from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .costs import compute_paths_costs, pick_final_path
from .geometry import bev_pixel_to_xy, goal_xy_to_bev_pixel
from .path_sampling import sample_paths_polynomial
from .path_selection import (
    adaptive_kmeans,
    filter_paths_with_high_costs,
    merge_centroids_by_angle,
    select_best_group_by_closest_path_angle,
)


@dataclass
class PlannerConfig:
    grid_size: int = 240
    unknown_cost: float = 0.2
    smooth_kernel: int = 3
    num_goals: int = 30
    num_mid_points_per_goal: int = 20
    path_num_samples: int = 100
    footprint_px: int = 18
    threshold_cost: float = 0.50
    threshold_points_ratio: float = 0.05
    number_of_points_to_filter: int = 60
    alpha: float = 1.0
    best_k: int = 12
    use_clustering: bool = False
    max_clusters: int = 4
    cluster_angle_threshold_deg: float = 40.0
    random_seed: int | None = 42
    include_goal_in_path_bank: bool = False
    include_random_goals: bool = True


@dataclass
class PlannedPath:
    visualization: np.ndarray
    cost_map: np.ndarray
    known_mask: np.ndarray
    final_path_pixels: np.ndarray
    final_path_xy_m: np.ndarray
    candidate_paths: list[np.ndarray] = field(default_factory=list)
    filtered_paths: list[np.ndarray] = field(default_factory=list)
    selected_paths: list[np.ndarray] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def traversability_to_cost(
    trav: np.ndarray,
    unknown_cost: float,
    confidence: np.ndarray | None = None,
) -> np.ndarray:
    t = np.asarray(trav, dtype=np.float32)
    out = np.full(t.shape, float(unknown_cost), dtype=np.float32)
    known = np.isfinite(t) & (t >= 0.0)
    known_cost = 1.0 - np.clip(t[known], 0.0, 1.0)
    if confidence is not None:
        conf = np.asarray(confidence, dtype=np.float32)
        if conf.shape != t.shape:
            raise ValueError(f"confidence shape {conf.shape} does not match traversability {t.shape}")
        conf_known = np.clip(conf[known], 0.0, 1.0)
        known_cost = float(unknown_cost) + conf_known * (known_cost - float(unknown_cost))
    out[known] = known_cost
    return np.clip(out, 0.0, 1.0)


def cost_to_vis(cost: np.ndarray, known_mask: np.ndarray | None = None) -> np.ndarray:
    trav = 1.0 - np.clip(cost, 0.0, 1.0)
    vis = np.stack(
        [
            ((1.0 - trav) * 255.0).astype(np.uint8),
            (trav * 255.0).astype(np.uint8),
            np.zeros_like(trav, dtype=np.uint8),
        ],
        axis=2,
    )
    if known_mask is not None:
        known = np.asarray(known_mask, dtype=bool)
        if known.shape != cost.shape:
            raise ValueError(f"known_mask shape {known.shape} does not match cost {cost.shape}")
        vis[~known] = 0
    return vis


def _resize_float_map(cost_map: np.ndarray, grid_size: int) -> np.ndarray:
    return np.asarray(
        Image.fromarray(np.asarray(cost_map, dtype=np.float32), mode="F").resize(
            (int(grid_size), int(grid_size)),
            resample=Image.BILINEAR,
        ),
        dtype=np.float32,
    )


def _resize_mask(mask: np.ndarray, grid_size: int) -> np.ndarray:
    arr = (np.asarray(mask, dtype=bool).astype(np.uint8) * 255)
    resized = np.asarray(
        Image.fromarray(arr, mode="L").resize((int(grid_size), int(grid_size)), resample=Image.NEAREST),
        dtype=np.uint8,
    )
    return resized > 0


def _resize_pixel(pixel_rc: tuple[int, int], src_shape: tuple[int, int], grid_size: int) -> tuple[int, int]:
    h0, w0 = int(src_shape[0]), int(src_shape[1])
    n = int(grid_size)
    if h0 <= 1:
        out_r = n - 1
    else:
        out_r = int(round(float(pixel_rc[0]) * float(n - 1) / float(h0 - 1)))
    if w0 <= 1:
        out_c = n // 2
    else:
        out_c = int(round(float(pixel_rc[1]) * float(n - 1) / float(w0 - 1)))
    return int(np.clip(out_r, 0, n - 1)), int(np.clip(out_c, 0, n - 1))


def _planner_path_to_bev_path(
    path_rc: np.ndarray,
    src_shape: tuple[int, int],
    grid_size: int,
) -> np.ndarray:
    h0, w0 = int(src_shape[0]), int(src_shape[1])
    n = int(grid_size)
    path = np.asarray(path_rc, dtype=np.float32)
    rows = path[:, 0]
    cols = path[:, 1]
    if n <= 1 or h0 <= 1:
        bev_rows = np.full(rows.shape, h0 - 1, dtype=np.float32)
    else:
        bev_rows = rows * (float(h0 - 1) / float(n - 1))
    if n <= 1 or w0 <= 1:
        bev_cols = np.full(cols.shape, w0 // 2, dtype=np.float32)
    else:
        bev_cols = cols * (float(w0 - 1) / float(n - 1))
    return np.stack([bev_rows, bev_cols], axis=1).astype(np.float32)


def _draw_plan_overlay(
    base_vis: np.ndarray,
    all_paths: list[np.ndarray],
    selected_paths: list[np.ndarray],
    final_rows: np.ndarray,
    final_cols: np.ndarray,
    start_rc: tuple[int, int],
    goal_rc: tuple[int, int],
) -> np.ndarray:
    img = Image.fromarray(base_vis.astype(np.uint8), mode="RGB").convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    if len(all_paths) > 0:
        stride = max(1, len(all_paths) // 80)
        for path in all_paths[::stride]:
            pts = np.round(np.stack([path[:, 1], path[:, 0]], axis=1)).astype(np.int32)
            draw.line([(int(c), int(r)) for c, r in pts], fill=(100, 100, 100, 70), width=1)

    if len(selected_paths) > 0:
        stride = max(1, len(selected_paths) // 120)
        for path in selected_paths[::stride]:
            pts = np.round(np.stack([path[:, 1], path[:, 0]], axis=1)).astype(np.int32)
            draw.line([(int(c), int(r)) for c, r in pts], fill=(255, 180, 40, 130), width=1)

    if final_rows.size > 0 and final_cols.size > 0:
        pts = np.round(np.stack([final_cols, final_rows], axis=1)).astype(np.int32)
        draw.line([(int(c), int(r)) for c, r in pts], fill=(255, 255, 255, 230), width=2)

    sr, sc = int(start_rc[0]), int(start_rc[1])
    draw.ellipse((sc - 3, sr - 3, sc + 3, sr + 3), fill=(255, 255, 255, 240), outline=(255, 255, 255, 240))

    gr, gc = int(goal_rc[0]), int(goal_rc[1])
    draw.ellipse((gc - 4, gr - 4, gc + 4, gr + 4), fill=(40, 220, 255, 240), outline=(40, 220, 255, 240))

    return np.asarray(Image.alpha_composite(img, overlay).convert("RGB"), dtype=np.uint8)


def _smooth_cost(cost_map: np.ndarray, smooth_kernel: int) -> np.ndarray:
    if int(smooth_kernel) <= 1:
        return cost_map
    k = int(smooth_kernel)
    if k % 2 == 0:
        k += 1
    radius = max(0.5, float(k) / 2.0)
    cost_u8 = (np.clip(cost_map, 0.0, 1.0) * 255.0).astype(np.uint8)
    cost_blur_u8 = np.asarray(
        Image.fromarray(cost_u8, mode="L").filter(ImageFilter.GaussianBlur(radius=radius)),
        dtype=np.uint8,
    )
    return cost_blur_u8.astype(np.float32) / 255.0


def _validate_config(config: PlannerConfig) -> None:
    if int(config.grid_size) <= 16:
        raise ValueError("planner grid_size must be > 16")
    if int(config.num_goals) <= 0:
        raise ValueError("planner num_goals must be > 0")
    if int(config.num_mid_points_per_goal) <= 0:
        raise ValueError("planner num_mid_points_per_goal must be > 0")
    if int(config.path_num_samples) <= 0:
        raise ValueError("planner path_num_samples must be > 0")
    if int(config.footprint_px) <= 0:
        raise ValueError("planner footprint_px must be > 0")


def plan_on_bev(
    bev_traversability: np.ndarray,
    observed_mask: np.ndarray | None,
    goal_x_m: float,
    goal_y_m: float,
    bev_resolution_m: float,
    config: PlannerConfig | None = None,
    candidate_path_bank: list[np.ndarray] | None = None,
) -> PlannedPath:
    """Plan a local path on a BEV traversability map.

    Goal coordinates use the live planner convention: x is lateral right in
    meters and y is forward in meters, both relative to the reference robot pose.
    """
    cfg = config or PlannerConfig()
    _validate_config(cfg)
    bev = np.asarray(bev_traversability, dtype=np.float32)
    if bev.ndim != 2:
        raise ValueError(f"bev_traversability must be 2D, got {bev.shape}")
    if float(bev_resolution_m) <= 0.0:
        raise ValueError("bev_resolution_m must be > 0")

    known0 = np.isfinite(bev) & (bev >= 0.0)
    if observed_mask is not None:
        known0 &= np.asarray(observed_mask, dtype=bool)

    start0 = (bev.shape[0] - 1, bev.shape[1] // 2)
    goal0 = goal_xy_to_bev_pixel(float(goal_x_m), float(goal_y_m), bev.shape, float(bev_resolution_m))
    cost0 = traversability_to_cost(bev, unknown_cost=float(cfg.unknown_cost))
    cost0 = _smooth_cost(cost0, int(cfg.smooth_kernel))

    planner_cost = _resize_float_map(cost0, int(cfg.grid_size))
    planner_known = _resize_mask(known0, int(cfg.grid_size))
    planner_start = _resize_pixel(start0, bev.shape, int(cfg.grid_size))
    planner_goal = _resize_pixel(goal0, bev.shape, int(cfg.grid_size))

    if candidate_path_bank is None:
        sampler_goal = planner_goal if bool(cfg.include_goal_in_path_bank) else None
        candidate_paths = sample_paths_polynomial(
            robot=planner_start,
            num_goals=int(cfg.num_goals),
            num_mid_points_per_goal=int(cfg.num_mid_points_per_goal),
            num_samples=int(cfg.path_num_samples),
            grid_size=int(cfg.grid_size),
            goal=sampler_goal,
            include_random_goals=bool(cfg.include_random_goals),
            random_seed=cfg.random_seed,
        )
    else:
        candidate_paths = list(candidate_path_bank)
    if len(candidate_paths) == 0:
        raise RuntimeError("Path sampler returned no candidate paths")

    num_points_to_filter = int(min(max(1, int(cfg.number_of_points_to_filter)), int(cfg.path_num_samples) + 1))
    filtered_paths = filter_paths_with_high_costs(
        candidate_paths,
        planner_cost,
        num_points=num_points_to_filter,
        footprint_px=int(cfg.footprint_px),
        threshold_points_ratio=float(cfg.threshold_points_ratio),
        threshold_cost=float(cfg.threshold_cost),
    )
    if len(filtered_paths) == 0:
        selected_paths: list[np.ndarray] = []
        final_rows = np.array([], dtype=np.float32)
        final_cols = np.array([], dtype=np.float32)
        final_path_px = np.empty((0, 2), dtype=np.float32)
        final_path_xy = np.empty((0, 2), dtype=np.float32)
        vis = _draw_plan_overlay(
            cost_to_vis(planner_cost, known_mask=planner_known),
            all_paths=candidate_paths,
            selected_paths=[],
            final_rows=final_rows,
            final_cols=final_cols,
            start_rc=planner_start,
            goal_rc=planner_goal,
        )
        return PlannedPath(
            visualization=vis,
            cost_map=planner_cost,
            known_mask=planner_known,
            final_path_pixels=final_path_px,
            final_path_xy_m=final_path_xy,
            candidate_paths=candidate_paths,
            filtered_paths=filtered_paths,
            selected_paths=selected_paths,
            metadata={
                "status": "no_valid_paths_after_filtering",
                "candidate_paths": int(len(candidate_paths)),
                "filtered_paths": 0,
                "selected_paths": 0,
                "start_row_col": [int(planner_start[0]), int(planner_start[1])],
                "goal_row_col": [int(planner_goal[0]), int(planner_goal[1])],
            },
        )

    selected_paths = filtered_paths
    cluster_meta: dict[str, Any] = {"enabled": bool(cfg.use_clustering), "used": False}
    if bool(cfg.use_clustering) and len(filtered_paths) >= 2:
        try:
            labels, centroids, best_k = adaptive_kmeans(
                filtered_paths,
                min_clusters=1,
                max_clusters=int(cfg.max_clusters),
            )
            cluster_meta["used"] = True
            cluster_meta["best_k"] = int(best_k)
            if len(centroids) >= 2:
                _merged, groups, _angles = merge_centroids_by_angle(
                    centroids,
                    angle_threshold_deg=float(cfg.cluster_angle_threshold_deg),
                )
                dr = float(planner_goal[0] - planner_start[0])
                dc = float(planner_goal[1] - planner_start[1])
                robot_goal_angle = math.degrees(math.atan2(dr, dc))
                best_group_idx, best_diff = select_best_group_by_closest_path_angle(
                    paths=filtered_paths,
                    labels=labels,
                    groups=groups,
                    robot_goal_angle=robot_goal_angle,
                    lookahead_index=30,
                )
                best_group = groups[best_group_idx]
                selected_paths = [p for p, lab in zip(filtered_paths, labels) if int(lab) in best_group]
                cluster_meta.update(
                    {
                        "goal_row_col": [int(planner_goal[0]), int(planner_goal[1])],
                        "robot_goal_angle": float(robot_goal_angle),
                        "best_group_idx": int(best_group_idx),
                        "best_group": [int(x) for x in best_group],
                        "best_diff_deg": float(best_diff),
                        "selected_paths_after_group": int(len(selected_paths)),
                    }
                )
        except Exception as exc:
            cluster_meta["error"] = str(exc)
            selected_paths = filtered_paths
    if len(selected_paths) == 0:
        selected_paths = filtered_paths

    paths_costed = compute_paths_costs(
        selected_paths,
        planner_cost,
        alpha=float(cfg.alpha),
        footprint_px=int(cfg.footprint_px),
    )
    if len(paths_costed) == 0:
        raise RuntimeError("No paths left after cost computation")

    final_num_samples = int(selected_paths[0].shape[0])
    final_rows, final_cols = pick_final_path(
        paths_costed,
        best_k=int(min(max(1, int(cfg.best_k)), len(paths_costed))),
        num_samples=final_num_samples,
        cost_map=planner_cost,
        alpha=float(cfg.alpha),
        footprint_px=int(cfg.footprint_px),
    )
    final_path_px = np.stack([np.asarray(final_rows), np.asarray(final_cols)], axis=1).astype(np.float32)
    final_path_bev = _planner_path_to_bev_path(final_path_px, src_shape=bev.shape, grid_size=int(cfg.grid_size))
    x_right, y_forward = bev_pixel_to_xy(final_path_bev[:, 0], final_path_bev[:, 1], bev.shape, float(bev_resolution_m))
    final_path_xy = np.stack([x_right, y_forward], axis=1).astype(np.float32)

    costs = np.array([float(x[0]) for x in paths_costed], dtype=np.float64)
    vis = _draw_plan_overlay(
        cost_to_vis(planner_cost, known_mask=planner_known),
        all_paths=candidate_paths,
        selected_paths=selected_paths,
        final_rows=np.asarray(final_rows, dtype=np.float32),
        final_cols=np.asarray(final_cols, dtype=np.float32),
        start_rc=planner_start,
        goal_rc=planner_goal,
    )
    return PlannedPath(
        visualization=vis,
        cost_map=planner_cost,
        known_mask=planner_known,
        final_path_pixels=final_path_px,
        final_path_xy_m=final_path_xy,
        candidate_paths=candidate_paths,
        filtered_paths=filtered_paths,
        selected_paths=selected_paths,
        metadata={
            "status": "ok",
            "candidate_paths": int(len(candidate_paths)),
            "filtered_paths": int(len(filtered_paths)),
            "selected_paths": int(len(selected_paths)),
            "start_row_col": [int(planner_start[0]), int(planner_start[1])],
            "goal_row_col": [int(planner_goal[0]), int(planner_goal[1])],
            "goal_x_y_m": [float(goal_x_m), float(goal_y_m)],
            "best_k": int(min(max(1, int(cfg.best_k)), len(paths_costed))),
            "cost_stats": {
                "min": float(costs.min()),
                "max": float(costs.max()),
                "mean": float(costs.mean()),
            },
            "cluster": cluster_meta,
            "planner": {
                "grid_size": int(cfg.grid_size),
                "unknown_cost": float(cfg.unknown_cost),
                "smooth_kernel": int(cfg.smooth_kernel),
                "footprint_px": int(cfg.footprint_px),
                "threshold_cost": float(cfg.threshold_cost),
                "threshold_points_ratio": float(cfg.threshold_points_ratio),
                "number_of_points_to_filter": int(num_points_to_filter),
                "alpha": float(cfg.alpha),
                "path_num_samples": int(cfg.path_num_samples),
                "fixed_path_bank": True,
                "random_seed": cfg.random_seed,
            },
        },
    )
