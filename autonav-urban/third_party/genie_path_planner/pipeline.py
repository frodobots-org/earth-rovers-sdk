from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .geometry import pose_xy_yaw_to_matrix
from .io_utils import deep_get, load_depth_m, load_matrix, load_rgb_image, resolve_path, save_json, to_builtin
from .planner import PlannerConfig, plan_on_bev
from .projection import (
    BEVObservation,
    blend_modalities,
    depth_to_bev_height_and_traversability,
    fuse_bev_observations,
    logits_to_traversability,
    project_score_to_bev,
    traversability_vis,
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def planner_config_from_dict(config: dict[str, Any]) -> PlannerConfig:
    planner = config.get("planner", {})
    if not isinstance(planner, dict):
        planner = {}
    return PlannerConfig(
        grid_size=int(planner.get("grid_size", 240)),
        unknown_cost=float(planner.get("unknown_cost", 0.2)),
        smooth_kernel=int(planner.get("smooth_kernel", 3)),
        num_goals=int(planner.get("num_goals", 30)),
        num_mid_points_per_goal=int(planner.get("num_mid_points_per_goal", 20)),
        path_num_samples=int(planner.get("path_num_samples", 100)),
        footprint_px=int(planner.get("footprint_px", 18)),
        threshold_cost=float(planner.get("threshold_cost", 0.50)),
        threshold_points_ratio=float(planner.get("threshold_points_ratio", 0.05)),
        number_of_points_to_filter=int(planner.get("number_of_points_to_filter", 60)),
        alpha=float(planner.get("alpha", 1.0)),
        best_k=int(planner.get("best_k", 12)),
        use_clustering=bool(planner.get("use_clustering", False)),
        max_clusters=int(planner.get("max_clusters", 4)),
        cluster_angle_threshold_deg=float(planner.get("cluster_angle_threshold_deg", 40.0)),
        random_seed=planner.get("random_seed", 42),
        include_goal_in_path_bank=bool(planner.get("include_goal_in_path_bank", False)),
        include_random_goals=bool(planner.get("include_random_goals", True)),
    )


def load_samtp_model(config: dict[str, Any], config_dir: Path) -> Any:
    samtp_cfg = config.get("samtp", {})
    if not isinstance(samtp_cfg, dict):
        samtp_cfg = {}
    root = repo_root()
    cfg_path = resolve_path(
        samtp_cfg.get("config_path", "sam2/configs/sam2.1_inference_tiny/sam2.1_custom2.yaml"),
        base_dir=config_dir,
        repo_root=root,
    )
    ckpt_path = resolve_path(
        samtp_cfg.get(
            "checkpoint_path",
            "sam2_logs/configs/sam2.1_training_tiny/"
            "sam2_training_custom2_freezeNoneNone_f57.yaml/checkpoints/checkpoint_2.pt",
        ),
        base_dir=config_dir,
        repo_root=root,
    )
    if cfg_path is None or not cfg_path.exists():
        raise FileNotFoundError(f"SAM-TP config not found: {cfg_path}")
    if ckpt_path is None or not ckpt_path.exists():
        raise FileNotFoundError(f"SAM-TP checkpoint not found: {ckpt_path}")

    from sam2.sam_tp import SAM_TP

    return SAM_TP(
        sam2_cfg_path=str(cfg_path),
        sam2_checkpoint_path=str(ckpt_path),
        score_thresh=float(samtp_cfg.get("score_thresh", 0.0)),
        multimask=bool(samtp_cfg.get("multimask", False)),
    )


def _matrix_from_config(
    obs_cfg: dict[str, Any],
    config: dict[str, Any],
    config_dir: Path,
    shape: tuple[int, int],
    obs_keys: list[str],
    global_keys: list[str],
    name: str,
    required: bool = True,
) -> np.ndarray | None:
    root = repo_root()
    for key in obs_keys:
        value = deep_get(obs_cfg, key)
        if value is not None:
            return load_matrix(value, shape, config_dir, root, name)
    for key in global_keys:
        value = deep_get(config, key)
        if value is not None:
            return load_matrix(value, shape, config_dir, root, name)
    if required:
        raise ValueError(f"Missing required {name}; set one of {obs_keys + global_keys}")
    return None


def _robot_pose_from_config(obs_cfg: dict[str, Any], config: dict[str, Any], config_dir: Path) -> np.ndarray | None:
    root = repo_root()
    for key in ("robot_pose", "robot.pose", "base_pose", "base.pose"):
        value = deep_get(obs_cfg, key)
        if value is not None:
            return load_matrix(value, (4, 4), config_dir, root, "robot_pose")
    for key in ("robot_pose_path", "robot.pose_path", "base_pose_path", "base.pose_path"):
        value = deep_get(obs_cfg, key)
        if value is not None:
            return load_matrix(value, (4, 4), config_dir, root, "robot_pose")
    for key in ("robot_pose_xy_yaw", "robot.pose_xy_yaw", "base_pose_xy_yaw", "base.pose_xy_yaw"):
        value = deep_get(obs_cfg, key)
        if value is not None:
            return pose_xy_yaw_to_matrix(value)

    for key in ("robot.pose", "robot.base_pose"):
        value = deep_get(config, key)
        if value is not None:
            return load_matrix(value, (4, 4), config_dir, root, "robot_pose")
    for key in ("robot.pose_path", "robot.base_pose_path"):
        value = deep_get(config, key)
        if value is not None:
            return load_matrix(value, (4, 4), config_dir, root, "robot_pose")
    for key in ("robot.pose_xy_yaw", "robot.base_pose_xy_yaw"):
        value = deep_get(config, key)
        if value is not None:
            return pose_xy_yaw_to_matrix(value)
    return None


def _camera_pose_from_config(
    obs_cfg: dict[str, Any],
    config: dict[str, Any],
    config_dir: Path,
    robot_pose: np.ndarray | None,
) -> np.ndarray:
    camera_pose = _matrix_from_config(
        obs_cfg,
        config,
        config_dir,
        shape=(4, 4),
        obs_keys=["camera_pose", "camera.pose", "camera.pose_path"],
        global_keys=["camera.pose", "camera.pose_path"],
        name="camera_pose",
        required=False,
    )
    if camera_pose is not None:
        return camera_pose
    if robot_pose is not None:
        t_base_camera = _matrix_from_config(
            obs_cfg,
            config,
            config_dir,
            shape=(4, 4),
            obs_keys=["T_base_camera", "camera.T_base_camera", "camera.T_base_camera_path"],
            global_keys=["camera.T_base_camera", "camera.T_base_camera_path", "robot.T_base_camera"],
            name="T_base_camera",
            required=False,
        )
        if t_base_camera is not None:
            return robot_pose @ t_base_camera
    raise ValueError(
        "Missing camera pose. Provide camera_pose/T_world_camera, or provide robot_pose plus camera.T_base_camera."
    )


def _load_score_map(
    obs_cfg: dict[str, Any],
    config: dict[str, Any],
    config_dir: Path,
    cli_score_map: str | None = None,
    cli_score_map_type: str | None = None,
) -> tuple[np.ndarray | None, str | None]:
    root = repo_root()
    path_value = cli_score_map or obs_cfg.get("score_map") or obs_cfg.get("traversability_map") or obs_cfg.get("logits")
    if path_value is None:
        return None, None
    path = resolve_path(path_value, base_dir=config_dir, repo_root=root)
    if path is None or not path.exists():
        raise FileNotFoundError(f"Score map not found: {path}")
    score = np.asarray(np.load(path), dtype=np.float32)
    score_type = (
        cli_score_map_type
        or obs_cfg.get("score_map_type")
        or ("logits" if str(path_value).endswith("logits.npy") else "traversability")
    )
    score_type_l = str(score_type).lower()
    if score_type_l == "logits":
        transform = str(deep_get(config, "samtp.score_transform", "sigmoid"))
        score = logits_to_traversability(score, transform=transform)
    elif score_type_l not in {"traversability", "score", "probability"}:
        raise ValueError(f"Unsupported score_map_type {score_type!r}; use traversability or logits")
    return score.astype(np.float32), str(path)


def _run_samtp_or_score_map(
    obs_cfg: dict[str, Any],
    config: dict[str, Any],
    config_dir: Path,
    samtp_model: Any | None,
    rgb: np.ndarray | None,
    cli_score_map: str | None = None,
    cli_score_map_type: str | None = None,
) -> tuple[np.ndarray, np.ndarray | None, str]:
    score_map, score_source = _load_score_map(obs_cfg, config, config_dir, cli_score_map, cli_score_map_type)
    if score_map is not None:
        return score_map, None, score_source or "score_map"
    if rgb is None:
        raise ValueError("RGB image is required when no score_map/traversability_map is provided")
    if samtp_model is None:
        samtp_model = load_samtp_model(config, config_dir)
    out = samtp_model.run_sam2_inference(rgb)
    logits = np.asarray(out["logits"], dtype=np.float32)
    heatmap = np.asarray(out.get("heatmap"), dtype=np.uint8) if out.get("heatmap") is not None else None
    transform = str(deep_get(config, "samtp.score_transform", "sigmoid"))
    return logits_to_traversability(logits, transform=transform), heatmap, "samtp"


def _observation_configs_from_inputs(
    config: dict[str, Any],
    config_dir: Path,
    image: str | None,
    depth: str | None,
    score_map: str | None,
    camera_k: str | None,
    camera_pose: str | None,
    robot_pose_xy_yaw: str | None,
) -> list[dict[str, Any]]:
    if image or score_map:
        obs: dict[str, Any] = {"name": "cli_observation"}
        if image:
            obs["image"] = image
        if depth:
            obs["depth"] = depth
        if score_map:
            obs["score_map"] = score_map
        if camera_k:
            obs["camera"] = {"intrinsics_path": camera_k}
        if camera_pose:
            obs.setdefault("camera", {})["pose_path"] = camera_pose
        if robot_pose_xy_yaw:
            obs["robot_pose_xy_yaw"] = [float(x.strip()) for x in robot_pose_xy_yaw.split(",")]
        return [obs]

    observations = config.get("observations", [])
    if not isinstance(observations, list) or not observations:
        raise ValueError("Provide --image (or --score-map) or configure a non-empty observations list")
    return [dict(x) for x in observations]


def build_bev_observations(
    config: dict[str, Any],
    config_dir: Path,
    observations_cfg: list[dict[str, Any]],
    cli_score_map: str | None = None,
    cli_score_map_type: str | None = None,
) -> tuple[list[BEVObservation], dict[str, Any]]:
    root = repo_root()
    projection_cfg = config.get("projection", {}) if isinstance(config.get("projection", {}), dict) else {}
    depth_cfg = config.get("depth", {}) if isinstance(config.get("depth", {}), dict) else {}
    fusion_cfg = config.get("fusion", {}) if isinstance(config.get("fusion", {}), dict) else {}

    res = float(projection_cfg.get("resolution_m_per_px", projection_cfg.get("bev_resolution", 0.03)))
    forward_range = float(projection_cfg.get("forward_range_m", projection_cfg.get("bev_forward_range", 4.0)))
    side_range = float(projection_cfg.get("side_range_m", projection_cfg.get("bev_side_range", 2.0)))
    ground_z = float(projection_cfg.get("ground_z", 0.0))
    max_ray_distance = float(projection_cfg.get("max_ray_distance_m", 6.0))
    depth_enabled = bool(depth_cfg.get("enabled", True))
    depth_unit = str(depth_cfg.get("unit", "m"))

    samtp_model = None
    need_samtp = any(
        not (
            obs.get("score_map") is not None
            or obs.get("traversability_map") is not None
            or obs.get("logits") is not None
            or (idx == 0 and cli_score_map is not None)
        )
        for idx, obs in enumerate(observations_cfg)
    )
    if need_samtp:
        samtp_model = load_samtp_model(config, config_dir)

    records: list[BEVObservation] = []
    debug: dict[str, Any] = {"observations": []}
    for idx, obs_cfg in enumerate(observations_cfg):
        name = str(obs_cfg.get("name", f"obs_{idx + 1}"))
        image_path = resolve_path(obs_cfg.get("image") or obs_cfg.get("rgb"), config_dir, root)
        rgb = load_rgb_image(image_path) if image_path is not None and image_path.exists() else None
        camera_k = _matrix_from_config(
            obs_cfg,
            config,
            config_dir,
            shape=(3, 3),
            obs_keys=["camera_intrinsics", "camera_K", "camera.intrinsics", "camera.intrinsics_path"],
            global_keys=["camera.intrinsics", "camera.intrinsics_path", "camera.K", "camera.K_path"],
            name="camera_K",
            required=True,
        )
        robot_pose = _robot_pose_from_config(obs_cfg, config, config_dir)
        camera_pose = _camera_pose_from_config(obs_cfg, config, config_dir, robot_pose=robot_pose)
        score_map, heatmap, score_source = _run_samtp_or_score_map(
            obs_cfg,
            config,
            config_dir,
            samtp_model,
            rgb,
            cli_score_map=cli_score_map if idx == 0 else None,
            cli_score_map_type=cli_score_map_type if idx == 0 else None,
        )
        rgb_bev, rgb_observed, rgb_stats = project_score_to_bev(
            score_map=score_map,
            camera_k=camera_k,
            camera_pose=camera_pose,
            ground_z=ground_z,
            bev_resolution_m_per_px=res,
            bev_forward_range_m=forward_range,
            bev_side_range_m=side_range,
            max_ray_distance_m=max_ray_distance,
        )

        depth_bev = None
        depth_observed = None
        rgbd_bev = None
        rgbd_observed = None
        depth_stats: dict[str, Any] | None = None
        depth_height = None
        depth_path = resolve_path(obs_cfg.get("depth") or obs_cfg.get("depth_m"), config_dir, root)
        if depth_enabled and depth_path is not None:
            depth_m = load_depth_m(depth_path, unit=str(obs_cfg.get("depth_unit", depth_unit)))
            depth_height, depth_bev, depth_observed, depth_stats = depth_to_bev_height_and_traversability(
                depth_m=depth_m,
                camera_k=camera_k,
                camera_pose=camera_pose,
                ground_z=ground_z,
                reliable_depth_m=float(depth_cfg.get("reliable_depth_m", 2.4)),
                min_depth_m=float(depth_cfg.get("min_depth_m", 0.15)),
                obstacle_height_thresh_m=float(depth_cfg.get("obstacle_height_thresh_m", 0.20)),
                bev_resolution_m_per_px=res,
                bev_forward_range_m=forward_range,
                bev_side_range_m=side_range,
            )
            rgbd_bev, rgbd_observed = blend_modalities(
                rgb_bev=rgb_bev,
                rgb_observed=rgb_observed,
                depth_bev=depth_bev,
                depth_observed=depth_observed,
                rgb_weight=float(fusion_cfg.get("rgb_weight", 0.2)),
                depth_weight=float(fusion_cfg.get("depth_weight", 0.8)),
                require_depth=bool(fusion_cfg.get("require_depth_for_rgbd", True)),
            )

        records.append(
            BEVObservation(
                name=name,
                camera_pose=camera_pose,
                bev_resolution_m=res,
                rgb_bev=rgb_bev,
                rgb_observed=rgb_observed,
                depth_bev=depth_bev,
                depth_observed=depth_observed,
                rgbd_bev=rgbd_bev,
                rgbd_observed=rgbd_observed,
                robot_pose=robot_pose,
                metadata={
                    "image": str(image_path) if image_path is not None else None,
                    "depth": str(depth_path) if depth_path is not None else None,
                    "score_source": score_source,
                    "rgb_stats": rgb_stats,
                    "depth_stats": depth_stats,
                    "has_robot_pose": robot_pose is not None,
                    "heatmap": heatmap,
                    "depth_height": depth_height,
                },
            )
        )
        debug["observations"].append(
            {
                "name": name,
                "image": str(image_path) if image_path is not None else None,
                "depth": str(depth_path) if depth_path is not None else None,
                "score_source": score_source,
                "rgb_bev_stats": rgb_stats,
                "depth_bev_stats": depth_stats,
                "has_robot_pose": robot_pose is not None,
            }
        )
    return records, debug


def run_offline_path_planner(
    config: dict[str, Any],
    config_dir: Path,
    image: str | None = None,
    depth: str | None = None,
    score_map: str | None = None,
    score_map_type: str | None = None,
    camera_k: str | None = None,
    camera_pose: str | None = None,
    robot_pose_xy_yaw: str | None = None,
    goal_x: float | None = None,
    goal_y: float | None = None,
    mode: str | None = None,
    output_dir: str | None = None,
) -> dict[str, Any]:
    observations_cfg = _observation_configs_from_inputs(
        config,
        config_dir,
        image=image,
        depth=depth,
        score_map=score_map,
        camera_k=camera_k,
        camera_pose=camera_pose,
        robot_pose_xy_yaw=robot_pose_xy_yaw,
    )
    records, debug = build_bev_observations(
        config,
        config_dir,
        observations_cfg,
        cli_score_map=score_map,
        cli_score_map_type=score_map_type,
    )

    projection_cfg = config.get("projection", {}) if isinstance(config.get("projection", {}), dict) else {}
    obs_fusion_cfg = (
        config.get("observation_fusion", {}) if isinstance(config.get("observation_fusion", {}), dict) else {}
    )
    res = float(projection_cfg.get("resolution_m_per_px", projection_cfg.get("bev_resolution", 0.03)))
    forward_range = float(projection_cfg.get("forward_range_m", projection_cfg.get("bev_forward_range", 4.0)))
    side_range = float(projection_cfg.get("side_range_m", projection_cfg.get("bev_side_range", 2.0)))

    plan_mode = str(mode or deep_get(config, "planning.mode", deep_get(config, "fusion.mode", "rgbd"))).lower()
    if plan_mode not in {"rgb", "depth", "rgbd"}:
        raise ValueError(f"planning mode must be rgb/depth/rgbd, got {plan_mode!r}")

    max_obs = int(obs_fusion_cfg.get("max_observations", len(records)))
    if bool(obs_fusion_cfg.get("enabled", False)) or len(records) > 1:
        selected_records = records[-max(1, min(max_obs, len(records))) :]
    else:
        selected_records = [records[-1]]

    reference_spec = str(obs_fusion_cfg.get("reference", "last")).lower()
    if reference_spec == "first":
        reference_record = selected_records[0]
    elif reference_spec == "last":
        reference_record = selected_records[-1]
    else:
        reference_record = selected_records[int(reference_spec)]

    if reference_record.robot_pose is not None:
        reference_pose = reference_record.robot_pose
        reference_frame = "base"
    else:
        reference_pose = reference_record.camera_pose
        reference_frame = "camera"

    fused_bev, fused_observed, fusion_meta = fuse_bev_observations(
        selected_records,
        mode=plan_mode,
        reference_pose=reference_pose,
        reference_frame=reference_frame,
        bev_resolution_m=res,
        bev_forward_range_m=forward_range,
        bev_side_range_m=side_range,
    )

    gx = float(goal_x if goal_x is not None else deep_get(config, "goal.x_m", 0.0))
    gy = float(goal_y if goal_y is not None else deep_get(config, "goal.y_m", forward_range))
    planned = plan_on_bev(
        bev_traversability=fused_bev,
        observed_mask=fused_observed,
        goal_x_m=gx,
        goal_y_m=gy,
        bev_resolution_m=res,
        config=planner_config_from_dict(config),
    )

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_dir_value = output_dir or deep_get(config, "output.dir", "outputs/path_planner")
    out_dir = resolve_path(out_dir_value, base_dir=config_dir, repo_root=repo_root())
    assert out_dir is not None
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"genie_plan_{plan_mode}_{ts}"

    vis_path = out_dir / f"{stem}.png"
    fused_path = out_dir / f"{stem}_fused_bev_traversability.npy"
    observed_path = out_dir / f"{stem}_fused_bev_observed.npy"
    cost_path = out_dir / f"{stem}_cost.npy"
    path_px_path = out_dir / f"{stem}_final_path_pixels.npy"
    path_xy_path = out_dir / f"{stem}_final_path_xy_m.npy"
    meta_path = out_dir / f"{stem}.json"
    Image.fromarray(planned.visualization, mode="RGB").save(vis_path)
    np.save(fused_path, fused_bev.astype(np.float32))
    np.save(observed_path, fused_observed.astype(np.uint8))
    np.save(cost_path, planned.cost_map.astype(np.float32))
    np.save(path_px_path, planned.final_path_pixels.astype(np.float32))
    np.save(path_xy_path, planned.final_path_xy_m.astype(np.float32))

    fused_vis_path = out_dir / f"{stem}_fused_bev.png"
    Image.fromarray(traversability_vis(fused_bev, draw_robot_marker=True), mode="RGB").save(fused_vis_path)

    for rec in records:
        rec_meta = rec.metadata or {}
        prefix = out_dir / f"{stem}_{rec.name}"
        Image.fromarray(traversability_vis(rec.rgb_bev, draw_robot_marker=True), mode="RGB").save(
            f"{prefix}_rgb_bev.png"
        )
        if rec.depth_bev is not None:
            Image.fromarray(traversability_vis(rec.depth_bev, draw_robot_marker=True), mode="RGB").save(
                f"{prefix}_depth_bev.png"
            )
        if rec.rgbd_bev is not None:
            Image.fromarray(traversability_vis(rec.rgbd_bev, draw_robot_marker=True), mode="RGB").save(
                f"{prefix}_rgbd_bev.png"
            )
        heatmap = rec_meta.get("heatmap")
        if isinstance(heatmap, np.ndarray):
            Image.fromarray(heatmap.astype(np.uint8), mode="RGB").save(f"{prefix}_samtp.png")

    metadata = {
        "mode": plan_mode,
        "goal_x_y_m": [gx, gy],
        "reference_observation": reference_record.name,
        "reference_frame": reference_frame,
        "projection": {
            "resolution_m_per_px": res,
            "forward_range_m": forward_range,
            "side_range_m": side_range,
        },
        "fusion": fusion_meta,
        "observations": debug["observations"],
        "planner": planned.metadata,
        "output_files": {
            "visualization": str(vis_path),
            "fused_bev_png": str(fused_vis_path),
            "fused_bev_traversability_npy": str(fused_path),
            "fused_bev_observed_npy": str(observed_path),
            "planner_cost_npy": str(cost_path),
            "final_path_pixels_npy": str(path_px_path),
            "final_path_xy_m_npy": str(path_xy_path),
            "metadata_json": str(meta_path),
        },
        "semantics": {
            "goal_coordinates": "x is +right and y is +forward, meters, in the selected reference robot/camera frame",
            "path_xy_columns": ["x_right_m", "y_forward_m"],
            "traversability": "1.0 is easiest to traverse, 0.0 is least traversable, -1.0 is unknown",
            "visualization": {
                "green": "low cost / traversable",
                "red": "high cost / non-traversable",
                "black": "unknown",
                "gray_lines": "fixed sampled path bank",
                "orange_lines": "paths left after filtering and optional clustering",
                "white_line": "final selected path",
                "cyan_dot": "goal clipped to the local BEV window if outside range",
            },
        },
    }
    save_json(meta_path, to_builtin(metadata))
    return metadata
