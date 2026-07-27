"""Runtime configuration + state dataclasses for the urban autonav runtime.

Phase 4 lands the full behavior. This is the Phase 1 skeleton.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class UrbanRuntimeConfig:
    """Tunable runtime knobs. YAML planner config is loaded separately."""

    # Loop rates
    telemetry_hz: float = 5.0
    mission_hz: float = 1.0
    perception_target_hz: float = 3.0        # MPS-friendly default; raise to 8 on CUDA
    # Rover firmware wants commands ~every 1-2 seconds to keep motor cycle
    # alive. Too fast (10Hz) → constant motor reset, no motion. Too slow
    # (once every 30s) → firmware times out and stops. 2 Hz gives the
    # closed-loop align feedback time to react before the rover overshoots
    # during turn-in-place (previous 1 Hz caused the 240° runaway pirouette).
    control_hz: float = 2.0

    # Controller gains. Tuned for a 25cm rover on Botswana dirt.
    max_linear: float = 1.0
    # Minimum forward command when moving. Rover firmware has ~0.15
    # deadband — commanding 0.15 yields ~0.04 m/s actual, way too slow for
    # a 15-min mission. 0.45 puts us firmly out of the deadband so any
    # forward motion is at meaningful speed.
    min_linear: float = 0.45
    max_angular: float = 1.0
    # Skid-steer wheel-speed math: left = linear - angular, right = linear
    # + angular. When |angular| > linear, one wheel goes REVERSE while the
    # other goes forward → pivot in place, no forward translation. During
    # pursuit we cap |angular| below linear so both wheels always roll
    # forward; align mode still uses the full max_angular for pure-rotation
    # turn-in-place.
    max_angular_while_pursuing: float = 0.5
    k_ang: float = 1.0                    # moderate P gain — was 2.0 which saturated angular at small heading errors
    lookahead_m: float = 0.9              # raised 0.5->0.9: 0.5 gave too-high steering gain (pure-pursuit zig-zag); longer lookahead smooths steering
    turn_in_place_thresh_deg: float = 80.0  # only turn-in-place when heading is very wrong; else keep forward crawl
    # If the path we picked is more than this many degrees off from the raw
    # goal direction, ignore the path's heading and just aim at the goal.
    # This prevents the controller from spinning in place when the planner
    # got stuck in a "closest survivable but wrong-direction" arc.
    # Lowered 2026-07-25 from 90° → 45°. At 90°, when the plan's arc curved
    # 60° one way and the goal was 65° the other way, override didn't fire,
    # pursuit followed the flipping arc. Result: bot ping-pongs left-right-
    # left-right without making progress. 45° means we prefer the STABLE
    # goal direction over the JITTERY plan direction more often.
    goal_override_thresh_deg: float = 45.0
    # Hysteresis exit for goal-override (enter above goal_override_thresh_deg,
    # exit below this) so the path<->goal steering target stops flip-flopping
    # near the boundary.
    goal_override_exit_thresh_deg: float = 30.0
    # Closed-loop align-first control: if the goal bearing (computed every
    # tick from FRESH telemetry) is more than align_thresh_deg off from
    # straight-ahead, do a proportional turn-in-place (no forward motion)
    # until we're within align_deadband_deg. This is the fix for the
    # "runaway pirouette" — the control loop no longer resends a stale
    # angular command; it re-checks alignment against live yaw every tick.
    align_thresh_deg: float = 25.0
    # Widened from 8° → 20°. At 8°, small heading drift while driving forward
    # constantly dropped us back into pure-rotation align mode (linear=0),
    # killing forward progress. 20° lets pursuit handle normal heading noise
    # via its own linear+angular combo, and only escalates to pure-rotation
    # for large misalignments.
    align_deadband_deg: float = 20.0
    # Proportional gain used only during align-in-place. Larger → faster
    # correction but more overshoot. 1.5 with control_hz=2 converges in
    # 2-3 ticks from a 90° start without overshooting.
    k_align: float = 1.5

    # Planning
    planner_replan_distance_m: float = 1.0
    goal_virtual_range_m: float = 10.0

    # Checkpoint arrival: only count a CP as reached when the rover is
    # PHYSICALLY within this many meters. Previously hardcoded at 15m, which
    # let the code declare victory without actually moving to the checkpoint
    # (the bot could sit 14m away and still "score"). 3m is snug enough that
    # arriving requires driving to the CP but forgiving on RTK-GPS noise.
    checkpoint_arrival_m: float = 3.0

    # Auto-skip a checkpoint at mission start if it's genuinely behind the
    # rover. Prevents the "spin 180° in place trying to align to CP1 that's
    # behind me" failure mode when the operator started the mission facing
    # away from the first CP. Only applies to the FIRST target — subsequent
    # CPs must be physically reached. Set to False to disable.
    auto_skip_first_cp_if_behind: bool = True
    # Skip only when the CP is more than this many meters BEHIND the rover
    # (y < -threshold in body frame). Prevents skipping legitimate side-by-side
    # CPs due to GPS noise.
    auto_skip_behind_threshold_m: float = 1.5

    # Collision monitor (front strip of BEV).
    # trav_thresh: cells below this are considered "obstacles"
    # hazard_fraction: % of front-strip cells that must be obstacles to halt
    # trav_thresh=0.0 disables the monitor entirely — use as escape hatch when
    # SAM-TP + placeholder-calibration produce false-positive obstacles.
    collision_forward_m: float = 0.6
    collision_half_width_m: float = 0.25
    collision_trav_thresh: float = 0.10       # was 0.15 — stricter still (only very-obstacle cells count)
    collision_hazard_fraction: float = 0.40   # need 40% of strip to be obstacle before halting

    # Recovery
    recovery_off_road_votes: int = 2
    recovery_buffer_size: int = 5

    # Contrast-based obstacle refinement — post-processes SAM-TP output to
    # catch obstacles that SAM-TP mislabels as drivable (other rovers,
    # chairs, low objects) by comparing pixel luminance to the median
    # luminance of SAM-TP's own "drivable" pixels. See
    # samtp.refine_traversability_by_contrast for the math.
    contrast_refine_enabled: bool = False   # OFF: not in GeNIE; was flipping dark-but-drivable ground (asphalt/shadows) to obstacle
    contrast_drivable_thresh: float = 0.5     # SAM-TP > this = "reference ground"
    contrast_darkness_ratio: float = 0.65     # pixel < median * this = obstacle

    # CLIPSeg overlay — a second perception layer that understands what
    # things are (zero-shot from text prompts). Fuses with SAM-TP via
    #   trav_final = trav_samtp × (1 − alpha × obstacle_clipseg)
    # so anywhere CLIPSeg thinks there's a rover/car/person/etc, the
    # traversability drops toward zero even when SAM-TP said "drivable".
    # Requires transformers + the CIDAS/clipseg-rd64-refined checkpoint
    # (~180 MB, downloaded once, cached to ~/.cache/huggingface).
    clipseg_enabled: bool = False   # OFF: run GeNIE-style (SAM-TP only). Was compensating for the OLD checkpoint_2 painting everything green; the Mini-4K fine-tuned model should be discriminative on its own
    # CLIPSeg runs one image-encoding pass per prompt internally, so
    # doubling the prompt count roughly doubles perception latency. Keep
    # this list SHORT (≤ 5) or perception drops below the rate the
    # planner needs and BEV goes stale (see 2026-07-23 log). The 4 below
    # cover the classes we've actually seen block the rover in test runs.
    # To broaden coverage, prefer combining categories with " or " into
    # a single prompt instead of adding more entries:
    #   "a robot or car or truck or bicycle"
    clipseg_prompts: tuple = (
        "a robot or rover or car or truck or bicycle",
        "a person",
        "grass",
        "a wall or fence or curb",
    )
    clipseg_alpha: float = 0.9                # blend weight of CLIPSeg into trav
    clipseg_confidence_thresh: float = 0.3    # ignore CLIPSeg pixels below this

    # Safety
    battery_floor: int = 15
    max_error_streak: int = 3

    # Provider
    vlm_provider: str = "gemini"
    vlm_model: Optional[str] = None

    # Run mode
    dry_run: bool = False
    tick_logging_enabled: bool = True


@dataclass
class TelemetrySnapshot:
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    yaw_deg: Optional[float] = None            # /data.orientation, compass 0-360
    speed_ms: Optional[float] = None
    battery: Optional[int] = None
    gps_signal: Optional[float] = None
    ts: float = 0.0                             # local time when we polled
    rover_ts: float = 0.0                       # rover-side timestamp from window.rtm_data.timestamp
                                                # frozen rover_ts + advancing ts ⇒ RTM link is dead


@dataclass
class UrbanRuntimeState:
    """Shared state across all loops. Field-level locks are held externally."""

    running: bool = False
    mode: str = "idle"                          # idle | starting | driving | scoring | stopped | recovering | done | error
    iterations: int = 0
    error_streak: int = 0
    last_error: Optional[str] = None
    log_dir: Optional[str] = None

    # Telemetry
    last_telemetry: TelemetrySnapshot = field(default_factory=TelemetrySnapshot)

    # Perception
    last_bev: Any = None                        # np.ndarray (H, W) float32 or None
    last_observed_mask: Any = None              # np.ndarray (H, W) bool
    last_camera_pose: Any = None                # 4x4 T_world_camera
    last_bev_ts: float = 0.0
    # Raw SAM-TP traversability in image space (HxW float32) — for debug view
    last_samtp_trav: Any = None
    last_samtp_logits: Any = None       # raw logits, for GeNIE-style normalized JET
    last_samtp_ts: float = 0.0
    # Raw CLIPSeg obstacle mask in image space (HxW float32 in [0, 1]) — for debug view
    last_clipseg_mask: Any = None
    last_clipseg_ts: float = 0.0

    # Planning
    goal_x_m: float = 0.0
    goal_y_m: float = 0.0
    distance_to_next_m: float = float("inf")
    last_path_xy_m: Any = None                  # np.ndarray (N, 2) or None
    last_plan_visualization: Any = None         # RGB uint8 for /bev endpoint
    last_plan_meta: dict = field(default_factory=dict)
    last_plan_ts: float = 0.0

    # Mission
    checkpoints: list = field(default_factory=list)
    current_seq: int = 1
    history: list = field(default_factory=list)

    # Control
    last_linear: float = 0.0
    last_angular: float = 0.0
    last_control_ts: float = 0.0
    last_control_reason: str = ""       # "pursuit" | "hazard" | "no_path" | "stale_plan" | "not_driving"


def make_state_locks() -> dict[str, asyncio.Lock]:
    """Named per-bucket locks so writers/readers don't clobber each other."""
    return {
        "telemetry": asyncio.Lock(),
        "perception": asyncio.Lock(),
        "planning": asyncio.Lock(),
        "mission": asyncio.Lock(),
        "control": asyncio.Lock(),
    }
