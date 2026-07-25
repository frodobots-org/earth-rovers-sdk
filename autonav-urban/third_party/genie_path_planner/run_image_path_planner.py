#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from genie_path_planner.io_utils import load_config
from genie_path_planner.pipeline import run_offline_path_planner


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run SAM-TP projection, optional depth/observation fusion, and BEV path planning."
    )
    parser.add_argument("--config", required=True, help="Planner YAML/JSON config")
    parser.add_argument("--image", default=None, help="RGB image path. Overrides config observations when set.")
    parser.add_argument("--depth", default=None, help="Optional depth path (.npy meters or depth image)")
    parser.add_argument(
        "--score-map",
        default=None,
        help="Optional HxW .npy traversability/logits map to skip SAM-TP inference.",
    )
    parser.add_argument(
        "--score-map-type",
        choices=["traversability", "logits"],
        default=None,
        help="Interpret --score-map as traversability or raw logits.",
    )
    parser.add_argument(
        "--camera-k",
        default=None,
        help=(
            "Optional camera intrinsics .npy/.json/.yaml. If omitted, "
            "camera.intrinsics or camera.intrinsics_path from the config is used."
        ),
    )
    parser.add_argument(
        "--camera-pose",
        default=None,
        help=(
            "Optional T_world_camera .npy/.json/.yaml. If omitted, "
            "camera.pose or camera.pose_path from the config is used."
        ),
    )
    parser.add_argument(
        "--robot-pose-xy-yaw",
        default=None,
        help="Optional reference robot pose as 'x,y,yaw_rad'. Requires camera.T_base_camera in config.",
    )
    parser.add_argument("--goal-x", type=float, default=None, help="Goal x in meters (+right)")
    parser.add_argument("--goal-y", type=float, default=None, help="Goal y in meters (+forward)")
    parser.add_argument("--mode", choices=["rgb", "depth", "rgbd"], default=None, help="Planning mode override")
    parser.add_argument("--output-dir", default=None, help="Output directory override")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config, config_dir = load_config(args.config)
    meta = run_offline_path_planner(
        config=config,
        config_dir=config_dir,
        image=args.image,
        depth=args.depth,
        score_map=args.score_map,
        score_map_type=args.score_map_type,
        camera_k=args.camera_k,
        camera_pose=args.camera_pose,
        robot_pose_xy_yaw=args.robot_pose_xy_yaw,
        goal_x=args.goal_x,
        goal_y=args.goal_y,
        mode=args.mode,
        output_dir=args.output_dir,
    )
    files = meta["output_files"]
    print(f"[GENIE-PLAN] visualization: {files['visualization']}")
    print(f"[GENIE-PLAN] final path xy: {files['final_path_xy_m_npy']}")
    print(f"[GENIE-PLAN] metadata: {files['metadata_json']}")


if __name__ == "__main__":
    main()
