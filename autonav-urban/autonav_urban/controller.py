"""Pure-pursuit controller + collision monitor.

Turns GENIE's planned path (list of [x_right_m, y_forward_m] points in the
rover body frame) into (linear, angular) commands compatible with POST /control.

Phase 7 fills these in. Phase 1: signatures + docstrings.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

from .config import UrbanRuntimeConfig


def pick_lookahead(
    path_xy_m: np.ndarray,
    lookahead_m: float,
) -> Optional[tuple[float, float]]:
    """Pick a lookahead target ~lookahead_m ahead of the robot along the path.

    Robustness:
    - Prefer the first path point with y_forward >= lookahead_m (standard PP)
    - If NO point reaches that distance (path is very short), do NOT return
      the endpoint — its heading may be extreme after a curving dead-end.
      Instead, use the initial direction of the path (tangent at start) and
      project it forward to the desired lookahead distance. This keeps the
      controller aiming at a plausible-forward target rather than lashing
      hard-left/right on a stubby path.
    - Returns None only if the path is empty.
    """
    if path_xy_m is None:
        return None
    p = np.asarray(path_xy_m, dtype=np.float32)
    if p.size == 0 or p.shape[0] < 2:
        return None

    # 1. Standard: first point past the lookahead distance.
    fwd = p[:, 1]
    for i in range(p.shape[0]):
        if float(fwd[i]) >= float(lookahead_m):
            return float(p[i, 0]), float(p[i, 1])

    # 2. Path is too short — use tangent from origin to a near-origin point,
    #    then extrapolate to lookahead_m along that direction.
    tangent_idx = min(p.shape[0] - 1, max(1, p.shape[0] // 4))   # ~25% along the path
    dx = float(p[tangent_idx, 0]) - float(p[0, 0])
    dy = float(p[tangent_idx, 1]) - float(p[0, 1])
    norm = (dx * dx + dy * dy) ** 0.5
    if norm < 1e-3:
        # Degenerate: robot origin — command straight forward.
        return 0.0, float(lookahead_m)
    scale = float(lookahead_m) / norm
    return dx * scale, dy * scale


def pure_pursuit(
    target_xy: tuple[float, float],
    cfg: UrbanRuntimeConfig,
) -> tuple[float, float]:
    """Compute (linear, angular) in /control units from a lookahead target.

    - Heading error = atan2(x_right, y_forward)
    - Angular = K_ang * heading_err, clipped to [-max_angular, +max_angular]
    - Linear = max_linear * cos(heading_err) when heading error is manageable
    - If |heading_err| > turn_in_place_thresh_deg, we still emit a SMALL
      forward speed rather than pure zero, because full turn-in-place at
      max_angular can stall against motor stiction and we lose all progress.
      A tiny forward component breaks stiction and keeps the rover from
      appearing frozen while it rotates.

    Handles goals behind the rover (y_forward < 0) by computing the actual
    atan2 across the full ±pi range — that way turning around is a smooth
    180° rotation, not stuck at ±90°.
    """
    x_right, y_forward = float(target_xy[0]), float(target_xy[1])
    heading_err = math.atan2(x_right, y_forward)

    # Sign convention (verified 2026-07-23 in align_in_place from live logs):
    #   angular > 0 = LEFT (yaw decreasing)
    #   angular < 0 = RIGHT (yaw increasing)
    # For a lookahead on the RIGHT (x_right > 0), heading_err > 0, and we
    # want to turn RIGHT toward it → angular must be NEGATIVE. Hence the
    # minus sign. Without this, pure_pursuit sent the exact opposite steering
    # command from what the planner intended — a left-curving plan produced
    # a right-turn command, and the rover skid-steered into a pivot.
    angular = -cfg.k_ang * heading_err
    # Cap at max_angular_while_pursuing (default 0.5) rather than the
    # unconstrained max_angular. This keeps skid-steer wheels from
    # reverse-pivoting: with linear ≥ 0.45 and |angular| ≤ 0.5, both wheels
    # always roll forward, so pursuit actually translates instead of
    # spinning in place. Turn-in-place is align_in_place's job — it uses
    # the full max_angular.
    pursuit_cap = float(getattr(cfg, "max_angular_while_pursuing", cfg.max_angular))
    pursuit_cap = min(pursuit_cap, cfg.max_angular)
    angular = max(-pursuit_cap, min(pursuit_cap, angular))

    # min_linear is the deadband floor — sending below this yields token
    # motion (~0.04 m/s) that can't finish a 15-min mission. See config.py.
    min_lin = float(getattr(cfg, "min_linear", 0.45))
    min_lin = min(min_lin, cfg.max_linear)      # never floor above max

    heading_err_deg = abs(math.degrees(heading_err))
    if heading_err_deg > cfg.turn_in_place_thresh_deg:
        # Steep turn: mostly rotate, but keep a forward crawl at min_linear
        # so the rover breaks stiction and doesn't stall in place.
        linear = min_lin
    else:
        # Normal PP: scale forward speed with heading alignment, then floor
        # at min_linear so gentle turns don't drop us into the deadband.
        linear = cfg.max_linear * max(0.0, math.cos(heading_err))
        if linear > 0.0 and linear < min_lin:
            linear = min_lin

    return float(linear), float(angular)


def align_in_place(
    goal_bearing_rad: float,
    cfg: UrbanRuntimeConfig,
) -> tuple[float, float]:
    """Closed-loop turn-in-place command that stops itself once aligned.

    Given the goal direction in the ROVER's current body frame (positive
    right, radians), return (linear, angular). Guarantees:

    - |bearing| <= align_deadband_deg → (0, 0). Rover is aligned; do not send
      residual angular that would fight into stiction / overshoot.
    - Otherwise → linear=0, angular = clip(k_align * bearing, ±max_angular).
      Because bearing is re-measured from FRESH yaw each control tick, the
      angular command shrinks as we approach the goal direction, so we
      naturally decelerate and stop — no more open-loop pirouettes.

    Reserved for "misaligned enough that no forward progress is useful"
    situations — pursue_pursuit still handles small heading errors while
    driving forward.

    Sign convention (verified empirically 2026-07-23 from runtime logs):
      angular > 0  →  rover CCW (yaw decreases, left turn)
      angular < 0  →  rover CW  (yaw increases, right turn)
    So for a goal on the RIGHT (goal_bearing_rad > 0 from atan2 convention),
    we need to command NEGATIVE angular to turn right toward it. Hence the
    minus sign below — without it the rover turns the LONG way around and
    oscillates instead of converging.
    """
    dead_rad = math.radians(float(cfg.align_deadband_deg))
    if abs(goal_bearing_rad) <= dead_rad:
        return 0.0, 0.0
    angular = -float(cfg.k_align) * float(goal_bearing_rad)
    angular = max(-cfg.max_angular, min(cfg.max_angular, angular))
    return 0.0, float(angular)


def front_strip_hazard(
    bev: np.ndarray,
    observed_mask: np.ndarray,
    bev_resolution_m: float,
    forward_m: float,
    half_width_m: float,
    trav_threshold: float,
    hazard_fraction: float = 0.40,
) -> bool:
    """Return True if the front strip of the BEV is *dominantly* non-traversable.

    Old behavior: any single sub-threshold cell in the front strip fired hazard
    (via np.any). On real SAM-TP outputs this triggers on virtually every
    frame due to noise on the boundary between "sidewalk" and "obstacle" —
    the rover halts constantly and never makes progress.

    New behavior: hazard fires only when >= `hazard_fraction` (default 40%) of
    the observed cells in the front strip are below the threshold. Single
    outlier pixels are tolerated. A trav_threshold of 0.0 disables the monitor
    entirely (never fires).
    """
    if bev is None or observed_mask is None:
        return False
    if float(trav_threshold) <= 0.0:
        return False   # explicit disable
    b = np.asarray(bev, dtype=np.float32)
    m = np.asarray(observed_mask, dtype=bool)
    if b.ndim != 2 or m.shape != b.shape:
        return False

    h, w = b.shape
    res = float(bev_resolution_m)
    if res <= 0:
        return False

    rows_ahead = min(h, int(math.ceil(float(forward_m) / res)))
    cols_half = min(w // 2, int(math.ceil(float(half_width_m) / res)))
    r_start = max(0, h - rows_ahead)
    c_center = w // 2
    c_start = max(0, c_center - cols_half)
    c_end = min(w, c_center + cols_half + 1)

    strip = b[r_start:h, c_start:c_end]
    strip_mask = m[r_start:h, c_start:c_end]
    observed = strip_mask & (strip >= 0.0)
    total = int(np.count_nonzero(observed))
    if total < 4:
        # Too little data to trust — assume safe. Otherwise a mostly-unobserved
        # strip with one noisy red cell would false-trigger.
        return False
    dangerous = observed & (strip < float(trav_threshold))
    bad = int(np.count_nonzero(dangerous))
    return (bad / max(1, total)) >= float(hazard_fraction)
