"""Autonav Urban runtime — 5-loop asyncio orchestrator.

Phase 5: telemetry + perception (BEV visible).
Phase 6: + planning (plan_on_bev).
Phase 7: + control (pure pursuit -> POST /control).
Phase 8: + mission (checkpoint list, arrival, next_seq).
Phase 10: + recovery (VLM off-road detection).
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import numpy as np
from PIL import Image

from . import CALIBRATION_ROOT, CONFIGS_ROOT, LOGS_ROOT, THIRD_PARTY_ROOT
from .bev import build_T_world_camera, load_camera_K, load_T_base_camera, project_frame_to_bev
from .config import UrbanRuntimeConfig, UrbanRuntimeState, TelemetrySnapshot, make_state_locks
from .controller import align_in_place, front_strip_hazard, pick_lookahead, pure_pursuit
from .geo import gps_to_local_goal

logger = logging.getLogger("autonav_urban.runtime")


# Callback types injected by main.py so we don't hard-import SDK internals.
FrameFetcher = Callable[[str], Awaitable[str]]                       # (view) -> base64 str
DataFetcher = Callable[[], Awaitable[dict]]                          # () -> /data dict
ControlPoster = Callable[[float, float, int], Awaitable[Any]]        # (lin, ang, lamp) -> anything
CheckpointsFetcher = Callable[[], Awaitable[dict]]                   # () -> /checkpoints-list body
CheckpointReporter = Callable[[], Awaitable[dict]]                   # () -> /checkpoint-reached response OR raises


def _jet_normalized(score: np.ndarray) -> np.ndarray:
    """Per-frame min-max normalized JET colormap, matching the GeNIE paper's
    traversability visualization: RED = highest traversability (drivable),
    BLUE = lowest (obstacle). Normalizing per frame shows the relative gradient
    even when all raw scores share the same sign."""
    s = np.asarray(score, dtype=np.float32)
    finite = np.isfinite(s)
    lo = float(s[finite].min()) if finite.any() else 0.0
    hi = float(s[finite].max()) if finite.any() else 1.0
    t = np.clip((s - lo) / (hi - lo + 1e-8), 0.0, 1.0)
    r = np.clip(1.5 - np.abs(4.0 * t - 3.0), 0.0, 1.0)
    g = np.clip(1.5 - np.abs(4.0 * t - 2.0), 0.0, 1.0)
    b = np.clip(1.5 - np.abs(4.0 * t - 1.0), 0.0, 1.0)
    vis = (np.stack([r, g, b], axis=-1) * 255.0).astype(np.uint8)
    vis[~finite] = 0
    return vis


def _jet_bev(score_map: np.ndarray, draw_robot_marker: bool = True) -> np.ndarray:
    """GeNIE-style JET for the BEV: fixed [0,1] traversability (RED=drivable,
    BLUE=impassable), BLACK for unknown (-1) cells. Fixed range (not per-frame
    normalized) so BEV colors keep their absolute meaning."""
    s = np.asarray(score_map, dtype=np.float32)
    known = np.isfinite(s) & (s >= 0.0)
    t = np.clip(s, 0.0, 1.0)
    r = np.clip(1.5 - np.abs(4.0 * t - 3.0), 0.0, 1.0)
    g = np.clip(1.5 - np.abs(4.0 * t - 2.0), 0.0, 1.0)
    b = np.clip(1.5 - np.abs(4.0 * t - 1.0), 0.0, 1.0)
    vis = (np.stack([r, g, b], axis=-1) * 255.0).astype(np.uint8)
    vis[~known] = 0
    if draw_robot_marker:
        h, w = s.shape
        cc, cr = w // 2, h - 1
        vis[max(0, cr - 2):min(h, cr + 3), max(0, cc - 2):min(w, cc + 3)] = (255, 255, 255)
        vis[max(0, cr - 28):cr + 1, max(0, cc - 1):min(w, cc + 1)] = (255, 255, 255)
    return vis


def _decode_frame_b64(frame_b64: str) -> np.ndarray:
    raw = base64.b64decode(frame_b64)
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    return np.asarray(im, dtype=np.uint8)


def _load_planner_yaml(path: Path) -> dict:
    import yaml
    with path.open("r") as f:
        return yaml.safe_load(f) or {}


def _overlay_ideal_centerline(
    vis: np.ndarray,
    goal_x_m: float,
    goal_y_m: float,
    bev_res_m: float,
    color: tuple = (255, 255, 0),
) -> np.ndarray:
    """Draw a dashed yellow reference line from the rover to the goal direction.
    Lets the operator see at a glance when the planner's chosen (red) path is
    drifting off-center: if the ideal line and the chosen path diverge, the
    planner picked an asymmetric route. Also draws a short vertical crosshair
    at the rover for a visual "straight ahead" reference.
    """
    if vis is None or bev_res_m <= 0.0:
        return vis
    out = np.ascontiguousarray(vis).copy()
    H, W = out.shape[:2]
    r0 = H - 1
    c0 = W // 2

    # Vertical straight-ahead reference (short white ticks, spaced)
    for r in range(H - 1, -1, -6):
        if 0 <= c0 < W and 0 <= r < H:
            out[r, c0] = (255, 255, 255)

    # Reach limit — clip endpoint to on-image
    norm = (goal_x_m ** 2 + goal_y_m ** 2) ** 0.5
    if norm < 1e-3:
        return out
    max_reach_m = float(H) * bev_res_m
    scale = min(1.0, max_reach_m / norm)
    end_x = goal_x_m * scale
    end_y = goal_y_m * scale
    r1 = int(round(r0 - end_y / bev_res_m))
    c1 = int(round(c0 + end_x / bev_res_m))

    steps = max(abs(r1 - r0), abs(c1 - c0)) + 1
    for i in range(steps):
        # Dashed: 4 on, 4 off
        if (i // 4) % 2 != 0:
            continue
        t = i / max(1, steps - 1)
        r = int(round(r0 + t * (r1 - r0)))
        c = int(round(c0 + t * (c1 - c0)))
        for dc in (-1, 0, 1):  # 3-px thick so it stands out over cyan candidates
            cc = c + dc
            if 0 <= r < H and 0 <= cc < W:
                out[r, cc] = color
    return out


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in meters."""
    R = 6_371_000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


@dataclass
class UrbanRuntime:
    """Owner of loops + shared state. One instance per /autonav-urban/start."""

    config: UrbanRuntimeConfig
    state: UrbanRuntimeState
    planner_yaml_path: str
    camera_K_path: str
    T_base_camera_path: str

    # Injected callbacks (main.py wires these to the SDK internals).
    get_frame_base64: Optional[FrameFetcher] = None
    get_data: Optional[DataFetcher] = None
    post_control: Optional[ControlPoster] = None
    get_checkpoints_list: Optional[CheckpointsFetcher] = None
    checkpoint_reached: Optional[CheckpointReporter] = None

    # Optional pre-loaded SAM-TP model. When main.py's warmup task has already
    # loaded the model (and run a dummy inference to compile MPS kernels), it
    # hands it in here so /autonav-urban/start doesn't pay the ~5-8 s reload
    # cost on the click path.
    preloaded_samtp: Any = None
    # Optional pre-loaded CLIPSeg model. Same rationale as preloaded_samtp:
    # loading the HuggingFace CLIPSeg checkpoint takes 5-8 s cold; we do it
    # in the warmup task at server boot so /autonav-urban/start is fast.
    preloaded_clipseg: Any = None

    _tasks: list[asyncio.Task] = field(default_factory=list)
    _locks: dict[str, asyncio.Lock] = field(default_factory=make_state_locks)
    _samtp: Any = None
    _clipseg: Any = None
    _planner_yaml: dict = field(default_factory=dict)
    _planner_config: Any = None
    _K: Any = None
    _T_base_camera: Any = None
    _log_dir: Optional[Path] = None
    _stop_event: Optional[asyncio.Event] = None

    # Per-run cached state for driving:
    _last_planned_from_gps: Optional[tuple[float, float]] = None   # (lat, lon) at last plan
    _last_planned_goal_xy: Optional[tuple[float, float]] = None    # goal_x_m, goal_y_m at last plan
    _last_planned_seq: Optional[int] = None                        # mission sequence at last plan
    _last_plan_visualization: Optional[np.ndarray] = None
    _max_plan_age_s: float = 8.0                                    # force replan if plan older than this
    _rtm_stale_logged_age: float = 0.0                              # last logged rover_age (avoid log spam)
    # Ring buffer of the last N lookahead points chosen in the control
    # loop. Averaging them kills the "arc flipped between replans" wobble
    # where pure_pursuit's angular alternated between +0.89 and -1.0 every
    # tick. Buffer size 3 = ~1.5s smoothing at control_hz=2.
    _lookahead_history: list = field(default_factory=list)
    _lookahead_history_maxlen: int = 3
    # Track the checkpoint the control loop is currently steering to. When
    # this changes (a CP is scored → new target), we clear the lookahead
    # history and reset the plan-cache so a stale plan/target from the
    # previous CP can't contaminate the first commands toward the new CP.
    _control_seq_seen: int = 0

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self.state.running:
            raise RuntimeError("Runtime already started")

        self._stop_event = asyncio.Event()
        run_id = time.strftime("urban_%Y%m%d_%H%M%S")
        self._log_dir = LOGS_ROOT / run_id
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self.state.log_dir = str(self._log_dir)

        # Config + calibration
        self._planner_yaml = _load_planner_yaml(Path(self.planner_yaml_path))
        self._K = load_camera_K(self.camera_K_path)
        self._T_base_camera = load_T_base_camera(self.T_base_camera_path)

        from genie_path_planner.pipeline import planner_config_from_dict
        self._planner_config = planner_config_from_dict(self._planner_yaml)

        # SAM-TP model — reuse the pre-warmed one if main.py handed it in,
        # otherwise load fresh (adds ~5-8 s to Start on MPS).
        if self.preloaded_samtp is not None:
            self._samtp = self.preloaded_samtp
            logger.info("SAM-TP reused from warmup on %s", self._samtp.device)
        else:
            from .samtp import SAMTPModel, pick_device
            samtp_cfg = self._planner_yaml.get("samtp", {}) or {}
            cfg_path = str(THIRD_PARTY_ROOT / samtp_cfg.get(
                "config_path", "sam2/configs/sam2.1_inference_tiny/sam2.1_custom2.yaml"
            ))
            ckpt_path = str(THIRD_PARTY_ROOT / samtp_cfg.get(
                "checkpoint_path", "sam2_ckpt/checkpoint_2.pt"
            ))
            logger.info("Loading SAM-TP on %s ...", pick_device())
            t0 = time.time()
            self._samtp = SAMTPModel(
                cfg_path=cfg_path,
                ckpt_path=ckpt_path,
                device=None,
                score_thresh=float(samtp_cfg.get("score_thresh", 0.0)),
                multimask=bool(samtp_cfg.get("multimask", False)),
            )
            logger.info("SAM-TP loaded (%.2f s) on %s", time.time() - t0, self._samtp.device)

        # Optional CLIPSeg overlay — text-prompted obstacle segmenter that
        # runs alongside SAM-TP. Warmup usually hands the pre-warmed model
        # in via preloaded_clipseg; if not, load fresh now.
        if getattr(self.config, "clipseg_enabled", True):
            if self.preloaded_clipseg is not None:
                self._clipseg = self.preloaded_clipseg
                logger.info("CLIPSeg reused from warmup on %s", self._clipseg.device)
            else:
                try:
                    from .clipseg import CLIPSegModel
                    logger.info("Loading CLIPSeg on %s ...", "auto")
                    t0 = time.time()
                    self._clipseg = CLIPSegModel(
                        prompts=list(self.config.clipseg_prompts),
                        confidence_thresh=float(self.config.clipseg_confidence_thresh),
                    )
                    logger.info(
                        "CLIPSeg loaded (%.2f s) on %s",
                        time.time() - t0, self._clipseg.device,
                    )
                except Exception as exc:
                    # Non-fatal: run with SAM-TP only, log a big warning.
                    logger.warning(
                        "CLIPSeg failed to load — continuing with SAM-TP only (%s)", exc,
                    )
                    self._clipseg = None

        self.state.running = True
        self.state.mode = "starting"
        self.state.iterations = 0
        self.state.error_streak = 0
        self.state.last_error = None
        self._last_planned_from_gps = None

        # Spawn loops
        self._tasks.append(asyncio.create_task(self.telemetry_loop(), name="urban_telemetry"))
        self._tasks.append(asyncio.create_task(self.perception_loop(), name="urban_perception"))
        self._tasks.append(asyncio.create_task(self.planning_loop(), name="urban_planning"))
        self._tasks.append(asyncio.create_task(self.control_loop(), name="urban_control"))

        # Mission loop only if we have checkpoint plumbing
        if self.get_checkpoints_list is not None and self.checkpoint_reached is not None:
            self._tasks.append(asyncio.create_task(self.mission_loop(), name="urban_mission"))

        self.state.mode = "driving"

    async def stop(self, reason: str = "user_requested") -> None:
        if not self.state.running:
            return
        self.state.running = False
        self.state.mode = "stopped"
        self.state.last_error = f"stopped: {reason}"
        if self._stop_event is not None:
            self._stop_event.set()

        # Best-effort: send a zero-command to halt the rover if we have the callback.
        if self.post_control is not None and not self.config.dry_run:
            try:
                await asyncio.wait_for(self.post_control(0.0, 0.0, 0), timeout=1.0)
            except Exception:
                pass

        for t in self._tasks:
            if not t.done():
                t.cancel()
        for t in self._tasks:
            try:
                await asyncio.wait_for(t, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self._tasks.clear()
        logger.info("Runtime stopped (%s)", reason)

    # ------------------------------------------------------------------ telemetry

    async def telemetry_loop(self) -> None:
        """Poll /data. Softly handles None/missing values so a slow-to-warm-up
        rover doesn't tip us into mode=error before the first real frame.
        Only counts real exceptions toward the error streak, not empty reads.
        """
        assert self.get_data is not None
        period = 1.0 / max(0.5, self.config.telemetry_hz)
        empty_reads = 0                        # consecutive None/{} reads (not fatal)

        while self.state.running:
            t_start = time.time()
            try:
                raw = await self.get_data()
                # browser_service.data() can return None while the RTM/Agora
                # stream is still warming up. Not an exception — just skip.
                if not isinstance(raw, dict) or not raw:
                    empty_reads += 1
                    if empty_reads % 20 == 1:   # log every ~4s at 5Hz
                        logger.info("Telemetry warmup: /data still empty (attempt %d)", empty_reads)
                    await asyncio.sleep(max(0.01, period - (time.time() - t_start)))
                    continue

                empty_reads = 0
                # window.rtm_data.timestamp is set by basicRtm.js when a peer
                # message arrives from the rover. If it stops advancing while
                # our local clock ticks forward, the RTM channel is dead —
                # even though /data keeps returning stale-but-valid JSON.
                rover_ts = 0.0
                try:
                    rover_ts = float(raw.get("timestamp") or 0.0)
                except (TypeError, ValueError):
                    rover_ts = 0.0
                snap = TelemetrySnapshot(
                    latitude=raw.get("latitude"),
                    longitude=raw.get("longitude"),
                    yaw_deg=raw.get("orientation"),
                    speed_ms=raw.get("speed"),
                    battery=raw.get("battery"),
                    gps_signal=raw.get("gps_signal"),
                    ts=t_start,
                    rover_ts=rover_ts,
                )
                async with self._locks["telemetry"]:
                    self.state.last_telemetry = snap
                # RTM staleness detection: if we haven't seen a fresh rover
                # message in > 10 s, log clearly. Otherwise driving through
                # a dead link produces zero-effect commands and looks like
                # a controller bug.
                if rover_ts > 0.0:
                    rover_age = t_start - rover_ts
                    if rover_age > 10.0 and self._rtm_stale_logged_age < rover_age - 5.0:
                        logger.warning(
                            "RTM link appears stale: last rover message %.1f s ago. "
                            "Commands may not be reaching the rover. Restart the server "
                            "if this persists.", rover_age,
                        )
                        self._rtm_stale_logged_age = rover_age
                # Reset error streak once we get a good read
                if self.state.error_streak > 0:
                    self.state.error_streak = 0
                # Safety: battery cutoff
                if snap.battery is not None and snap.battery <= self.config.battery_floor:
                    logger.warning("Battery %s%% <= floor %s%%, stopping",
                                   snap.battery, self.config.battery_floor)
                    self.state.mode = "battery_low"
                    self.state.running = False
                    break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state.error_streak += 1
                self.state.last_error = f"telemetry: {exc}"
                logger.warning("Telemetry error: %s", exc)
                # Don't cascade to fatal 'error' — perception/planning/control
                # can still run without fresh telemetry (mission_loop just waits).
                # Only complain hard after many streak errors.
                if self.state.error_streak >= self.config.max_error_streak * 3:
                    logger.warning("Telemetry has failed %d times — flagging error but continuing",
                                   self.state.error_streak)
            await asyncio.sleep(max(0.01, period - (time.time() - t_start)))

    # ------------------------------------------------------------------ perception

    async def perception_loop(self) -> None:
        assert self.get_frame_base64 is not None
        period = 1.0 / max(0.5, self.config.perception_target_hz)

        projection_cfg = (self._planner_yaml.get("projection") or {})
        bev_res = float(projection_cfg.get("resolution_m_per_px", 0.05))
        bev_forward = float(projection_cfg.get("forward_range_m", 6.0))
        bev_side = float(projection_cfg.get("side_range_m", 2.5))
        max_ray = float(projection_cfg.get("max_ray_distance_m", 10.0))
        ground_z = float(projection_cfg.get("ground_z", 0.0))
        score_transform = str((self._planner_yaml.get("samtp") or {}).get("score_transform", "sigmoid"))

        while self.state.running:
            t_start = time.time()
            try:
                b64 = await self.get_frame_base64("front")
                rgb = await asyncio.to_thread(_decode_frame_b64, b64)

                snap = self.state.last_telemetry
                yaw_deg = float(snap.yaw_deg) if snap.yaw_deg is not None else 0.0

                # SAM-TP + CLIPSeg run in PARALLEL on MPS via asyncio.gather.
                # Each is pushed to a worker thread so telemetry/control/mission
                # loops keep ticking. Wall-clock ≈ max(sam-tp, clipseg), NOT
                # sum, because Python asyncio + MPS runs them concurrently.
                _t_infer_start = time.time()
                sam_task = asyncio.to_thread(self._samtp.run_sam2_inference, rgb)
                clipseg_mask_np = None
                if self._clipseg is not None:
                    clipseg_task = asyncio.to_thread(self._clipseg.predict, rgb)
                    out, clipseg_mask_np = await asyncio.gather(sam_task, clipseg_task)
                else:
                    out = await sam_task
                _t_infer = time.time() - _t_infer_start
                if self.state.iterations % 20 == 0:
                    logger.info(
                        "Perception inference: %.2fs  (SAM-TP %s + CLIPSeg %s)",
                        _t_infer,
                        "yes",
                        "yes" if self._clipseg is not None else "off",
                    )

                from genie_path_planner.projection import logits_to_traversability
                trav = await asyncio.to_thread(logits_to_traversability, out["logits"], score_transform)

                # SAM-TP checkpoint doesn't generalize to Mini+ imagery — it
                # labels other rovers / chairs / low objects on the ground as
                # drivable because it wasn't trained on those. Refine here:
                # any pixel SAM-TP called drivable but is significantly darker
                # than the median drivable pixel is very likely an obstacle
                # sitting on the ground. See samtp.py:refine_traversability_by_contrast.
                if getattr(self.config, "contrast_refine_enabled", True):
                    from .samtp import refine_traversability_by_contrast
                    trav_before = trav
                    trav = await asyncio.to_thread(
                        refine_traversability_by_contrast,
                        trav, rgb,
                        float(getattr(self.config, "contrast_drivable_thresh", 0.5)),
                        float(getattr(self.config, "contrast_darkness_ratio", 0.65)),
                    )
                    if self.state.iterations % 20 == 0:
                        n_downgraded = int(np.count_nonzero((trav_before > 0.5) & (trav <= 0.5)))
                        if n_downgraded > 0:
                            logger.info(
                                "Contrast refine: downgraded %d SAM-TP-drivable pixels → obstacle",
                                n_downgraded,
                            )

                # CLIPSeg fusion: trav_final = trav_samtp × (1 − alpha × clipseg_mask).
                # This lets CLIPSeg VETO SAM-TP wherever it recognises a named
                # obstacle (grass, rover, car, person, ...). If CLIPSeg isn't
                # loaded (failed at startup or disabled in config), we just
                # skip this step.
                if clipseg_mask_np is not None and clipseg_mask_np.shape == trav.shape:
                    alpha = float(getattr(self.config, "clipseg_alpha", 0.9))
                    trav_before_clip = trav
                    trav = trav * (1.0 - alpha * clipseg_mask_np.astype(np.float32))
                    trav = np.clip(trav, 0.0, 1.0)
                    async with self._locks["perception"]:
                        self.state.last_clipseg_mask = clipseg_mask_np
                        self.state.last_clipseg_ts = time.time()
                    if self.state.iterations % 20 == 0:
                        n_vetoed = int(np.count_nonzero(
                            (trav_before_clip > 0.5) & (trav <= 0.5)
                        ))
                        max_clip = float(clipseg_mask_np.max()) if clipseg_mask_np.size else 0.0
                        logger.info(
                            "CLIPSeg fusion: max=%.2f  vetoed %d SAM-TP-drivable pixels",
                            max_clip, n_vetoed,
                        )

                # Diagnostic: log raw-logit distribution every 20 iters so we
                # can tell if the model is saturating (all-positive → labels
                # every pixel as drivable — the "hedge is green" failure mode).
                # Healthy output: min<0, max>0, roughly zero-centered.
                if self.state.iterations % 20 == 0:
                    lo = float(np.nanmin(out["logits"]))
                    hi = float(np.nanmax(out["logits"]))
                    mean = float(np.nanmean(out["logits"]))
                    trav_finite = trav[np.isfinite(trav)]
                    frac_drivable = float(np.mean(trav_finite > 0.5)) if trav_finite.size else 0.0
                    logger.info(
                        "SAMTP logits: min=%+.2f mean=%+.2f max=%+.2f  trav>0.5 frac=%.2f",
                        lo, mean, hi, frac_drivable,
                    )

                bev, observed, _stats = await asyncio.to_thread(
                    project_frame_to_bev,
                    trav,
                    yaw_deg,
                    self._K,
                    self._T_base_camera,
                    ground_z,
                    bev_res,
                    bev_forward,
                    bev_side,
                    max_ray,
                )
                # Temporal smoothing (EMA). Cuts frame-to-frame perception
                # noise that makes the planner flip between near-equivalent
                # paths on straight sections. Only applied when previous BEV
                # exists with matching shape — first frame passes through.
                # NOTE: assumes small pose change between frames. Valid at
                # slow (confined) speeds; may lag at full cruise speed.
                if (
                    getattr(self.config, "bev_ema_enabled", False)
                    and self.state.last_bev is not None
                    and getattr(self.state.last_bev, "shape", None) == bev.shape
                ):
                    alpha = float(self.config.bev_ema_alpha)
                    alpha = max(0.05, min(1.0, alpha))
                    prev_bev = np.asarray(self.state.last_bev, dtype=np.float32)
                    prev_obs = np.asarray(self.state.last_observed_mask, dtype=bool)
                    # Blend only in cells both frames observed. Cells unique to
                    # one frame take that frame's value. Unknown-in-both stays
                    # unknown.
                    new_obs = observed.astype(bool)
                    both = prev_obs & new_obs
                    only_new = new_obs & ~prev_obs
                    only_old = prev_obs & ~new_obs
                    blended = np.full_like(bev, -1.0, dtype=np.float32)
                    blended[both] = alpha * bev[both] + (1.0 - alpha) * prev_bev[both]
                    blended[only_new] = bev[only_new]
                    blended[only_old] = prev_bev[only_old]
                    bev = blended
                    observed = new_obs | prev_obs
                async with self._locks["perception"]:
                    self.state.last_bev = bev
                    self.state.last_observed_mask = observed.astype(bool)
                    self.state.last_bev_ts = t_start
                    self.state.last_samtp_trav = trav
                    self.state.last_samtp_logits = out["logits"]
                    self.state.last_samtp_ts = t_start
                    self.state.iterations += 1
                self.state.error_streak = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state.error_streak += 1
                self.state.last_error = f"perception: {exc}"
                # Log every 20th error to avoid spamming the terminal when the
                # video stream is unavailable (imageFormat undefined loop).
                if self.state.error_streak % 20 == 1:
                    logger.warning(
                        "Perception error x%d: %s",
                        self.state.error_streak, str(exc)[:140],
                    )
                # Exponential backoff: 100ms → 200ms → ... → capped at 2s
                sleep_s = min(2.0, 0.1 * (2 ** min(self.state.error_streak, 5)))
                await asyncio.sleep(sleep_s)
                continue
            elapsed = time.time() - t_start
            await asyncio.sleep(max(0.0, period - elapsed))

    # ------------------------------------------------------------------ planning

    def _bev_resolution(self) -> float:
        return float((self._planner_yaml.get("projection") or {}).get("resolution_m_per_px", 0.05))

    def _needs_replan(
        self,
        cur_gps: Optional[tuple[float, float]],
        cur_goal: tuple[float, float],
        cur_seq: int,
    ) -> bool:
        """Replan on ANY of:
        - never planned yet
        - mission sequence changed (goal jumped to next checkpoint)
        - goal_x_m/goal_y_m shifted materially since last plan
        - rover has traveled >= planner_replan_distance_m
        - plan is older than _max_plan_age_s (fail-safe against stale plans)
        """
        if self.state.last_path_xy_m is None:
            return True
        # Sequence changed → new checkpoint, replan immediately
        if self._last_planned_seq is None or cur_seq != self._last_planned_seq:
            return True
        # Goal shifted materially (e.g. yaw changed, dead-reckoning update)
        if self._last_planned_goal_xy is not None:
            dgx = cur_goal[0] - self._last_planned_goal_xy[0]
            dgy = cur_goal[1] - self._last_planned_goal_xy[1]
            if (dgx * dgx + dgy * dgy) ** 0.5 >= 1.0:      # 1 m body-frame shift
                return True
        # Plan too old → force replan (safety net against a stuck loop)
        if self.state.last_plan_ts > 0 and (time.time() - self.state.last_plan_ts) > self._max_plan_age_s:
            return True
        # Otherwise, only replan after we've moved
        if cur_gps is None or self._last_planned_from_gps is None:
            return True
        traveled = _haversine_m(
            self._last_planned_from_gps[0], self._last_planned_from_gps[1],
            cur_gps[0], cur_gps[1],
        )
        return traveled >= float(self.config.planner_replan_distance_m)

    async def planning_loop(self) -> None:
        from genie_path_planner.planner import plan_on_bev

        # Diagnostic counters
        skip_reasons: dict[str, int] = {}
        last_diag_ts = time.time()

        def _skip(reason: str):
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

        # Poll ~4 Hz but only actually plan when needed.
        while self.state.running:
            try:
                await asyncio.sleep(0.25)

                # Print skip-reason distribution every 8s so we can see WHY
                # planning is idle.
                if time.time() - last_diag_ts > 8.0:
                    logger.info("Planning loop skips in last 8s: %s", dict(skip_reasons))
                    skip_reasons.clear()
                    last_diag_ts = time.time()

                if self.state.mode not in {"driving", "starting", "scoring"}:
                    _skip(f"mode={self.state.mode}")
                    continue

                # Need a fresh BEV
                async with self._locks["perception"]:
                    bev = self.state.last_bev
                    observed = self.state.last_observed_mask
                    bev_ts = self.state.last_bev_ts
                if bev is None:
                    _skip("bev_none")
                    continue
                bev_age = time.time() - bev_ts
                if bev_age > 2.0:
                    _skip(f"bev_stale_{bev_age:.1f}s")
                    continue

                # Need a goal — either from mission loop (state.goal_x_m/y_m) or a fake "straight ahead" fallback
                async with self._locks["mission"]:
                    goal_x = float(self.state.goal_x_m)
                    goal_y = float(self.state.goal_y_m)
                    cur_seq = int(self.state.current_seq)

                snap = self.state.last_telemetry
                cur_gps = (
                    (snap.latitude, snap.longitude)
                    if snap.latitude is not None and snap.longitude is not None
                    else None
                )
                if not self._needs_replan(cur_gps, (goal_x, goal_y), cur_seq):
                    _skip("no_replan_needed")
                    continue

                # About to plan
                logger.info(
                    "Planning: goal=(%.2f, %.2f) seq=%d bev_age=%.2fs",
                    goal_x, goal_y, cur_seq, bev_age,
                )

                # plan_on_bev is a heavy sync NumPy call (200-400ms with clustering).
                # Push to a worker thread so control/mission loops keep responsive.
                planned = await asyncio.to_thread(
                    plan_on_bev,
                    bev,               # bev_traversability
                    observed,          # observed_mask
                    goal_x,            # goal_x_m
                    goal_y,            # goal_y_m
                    self._bev_resolution(),
                    self._planner_config,
                )
                # Overlay a yellow dashed reference line from rover to goal on the
                # plan visualization. When the chosen path (red) diverges from
                # this yellow line, the planner picked an asymmetric route.
                plan_vis_with_ref = planned.visualization
                try:
                    plan_vis_with_ref = _overlay_ideal_centerline(
                        planned.visualization,
                        goal_x,
                        goal_y,
                        self._bev_resolution(),
                    )
                except Exception as exc:
                    logger.debug("centerline overlay failed: %s", exc)
                async with self._locks["planning"]:
                    self.state.last_path_xy_m = planned.final_path_xy_m
                    self.state.last_plan_visualization = plan_vis_with_ref
                    self.state.last_plan_meta = dict(planned.metadata or {})
                    self.state.last_plan_ts = time.time()
                self._last_planned_from_gps = cur_gps
                self._last_planned_goal_xy = (goal_x, goal_y)
                self._last_planned_seq = cur_seq

                # Log every plan outcome so we can see whether planner is
                # producing usable paths.
                n_points = 0
                try:
                    n_points = int(getattr(planned.final_path_xy_m, "shape", (0,))[0])
                except Exception:
                    pass
                logger.info(
                    "Plan done: status=%s points=%d",
                    planned.metadata.get("status"), n_points,
                )
                if planned.metadata.get("status") != "ok":
                    logger.warning("Planner returned status=%s", planned.metadata.get("status"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state.error_streak += 1
                self.state.last_error = f"planning: {exc}"
                logger.warning("Planning error: %s", exc, exc_info=False)
                # Same soft-fail rule: back off, don't fatal-out.
                if self.state.error_streak >= self.config.max_error_streak:
                    await asyncio.sleep(1.0)

    # ------------------------------------------------------------------ control

    def _current_mission_target(self) -> Optional[dict]:
        """Return the checkpoint dict matching state.current_seq, or None if
        we're in free-drive mode (no checkpoints) or the mission is done."""
        seq = int(self.state.current_seq)
        for c in self.state.checkpoints or []:
            try:
                if int(c.get("sequence", 0)) == seq:
                    return c
            except (TypeError, ValueError):
                continue
        return None

    async def control_loop(self) -> None:
        period = 1.0 / max(1.0, self.config.control_hz)
        bev_res = self._bev_resolution()

        while self.state.running:
            t_start = time.time()
            try:
                # Halt-if-not-driving states
                if self.state.mode not in {"driving", "scoring"}:
                    await self._send_control(0.0, 0.0, reason="not_driving")
                    await asyncio.sleep(period)
                    continue

                # Collision monitor
                bev = self.state.last_bev
                obs = self.state.last_observed_mask
                hazard = False
                if bev is not None and obs is not None:
                    hazard = front_strip_hazard(
                        bev=bev,
                        observed_mask=obs,
                        bev_resolution_m=bev_res,
                        forward_m=self.config.collision_forward_m,
                        half_width_m=self.config.collision_half_width_m,
                        trav_threshold=self.config.collision_trav_thresh,
                        hazard_fraction=self.config.collision_hazard_fraction,
                    )
                if hazard:
                    await self._send_control(0.0, 0.0, reason="hazard")
                    await asyncio.sleep(period)
                    continue

                # DO NOT send stop just because a plan is stale — the
                # planner loop will refresh it. If we sent (0, 0) here we
                # would repeatedly interrupt in-flight rover motion the
                # instant the plan hits 8s old, then let it start again on
                # the next plan. That STOP/GO/STOP/GO pattern is exactly
                # what prevented all motion in earlier tests.
                #
                # If planning is TRULY dead (>30s stale), fall through to
                # send stop as a safety measure. Otherwise hold the last
                # good command.
                path = self.state.last_path_xy_m
                plan_age = time.time() - self.state.last_plan_ts if self.state.last_plan_ts > 0 else float("inf")
                if plan_age > 30.0:
                    await self._send_control(0.0, 0.0, reason="stale_plan_hard")
                    await asyncio.sleep(period)
                    continue

                # ---- CP transition detection --------------------------
                # If the mission loop scored the last CP and advanced to a
                # new sequence, blow away the previous CP's residual state
                # (lookahead smoothing buffer, path) so we don't steer
                # toward the old target for the first tick or two.
                cur_seq_now = int(self.state.current_seq)
                if cur_seq_now != self._control_seq_seen and cur_seq_now > 0:
                    if self._control_seq_seen != 0:
                        # Log which target we just moved to and where it is
                        # RIGHT NOW in the body frame. Distinguishes "CP is
                        # ahead → smooth continuation" from "CP is behind
                        # → we should immediately turn around".
                        tgt_after = self._current_mission_target()
                        s_here = self.state.last_telemetry
                        gy_desc = "?"
                        gbear_desc = "?"
                        if (tgt_after is not None
                                and s_here.latitude is not None
                                and s_here.longitude is not None
                                and s_here.yaw_deg is not None):
                            try:
                                gx_h, gy_h, _dh = gps_to_local_goal(
                                    current_lat=float(s_here.latitude),
                                    current_lon=float(s_here.longitude),
                                    current_yaw_deg=float(s_here.yaw_deg),
                                    target_lat=float(tgt_after["latitude"]),
                                    target_lon=float(tgt_after["longitude"]),
                                    virtual_range_m=float(self.config.goal_virtual_range_m),
                                )
                                gy_desc = "ahead" if gy_h >= 0 else "BEHIND"
                                gbear_desc = f"{math.degrees(math.atan2(gx_h, gy_h)):+.0f}°"
                            except Exception:
                                pass
                        logger.info(
                            "CP transition: control switching from seq=%d to seq=%d "
                            "(new target is %s, bearing %s from bot)",
                            self._control_seq_seen, cur_seq_now, gy_desc, gbear_desc,
                        )
                    self._lookahead_history.clear()
                    async with self._locks["planning"]:
                        # Drop the stale plan too so pursuit doesn't chase
                        # points computed for the previous CP.
                        self.state.last_path_xy_m = None
                    self._control_seq_seen = cur_seq_now

                # ---- Closed-loop align-first check --------------------
                # Recompute goal bearing in the CURRENT body frame using
                # FRESH telemetry. This runs at control rate (2 Hz), so
                # even during rotation the bot always knows how much
                # further to turn. If we're misaligned enough that
                # forward motion is wasted → turn in place with
                # proportional feedback (which naturally stops itself
                # once aligned). Only if we're already reasonably aimed
                # at the goal do we run the planner-driven pure-pursuit.
                snap_ctrl = self.state.last_telemetry
                tgt_ctrl = self._current_mission_target()
                fresh_goal_bearing: Optional[float] = None
                if (tgt_ctrl is not None and snap_ctrl.latitude is not None
                        and snap_ctrl.longitude is not None
                        and snap_ctrl.yaw_deg is not None):
                    live_gx, live_gy, _ = gps_to_local_goal(
                        current_lat=float(snap_ctrl.latitude),
                        current_lon=float(snap_ctrl.longitude),
                        current_yaw_deg=float(snap_ctrl.yaw_deg),
                        target_lat=float(tgt_ctrl["latitude"]),
                        target_lon=float(tgt_ctrl["longitude"]),
                        virtual_range_m=float(self.config.goal_virtual_range_m),
                    )
                    # Plain atan2 across the full [-π, π] range so goals BEHIND
                    # the rover (live_gy < 0) return the correct bearing instead
                    # of a clamped ±π/2. Previously we clamped y to 1e-3 which
                    # made every "behind-right" or "behind-left" goal look like
                    # "straight side" — bot oscillated across the gx=0 boundary
                    # instead of rotating to face the goal.
                    fresh_goal_bearing = math.atan2(live_gx, live_gy)

                if fresh_goal_bearing is not None:
                    bearing_deg = abs(math.degrees(fresh_goal_bearing))
                    # Hysteresis: ENTER align above align_thresh_deg, and STAY in
                    # align until bearing drops below align_deadband_deg. A single
                    # hard threshold made the rover flip between align (linear=0)
                    # and pursuit every tick on noisy bearings — and each flip
                    # wiped the lookahead smoothing buffer, defeating it.
                    if not self._aligning and bearing_deg > self.config.align_thresh_deg:
                        self._aligning = True
                        # Rotation invalidates the body-frame lookahead history —
                        # clear it ONCE on entry, not on every align tick.
                        self._lookahead_history.clear()
                    elif self._aligning and bearing_deg <= self.config.align_deadband_deg:
                        self._aligning = False
                    if self._aligning:
                        lin, ang = align_in_place(fresh_goal_bearing, self.config)
                        await self._send_control(float(lin), float(ang), reason="align")
                        await asyncio.sleep(max(0.0, period - (time.time() - t_start)))
                        continue

                # Pure pursuit — but if we have no path yet, do NOT emit
                # a stop command every tick (that too resets the motors).
                # Just skip this tick and wait for a plan.
                target = pick_lookahead(path, self.config.lookahead_m) if path is not None else None
                # Smooth the lookahead across recent ticks so a single
                # noisy plan can't yank the steering hard-over. Averaging
                # the last N raw lookahead points converts flip-flopping
                # arcs into a smooth blended one.
                if target is not None:
                    self._lookahead_history.append(target)
                    if len(self._lookahead_history) > self._lookahead_history_maxlen:
                        self._lookahead_history.pop(0)
                    sx = sum(t[0] for t in self._lookahead_history) / len(self._lookahead_history)
                    sy = sum(t[1] for t in self._lookahead_history) / len(self._lookahead_history)
                    target = (sx, sy)
                else:
                    self._lookahead_history.clear()
                if target is None:
                    # First-plan-not-ready case. Only emit ONE stop; after
                    # that dedup will skip. Rover holds whatever it was
                    # doing (typically stopped if brand new start).
                    await self._send_control(0.0, 0.0, reason="no_path")
                else:
                    tx, ty = float(target[0]), float(target[1])
                    gx, gy = float(self.state.goal_x_m), float(self.state.goal_y_m)
                    # Full-range atan2 (no ty/gy clamp) so a target/goal BEHIND the
                    # rover reads as ±180° instead of a clamped ±90° that oscillates
                    # across the boundary.
                    path_heading = math.atan2(tx, ty)
                    goal_heading = math.atan2(gx, gy)
                    heading_diff_deg = abs(math.degrees(path_heading - goal_heading))
                    if heading_diff_deg > 180:
                        heading_diff_deg = 360 - heading_diff_deg
                    # Hysteresis: ENTER override above goal_override_thresh_deg, EXIT
                    # below goal_override_exit_thresh_deg — stops the path<->goal
                    # steering target from flipping every replan near the boundary.
                    if not self._goal_overriding and heading_diff_deg > self.config.goal_override_thresh_deg:
                        self._goal_overriding = True
                    elif self._goal_overriding and heading_diff_deg <= self.config.goal_override_exit_thresh_deg:
                        self._goal_overriding = False
                    if self._goal_overriding:
                        # Plan and goal disagree strongly — aim at a virtual
                        # target in the goal direction at lookahead distance.
                        gn = (gx * gx + gy * gy) ** 0.5
                        if gn > 1e-3:
                            scale = self.config.lookahead_m / gn
                            target = (gx * scale, gy * scale)
                        lin, ang = pure_pursuit(target, self.config)
                        reason = "pursuit_goal_override"
                    else:
                        lin, ang = pure_pursuit(target, self.config)
                        reason = "pursuit"

                    # Adaptive slowdown: if the scene is confined (walls close
                    # on both sides), cap the forward speed. Slower motion =
                    # fresher plans stay valid longer = safer in narrow gaps.
                    cap = self._confinement_cap(bev, obs)
                    if cap is not None and lin > cap:
                        # Respect the firmware deadband: never floor below
                        # min_linear when moving, else the rover stalls.
                        lin = max(float(self.config.min_linear), cap)
                        reason = reason + "_confined"

                    await self._send_control(float(lin), float(ang), reason=reason)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state.last_error = f"control: {exc}"
                logger.warning("Control error: %s", exc)
            elapsed = time.time() - t_start
            await asyncio.sleep(max(0.0, period - elapsed))

    _control_log_counter: int = 0
    _last_sent_linear: float = 0.0
    _last_sent_angular: float = 0.0
    _last_sent_ts: float = 0.0
    _last_hold_log_ts: float = 0.0
    _aligning: bool = False           # hysteresis state for align-in-place mode
    _goal_overriding: bool = False    # hysteresis state for path->goal override

    def _confinement_cap(self, bev, obs) -> Optional[float]:
        """Return a linear-speed cap when the rover is in a confined scene
        (walls close on both sides), else None. Uses the fraction of observed
        BEV cells within a radius that are obstacles as the confinement signal.

        Side effect: updates self.state.confined_active + confined_obstacle_ratio
        so the dashboard can show whether we're currently in confined mode.
        """
        if not getattr(self.config, "confined_speed_enabled", False):
            self.state.confined_active = False
            return None
        if bev is None or obs is None:
            return None
        res = self._bev_resolution()
        if res <= 0.0:
            return None
        H, W = bev.shape[:2]
        r_m = float(self.config.confined_check_radius_m)
        r_px = max(1, int(round(r_m / res)))
        cr, cc = H - 1, W // 2
        r0 = max(0, cr - r_px)
        r1 = cr + 1
        c0 = max(0, cc - r_px)
        c1 = min(W, cc + r_px + 1)
        patch = bev[r0:r1, c0:c1]
        obs_patch = obs[r0:r1, c0:c1]
        seen = obs_patch & np.isfinite(patch)
        if not seen.any():
            self.state.confined_active = False
            return None
        # An "obstacle" cell for this purpose = below the collision threshold.
        thresh = float(getattr(self.config, "collision_trav_thresh", 0.1)) or 0.1
        obstacles = seen & (patch < thresh)
        ratio = float(obstacles.sum()) / float(seen.sum())
        self.state.confined_obstacle_ratio = ratio
        need = float(self.config.confined_obstacle_ratio_thresh)
        if ratio >= need:
            self.state.confined_active = True
            return float(self.config.confined_speed_max)
        self.state.confined_active = False
        return None

    async def _send_control(self, linear: float, angular: float, reason: str = "") -> None:
        async with self._locks["control"]:
            self.state.last_linear = float(linear)
            self.state.last_angular = float(angular)
            self.state.last_control_ts = time.time()
            self.state.last_control_reason = reason
        if self.config.dry_run:
            return
        if self.post_control is None:
            return

        # ROVER FIRMWARE BEHAVIOR (empirically determined):
        #   - Sending at 10 Hz: motors constantly reset, no motion
        #   - Sending ONCE: rover holds command for ~4s then auto-stops
        #   - Sending every 1-2s: rover keeps executing smoothly (openClaw pattern)
        #
        # So we send at ~1 Hz UNCONDITIONALLY. No dedup. Same-command resends
        # at 1 Hz keep the rover firmware's motor cycle alive without
        # cancelling in-flight motion. This matches the working openClaw
        # tick_ms=1500 pattern.
        now = time.time()
        # Send FRESH commands immediately: a materially-changed linear/angular
        # (e.g. an align correction) must NOT be throttled — the flat 0.9s gate
        # dropped the fresher of every two 2 Hz ticks, reverting control to the
        # ~1 Hz cadence that caused the runaway pirouette. Only an UNCHANGED
        # command is resent at ~1 Hz to keep the firmware's motor cycle alive
        # (avoids the 10 Hz spam that stalls the motors).
        changed = (abs(linear - self._last_sent_linear) > 0.02
                   or abs(angular - self._last_sent_angular) > 0.02)
        if not changed and (now - self._last_sent_ts) < 0.9:
            return

        self._last_sent_linear = float(linear)
        self._last_sent_angular = float(angular)
        self._last_sent_ts = now

        try:
            await asyncio.wait_for(self.post_control(linear, angular, 0), timeout=1.5)
            # Sends are now rare (only on material change), so log EVERY one
            # for clear diagnostics.
            # Zigzag debug: also log goal_local + how far chosen path leans off
            # from goal direction. If ang flips sign every 1-2 sends while the
            # goal bearing stays steady, that's plan flipping, not real steering.
            gx = float(getattr(self.state, "goal_x_m", 0.0))
            gy = float(getattr(self.state, "goal_y_m", 0.0))
            goal_bearing_deg = math.degrees(math.atan2(gx, gy)) if (gx or gy) else 0.0
            logger.info(
                "SEND -> linear=%.2f angular=%+.2f reason=%s  yaw=%.1f goal(%.1f,%.1f)=%+.0f° dist=%.1f",
                linear, angular, reason,
                self.state.last_telemetry.yaw_deg or 0.0,
                gx, gy, goal_bearing_deg,
                self.state.distance_to_next_m
                if self.state.distance_to_next_m != float("inf") else -1.0,
            )
        except asyncio.TimeoutError:
            logger.warning("POST /control timeout — command may not be reaching rover")
        except Exception as exc:
            logger.warning("POST /control failed: %s", exc)

    # ------------------------------------------------------------------ mission

    async def mission_loop(self) -> None:
        assert self.get_checkpoints_list is not None
        assert self.checkpoint_reached is not None

        # 1. Fetch checkpoints once at start
        try:
            body = await self.get_checkpoints_list()
        except Exception as exc:
            self.state.last_error = f"mission fetch: {exc}"
            self.state.mode = "error"
            return

        checkpoints = list(body.get("checkpoints_list") or [])
        checkpoints.sort(key=lambda c: int(c.get("sequence", 0)))
        latest_scanned = int(body.get("latest_scanned_checkpoint", 0))
        async with self._locks["mission"]:
            self.state.checkpoints = checkpoints
            self.state.current_seq = latest_scanned + 1

        if not checkpoints:
            logger.info("No checkpoints in mission — free-drive mode; mission_loop exits.")
            return

        period = 1.0 / max(0.2, self.config.mission_hz)
        while self.state.running:
            t_start = time.time()
            try:
                target = next(
                    (c for c in checkpoints if int(c.get("sequence", 0)) == self.state.current_seq),
                    None,
                )
                if target is None:
                    logger.info("Mission complete — no CP with sequence %d", self.state.current_seq)
                    self.state.mode = "done"
                    self.state.running = False
                    break

                snap = self.state.last_telemetry
                if snap.latitude is None or snap.longitude is None or snap.yaw_deg is None:
                    await asyncio.sleep(period)
                    continue

                gx, gy, dist = gps_to_local_goal(
                    current_lat=float(snap.latitude),
                    current_lon=float(snap.longitude),
                    current_yaw_deg=float(snap.yaw_deg),
                    target_lat=float(target["latitude"]),
                    target_lon=float(target["longitude"]),
                    virtual_range_m=float(self.config.goal_virtual_range_m),
                )
                async with self._locks["mission"]:
                    self.state.goal_x_m = gx
                    self.state.goal_y_m = gy
                    self.state.distance_to_next_m = dist

                # Auto-skip FIRST CP if it's genuinely behind the rover at
                # start. Prevents the "spin 180° in place to align to CP1
                # that's behind me" failure mode. Only runs on the very
                # first checkpoint of the mission (not on later ones).
                if (getattr(self.config, "auto_skip_first_cp_if_behind", False)
                        and int(target.get("sequence", 0)) == 1
                        and not self.state.history       # nothing scored yet
                        and gy < -float(getattr(self.config, "auto_skip_behind_threshold_m", 1.5))):
                    logger.warning(
                        "CP 1 is BEHIND rover (y=%.1fm, dist=%.1fm) at start — "
                        "auto-skipping to avoid wasted 180° spin. Bot will "
                        "physically reach CPs 2+ normally.",
                        gy, dist,
                    )
                    # Try to notify the backend it's "reached" — if the backend
                    # rejects because we're not close enough, we still skip
                    # locally so mission can progress. Points may be lost.
                    try:
                        resp = await self.checkpoint_reached()
                        next_seq = resp.get("next_checkpoint_sequence")
                    except Exception as exc:
                        logger.warning("Auto-skip: backend rejected checkpoint_reached (%s); advancing locally", exc)
                        next_seq = int(target.get("sequence", 0)) + 1
                    self.state.history.append({
                        "seq": int(target["sequence"]),
                        "distance_m": float(dist),
                        "ts": time.time(),
                        "auto_skipped": True,
                    })
                    if next_seq in (None, 0, ""):
                        self.state.mode = "done"
                        self.state.running = False
                        break
                    async with self._locks["mission"]:
                        self.state.current_seq = int(next_seq)
                    await asyncio.sleep(period)
                    continue

                # Arrival check — configurable via cfg.checkpoint_arrival_m
                # so we can tighten from the earlier 15m default that let the
                # bot "score" without physically reaching the CP.
                if dist < self.config.checkpoint_arrival_m:
                    self.state.mode = "scoring"
                    try:
                        resp = await self.checkpoint_reached()
                        next_seq = resp.get("next_checkpoint_sequence")
                        logger.info("CP %s reached (dist=%.1fm), next=%s",
                                    target["sequence"], dist, next_seq)
                        self.state.history.append({
                            "seq": int(target["sequence"]),
                            "distance_m": float(dist),
                            "ts": time.time(),
                        })
                        if next_seq in (None, 0, ""):
                            self.state.mode = "done"
                            self.state.running = False
                            break
                        async with self._locks["mission"]:
                            self.state.current_seq = int(next_seq)
                        self.state.mode = "driving"
                    except Exception as exc:
                        # Try to parse a backend "proximate_distance_to_checkpoint"
                        proximate = None
                        detail = getattr(exc, "detail", None)
                        if isinstance(detail, dict):
                            proximate = detail.get("proximate_distance_to_checkpoint")
                        elif isinstance(getattr(exc, "args", None), tuple) and exc.args:
                            # Some HTTPException detail shapes
                            first = exc.args[0]
                            if isinstance(first, dict):
                                proximate = first.get("proximate_distance_to_checkpoint")
                        if isinstance(proximate, (int, float)):
                            logger.info("Backend says still %.1fm from CP; keep driving.", proximate)
                            async with self._locks["mission"]:
                                self.state.distance_to_next_m = float(proximate)
                        else:
                            logger.warning("checkpoint_reached failed: %s", exc)
                        self.state.mode = "driving"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state.last_error = f"mission: {exc}"
                logger.warning("Mission-loop error: %s", exc)
            elapsed = time.time() - t_start
            await asyncio.sleep(max(0.0, period - elapsed))

    async def recovery_loop(self) -> None:
        """Phase 10 stub."""
        raise NotImplementedError("Phase 10")

    # ------------------------------------------------------------------ helpers

    def latest_bev_png(self) -> Optional[bytes]:
        if self.state.last_bev is None:
            return None
        vis = _jet_bev(np.asarray(self.state.last_bev, dtype=np.float32), draw_robot_marker=True)
        buf = io.BytesIO()
        Image.fromarray(vis).save(buf, format="PNG")
        return buf.getvalue()

    def latest_samtp_png(self) -> Optional[bytes]:
        """Raw SAM-TP traversability in image space, rendered like the GeNIE
        paper: per-frame min-max normalized JET (RED = drivable, BLUE = obstacle).

        Uses the raw logits (what the paper normalizes); falls back to the sigmoid
        traversability if logits aren't available yet.
        """
        score = self.state.last_samtp_logits
        if score is None:
            score = self.state.last_samtp_trav
        if score is None:
            return None
        vis = _jet_normalized(np.asarray(score, dtype=np.float32))
        buf = io.BytesIO()
        Image.fromarray(vis).save(buf, format="PNG")
        return buf.getvalue()

    def latest_clipseg_png(self) -> Optional[bytes]:
        """CLIPSeg obstacle mask in image space, blue→low, red→high probability."""
        mask = self.state.last_clipseg_mask
        if mask is None or self._clipseg is None:
            return None
        vis = self._clipseg.heatmap(np.asarray(mask, dtype=np.float32))
        buf = io.BytesIO()
        Image.fromarray(vis).save(buf, format="PNG")
        return buf.getvalue()

    def latest_plan_png(self) -> Optional[bytes]:
        vis = self.state.last_plan_visualization
        if vis is None:
            return None
        buf = io.BytesIO()
        Image.fromarray(np.asarray(vis, dtype=np.uint8)).save(buf, format="PNG")
        return buf.getvalue()

    def latest_plan_json(self) -> Optional[dict]:
        p = self.state.last_path_xy_m
        if p is None:
            return None
        arr = np.asarray(p, dtype=np.float32)
        return {
            "path_xy_m": arr.tolist(),
            "num_points": int(arr.shape[0]),
            "goal_local": {"x_m": self.state.goal_x_m, "y_m": self.state.goal_y_m},
            "distance_to_next_m": (
                self.state.distance_to_next_m
                if self.state.distance_to_next_m != float("inf")
                else None
            ),
            "plan_ts": self.state.last_plan_ts,
            "meta": self.state.last_plan_meta,
        }

    def _checkpoints_status_list(self) -> list[dict]:
        """Per-checkpoint payload for the dashboard.

        For each CP in state.checkpoints returns:
          seq, lat, lon, status ("achieved"|"current"|"pending"),
          distance_m (from bot's live GPS, None if bot GPS unknown),
          reached_ts (from state.history, if any).
        Sorted by sequence.
        """
        snap = self.state.last_telemetry
        bot_lat = snap.latitude
        bot_lon = snap.longitude
        history_by_seq = {int(h.get("seq", -1)): h for h in (self.state.history or [])}

        out: list[dict] = []
        for cp in self.state.checkpoints or []:
            try:
                seq = int(cp.get("sequence", 0))
                cp_lat = float(cp.get("latitude"))
                cp_lon = float(cp.get("longitude"))
            except (TypeError, ValueError):
                continue

            if seq < self.state.current_seq:
                status = "achieved"
            elif seq == self.state.current_seq:
                status = "current"
            else:
                status = "pending"

            distance_m: Optional[float] = None
            if bot_lat is not None and bot_lon is not None:
                try:
                    distance_m = float(_haversine_m(
                        float(bot_lat), float(bot_lon), cp_lat, cp_lon
                    ))
                except Exception:
                    distance_m = None

            hist = history_by_seq.get(seq)
            out.append({
                "seq": seq,
                "lat": cp_lat,
                "lon": cp_lon,
                "status": status,
                "distance_m": distance_m,
                "reached_ts": float(hist["ts"]) if hist and "ts" in hist else None,
                "reached_at_dist_m": float(hist["distance_m"]) if hist and "distance_m" in hist else None,
            })
        out.sort(key=lambda x: x["seq"])
        return out

    def status_dict(self) -> dict:
        snap = self.state.last_telemetry
        rover_age_ms = None
        rtm_link = "unknown"
        if snap.rover_ts and snap.rover_ts > 0.0:
            age_s = time.time() - float(snap.rover_ts)
            rover_age_ms = int(age_s * 1000)
            if age_s < 3.0:
                rtm_link = "alive"
            elif age_s < 10.0:
                rtm_link = "slow"
            else:
                rtm_link = "DEAD"
        return {
            "running": self.state.running,
            "mode": self.state.mode,
            "iterations": self.state.iterations,
            "error_streak": self.state.error_streak,
            "last_error": self.state.last_error,
            "log_dir": self.state.log_dir,
            "last_gps": (
                {"lat": snap.latitude, "lon": snap.longitude, "ts": snap.ts}
                if snap.latitude is not None
                else None
            ),
            "last_yaw_deg": snap.yaw_deg,
            "last_speed_ms": snap.speed_ms,
            "battery": snap.battery,
            "rover_ts": snap.rover_ts,
            "rover_age_ms": rover_age_ms,
            "rtm_link": rtm_link,
            "last_bev_ts": self.state.last_bev_ts,
            "last_bev_age_ms": (
                int((time.time() - self.state.last_bev_ts) * 1000)
                if self.state.last_bev_ts > 0
                else None
            ),
            "last_samtp_ts": self.state.last_samtp_ts,
            "last_samtp_age_ms": (
                int((time.time() - self.state.last_samtp_ts) * 1000)
                if self.state.last_samtp_ts > 0
                else None
            ),
            "last_clipseg_ts": self.state.last_clipseg_ts,
            "last_clipseg_age_ms": (
                int((time.time() - self.state.last_clipseg_ts) * 1000)
                if self.state.last_clipseg_ts > 0
                else None
            ),
            "clipseg_active": self._clipseg is not None,
            "confined": {
                "enabled": bool(getattr(self.config, "confined_speed_enabled", False)),
                "active": bool(self.state.confined_active),
                "obstacle_ratio": float(self.state.confined_obstacle_ratio),
                "speed_cap": float(getattr(self.config, "confined_speed_max", 0.0)),
            },
            "last_plan_ts": self.state.last_plan_ts,
            "last_plan_age_ms": (
                int((time.time() - self.state.last_plan_ts) * 1000)
                if self.state.last_plan_ts > 0
                else None
            ),
            "current_seq": self.state.current_seq,
            "total_checkpoints": len(self.state.checkpoints),
            "distance_to_next_m": (
                self.state.distance_to_next_m
                if self.state.distance_to_next_m != float("inf")
                else None
            ),
            "checkpoints_status": self._checkpoints_status_list(),
            "bot_gps": (
                {"lat": float(snap.latitude), "lon": float(snap.longitude)}
                if snap.latitude is not None and snap.longitude is not None
                else None
            ),
            "goal_local": {"x_m": self.state.goal_x_m, "y_m": self.state.goal_y_m},
            "last_command": {
                "linear": self.state.last_linear,
                "angular": self.state.last_angular,
                "reason": self.state.last_control_reason,
            },
            "dry_run": self.config.dry_run,
            "device": self._samtp.device if self._samtp is not None else None,
        }


def build_runtime(
    config: UrbanRuntimeConfig,
    get_frame_base64: FrameFetcher,
    get_data: DataFetcher,
    post_control: Optional[ControlPoster] = None,
    get_checkpoints_list: Optional[CheckpointsFetcher] = None,
    checkpoint_reached: Optional[CheckpointReporter] = None,
    planner_yaml_path: Optional[str] = None,
    camera_K_path: Optional[str] = None,
    T_base_camera_path: Optional[str] = None,
    preloaded_samtp: Any = None,
    preloaded_clipseg: Any = None,
) -> UrbanRuntime:
    planner_yaml = planner_yaml_path or str(CONFIGS_ROOT / "mini_urban.yaml")
    K = camera_K_path or str(CALIBRATION_ROOT / "mini_camera_K.npy")
    T_bc = T_base_camera_path or str(CALIBRATION_ROOT / "mini_T_base_camera.npy")

    state = UrbanRuntimeState()
    return UrbanRuntime(
        config=config,
        state=state,
        planner_yaml_path=planner_yaml,
        camera_K_path=K,
        T_base_camera_path=T_bc,
        get_frame_base64=get_frame_base64,
        get_data=get_data,
        post_control=post_control,
        get_checkpoints_list=get_checkpoints_list,
        checkpoint_reached=checkpoint_reached,
        preloaded_samtp=preloaded_samtp,
        preloaded_clipseg=preloaded_clipseg,
    )
