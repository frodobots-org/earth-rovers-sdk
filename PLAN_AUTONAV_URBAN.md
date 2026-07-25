# Autonav Urban — GENIE-SAMTP Integration Plan

Living plan for integrating the GENIE-SAMTP navigation stack (ICRA 2025 ERC winner) into `earth-rovers-sdk` for the ERC 2026 Urban track.

Owner: Divyesh Bhalala. Target: policy submission #2 window (~Aug 15, 2026); competition Sep 27 – Oct 1, 2026 (Pittsburgh).

---

## 1. Executive Summary

Build a new autonomous urban navigation runtime in this repo — `autonav_urban.py` — that consumes the existing SDK endpoints (`/start-mission`, `/checkpoints-list`, `/data`, `/v2/front`, `/control`, `/checkpoint-reached`) and drives the rover from GPS checkpoint to GPS checkpoint using **vendored GENIE-SAMTP** (SAM-TP perception + BEV path planner) as the core motion planner.

Existing VLM-as-pilot autonav on `feature/openClaw` is preserved untouched; this is a parallel implementation targeted at outdoor urban driving.

**Success = rover completes a real 3-checkpoint outdoor mission without human intervention, at least once, by end of Phase 8.**

---

## 2. Context

### 2.1 Competition
- ERC 2026 at IROS 2026, Pittsburgh, Sep 30 – Oct 1
- Four tracks, **one policy across all four**
- Urban track: GPS waypoints, ≤ 15 m tolerance, 10+ cities. Rounds 01, 02, 05, 06.
- Hardware: Earth Rover Mini+ — 4 km/h max, RTK GPS ~50 cm, front 1024×576, rear 540×360, 4G LTE
- ~20 Hz action stream with ~500 ms latency
- No model-size cap; policy runs off-board on team compute
- Timeline: Policy submission #1 (past mid Jul), Submission #2 (~mid Aug), dry runs early Sep, competition Sep 27 – Oct 1

### 2.2 Reference implementation
- GeNIE won ICRA 2025 ERC (NUS, 57% of top-human score, +20 pts vs. 2024 Seoul NU baseline)
- Public repo: `/Users/dev/Documents/GENIE-SAMTP-master/` — contains perception (SAM-TP) + BEV planner + config only; no controller, no VLM recovery
- Trained on FrodoBots-2K, i.e. this exact rover hardware and sidewalk domain
- Paper: https://arxiv.org/abs/2506.17960 · Project page: https://clear-nus.github.io/genie/

### 2.3 Existing autonav in this repo
- `main` branch: no autonav code
- `feature/openClaw`, `Mini+Agent-Kit`: VLM-as-pilot loop (`autonav_service.py`, 652 LOC) that ticks at 1.5 s intervals — designed for indoor tabletop mazes, not suitable for ERC as-is due to rate mismatch and paradigm mismatch
- Reusable pieces from that codebase: LLM provider abstraction (Gemini + OpenAI), guardrail scaffolding, `autonav_logs/` scheme, battery/error-streak safety, `_perform_turn()` closed-loop turn primitive

---

## 3. Goals and Non-Goals

### Goals
1. Ship a working `/autonav-urban/*` endpoint set that drives the rover autonomously through a GPS-checkpoint mission
2. Preserve GENIE-SAMTP code unmodified inside `third_party/` so upstream fixes can be pulled
3. Match or exceed 57% of top-human score on urban missions
4. Runtime dashboard shows live BEV + planned path
5. Structured per-tick logs enable offline replay and debugging

### Non-Goals (this plan)
- Indoor image-goal (Track 2) — deferred, needs different goal-conditioning
- Off-road image-goal (Track 3) — deferred, needs SAM-TP retraining
- Marathon (Track 4) — endurance layer, mostly reuses Urban stack; separate work
- Replacing or deprecating the existing maze-VLM autonav
- Full SLAM / metric map fusion beyond GENIE's temporal BEV fusion
- Training / fine-tuning SAM-TP; we use the released `checkpoint_2.pt` as-is

---

## 4. Success Criteria

| Phase gate | Verifiable criterion |
|---|---|
| Phase 1 done | `bash plan_from_obs.sh` runs green from inside `venv39` on the vendored copy |
| Phase 2 done | Known checkerboard corner projects to expected world XY within ±10 cm |
| Phase 3 done | `pytest tests/test_urban_geo.py` green — all coordinate transforms round-trip |
| Phase 5 done | `http://localhost:8000` dashboard shows live BEV updating at ≥ 3 Hz |
| Phase 6 done | Dashboard shows live planned path on BEV, updating on GPS goal change |
| Phase 7 done | Dry-run 60 s log shows sensible `(linear, angular)` commands, no exceptions |
| Phase 8 done | Live rover drives from A to B (~10 m single checkpoint), `POST /checkpoint-reached` succeeds |
| Phase 9 done | Live rover completes a real 3-checkpoint mission, no human touch |
| Phase 10 done | Rover recovers from being nudged onto grass without human intervention |

---

## 5. Architecture Overview

### 5.1 The runtime — 5 concurrent asyncio loops

```
┌──────────────────────────────────────────────────────────────┐
│  autonav_urban.py                                             │
├──────────────────────────────────────────────────────────────┤
│                                                                │
│  TELEMETRY LOOP  (5 Hz)                                        │
│    GET /data → state.last_gps, .last_yaw_deg, .last_speed_ms   │
│                                                                │
│  MISSION LOOP  (1 Hz)                                          │
│    Reads checkpoints, picks next target, computes local goal   │
│    Calls POST /checkpoint-reached when distance < 15 m         │
│                                                                │
│  PERCEPTION LOOP  (5-10 Hz)                                    │
│    front frame → SAM-TP → BEV projection → state.last_bev      │
│                                                                │
│  PLANNING LOOP  (event-driven, ~1 Hz on 1 m travel)            │
│    plan_on_bev(bev, mask, goal_x_m, goal_y_m) →                │
│    state.last_path_xy_m                                        │
│                                                                │
│  CONTROL LOOP  (10 Hz)                                         │
│    pure-pursuit on last_path_xy_m →                            │
│    POST /control {linear, angular, lamp}                       │
│    + collision monitor overrides with hard stop                │
│                                                                │
│  RECOVERY LOOP  (async, triggered on stuck / off-road)         │
│    VLM classification, 360° look-around, resume normal flow    │
│                                                                │
└──────────────────────────────────────────────────────────────┘
```

### 5.2 Data flow — one iteration

```
FrodoBots backend
     ↕
POST /start-mission ─ once
GET  /checkpoints-list ─ once
     │
     ▼
[MISSION LOOP] target = checkpoints[current_seq-1]
     │
GET /data → (lat, lon, yaw, speed) ─── every 200 ms ─── [TELEMETRY LOOP]
     │
     ▼
gps_to_local_goal(current_lat, current_lon, current_yaw,
                  target.latitude, target.longitude)
     │  → (goal_x_m, goal_y_m, distance_m)
     ▼
GET /v2/front → RGB ─────► SAM-TP ─► logits ─► BEV [PERCEPTION LOOP]
     │
     ▼
plan_on_bev(bev, mask, goal_x_m, goal_y_m) ─► final_path_xy_m [PLANNING LOOP]
     │
     ▼
pure_pursuit(final_path_xy_m) ─► POST /control {linear, angular} [CONTROL LOOP]
     │
     ▼
When distance < 15 m:
  POST /checkpoint-reached → next_checkpoint_sequence [MISSION LOOP]
  If last CP reached → state.mode = "done", POST /control {0, 0, 0}
```

### 5.3 New HTTP surface

| Endpoint | Purpose |
|---|---|
| `POST /autonav-urban/start` | Begin autonomous mission; body may set `dry_run`, `max_linear`, `provider`, `model`, `config_path` |
| `POST /autonav-urban/stop` | Cancel loops, POST `/control {0,0,0}`, do NOT `POST /end-mission` (would lose progress) |
| `GET /autonav-urban/status` | Runtime snapshot: mode, current_seq, distance_to_next, ages, error_streak, log_dir |
| `GET /autonav-urban/bev` | Latest BEV visualization PNG (uses `traversability_vis()` from GENIE) |
| `GET /autonav-urban/plan` | Latest plan JSON + overlay PNG |

---

## 6. GENIE code inventory

### 6.1 Vendored, actively called (`third_party/genie_path_planner/`)

| File | LOC | Symbols we import |
|---|---|---|
| `pipeline.py` | 570 | `load_samtp_model`, `planner_config_from_dict`, `run_offline_path_planner`, `build_bev_observations` |
| `planner.py` | 424 | `plan_on_bev`, `PlannerConfig`, `PlannedPath`, `traversability_to_cost`, `cost_to_vis` |
| `projection.py` | 469 | `project_score_to_bev`, `logits_to_traversability`, `traversability_vis`, `BEVObservation`, `fuse_bev_observations` |
| `path_sampling.py` | 191 | (called via planner) |
| `path_selection.py` | 210 | (called via planner) |
| `costs.py` | 93 | (called via planner) |
| `geometry.py` | 123 | `pose_xy_yaw_to_matrix`, `goal_xy_to_bev_pixel`, `bev_pixel_to_xy`, `camera_planar_axes` |
| `io_utils.py` | 125 | `load_matrix`, `load_rgb_image`, `resolve_path`, `save_json` |
| `run_image_path_planner.py` | 82 | not called; CLI-only |
| `__init__.py` | 23 | re-exports |

### 6.2 Vendored, called only indirectly (`third_party/sam2/`)

Only three files matter to our call graph; the rest ride along so imports resolve:
- `sam2/sam_tp.py` (74 LOC) — the entry point (`SAM_TP` class, 2 methods)
- `sam2/build_sam.py` — model builder (called by `SAM_TP.__init__`)
- `sam2/sam2_image_predictor.py` — inference (called by `SAM_TP.run_sam2_inference`)
- `sam2/modeling/*`, `sam2/utils/*`, `sam2/configs/sam2.1_inference_tiny/sam2.1_custom2.yaml` — model machinery
- `sam2/csrc/connected_components.cu` — CUDA kernel (optional; not needed on MPS)

Never called at runtime but vendored anyway to keep the package importable:
- `sam2/automatic_mask_generator.py`, `sam2/sam2_video_predictor*.py`, `sam2/benchmark.py`

### 6.3 Runtime imports — 5 lines total

```python
from third_party.sam2.sam_tp import SAM_TP
from third_party.genie_path_planner.pipeline import load_samtp_model, planner_config_from_dict
from third_party.genie_path_planner.planner import plan_on_bev
from third_party.genie_path_planner.projection import project_score_to_bev, logits_to_traversability, traversability_vis
from third_party.genie_path_planner.geometry import pose_xy_yaw_to_matrix
```

### 6.4 NOT vendored / skipped

| Item | Reason |
|---|---|
| `configs/stretch_path_planner.yaml` | Replaced by `configs/mini_urban.yaml` |
| `stretch_example/` | Test data only |
| `run_path_planner.py`, `visualize_heatmap.py`, `plan_from_obs.sh` | CLI wrappers; we call `run_offline_path_planner()` directly |
| `setup.py`, `pyproject.toml`, `environment.yml`, `backend.Dockerfile` | We use our own `requirements.txt` and venv |

### 6.5 Modifications to vendored code

**None planned.** All tuning happens via config (`configs/mini_urban.yaml`) or wrapper code. If `sam2/sam_tp.py` line 6 (matplotlib import) is annoying we may patch it out to slim deps — this is the only exception.

---

## 7. Mission flow using existing SDK endpoints

### 7.1 Startup sequence

1. `POST /autonav-urban/start`
2. → `POST /start-mission` (registers rover)
3. → `GET /checkpoints-list` (fetches full list + `latest_scanned_checkpoint`)
4. → Load SAM-TP checkpoint (~5 s cold)
5. → Load `calibration/mini_camera_K.npy`, `calibration/mini_T_base_camera.npy`
6. → Spawn 5 asyncio loops
7. → `state.current_seq = latest_scanned_checkpoint + 1`

### 7.2 Steady-state per 1 s

- ~5× `GET /data`
- ~5–10× frame captures (via `browser_service`, not HTTP)
- ~5–10× SAM-TP inferences (GPU)
- ~1 mission-loop tick (recompute goal, arrival check)
- ~1 plan (event-driven every 1 m of travel)
- ~10× `POST /control`
- 0× mission-API calls (until arrival)

### 7.3 Checkpoint arrival

- Local: mission loop sees `distance_to_next < 15 m`
- → `POST /checkpoint-reached` with empty body (SDK reads current GPS internally)
- On 200: response includes `next_checkpoint_sequence`; `state.current_seq = next_seq`
- On 400 (`Bot is not within XXm`): trust backend's `proximate_distance_to_checkpoint`, keep driving
- If `next_seq` is null / 0: `state.mode = "done"`, controller sends `{0,0,0}`, loops exit

### 7.4 Failure modes and responses

| Failure | Detection | Response |
|---|---|---|
| GPS jump | Δlat > 5e-4 between adjacent reads | Use gyro-integrated yaw for a few sec, hold mission logic |
| Compass drift vs. GPS-track disagreement | speed > 0.5 m/s and |compass − GPS-track| > 20° | Trust GPS-track heading |
| RTK signal lost | `/data.signal_level` drop or `gps_signal` < threshold | Slow to 0.3 m/s, don't advance until re-acquired |
| Planner returns empty | `planned.metadata.status == "no_valid_paths_after_filtering"` | Stop, trigger VLM recovery |
| Rover not moving despite forward cmd | `speed < 0.05 m/s` for 3 s | Stop, VLM look-around |
| Battery < 15% | `/data.battery` | Stop, safe park |
| 4G latency > 2 s | `POST /control` response time | Halve MAX_LINEAR, continue |
| `POST /checkpoint-reached` rejected | HTTP 400 | Trust backend distance, keep driving |
| Overshoot checkpoint | goal_y_m becomes negative | GENIE clips + selects "back-toward-goal" arcs; Mini+ turn-in-place |

---

## 8. Phased implementation

Ten phases, roughly one per session. Every phase has a runnable, verifiable outcome.

### Phase 1 — Vendor GENIE + environment (Days 1–3)

**Tasks**
1. Create branch `feature/autonav-urban` off `main`
2. Copy `/Users/dev/Documents/GENIE-SAMTP-master/genie_path_planner/` → `third_party/genie_path_planner/`
3. Copy `/Users/dev/Documents/GENIE-SAMTP-master/sam2/` → `third_party/sam2/`
4. Write `scripts/download_samtp_ckpt.sh` — fetches `checkpoint_2.pt` from Google Drive into `third_party/sam2_ckpt/`
5. Update `.gitignore` to exclude `third_party/sam2_ckpt/*.pt` and other large artifacts
6. Extend `requirements.txt`:
   - `torch>=2.2,<2.4`
   - `torchvision>=0.17,<0.19`
   - `hydra-core>=1.3`
   - `iopath>=0.1.10`
   - `scikit-learn>=1.3`
   - `matplotlib>=3.7`
7. `pip install -r requirements.txt` in `venv39`
8. Smoke test: `python -c "from third_party.genie_path_planner.pipeline import run_offline_path_planner"` succeeds
9. Copy `stretch_example/` under `third_party/genie_path_planner/stretch_example/` and run its bundled `plan_from_obs.sh` equivalent → confirm identical output PNG

**Deliverable**: GENIE runs on the Stretch sample from inside our venv. Output written to `autonav_logs/genie_smoke/`.

**Acceptance**: `.png` from smoke test visually matches the reference from GENIE's `stretch_example/stretch_obs/planner_output/`.

**Risk**: PyTorch ↔ SAM-TP checkpoint compatibility. If mismatch, pin `torch==2.3.1` explicitly. Document the pinned version.

---

### Phase 2 — Mini+ camera calibration (Days 4–7)

**Tasks**
1. Open FrodoBots-2K helpercode.ipynb (linked from README), extract stock front-camera `K` for 1024×576 and mount pose `T_base_camera`
2. Save `calibration/mini_camera_K.npy` (3×3 float64) and `calibration/mini_T_base_camera.npy` (4×4 float64)
3. Independent verification: print an 8×6 checkerboard (25 mm squares), capture ~20 varied frames via `/v2/front`, run `cv2.calibrateCamera()`
4. Compare Track-A vs. Track-B; if `fx`/`fy`/`cx`/`cy` diverge > 5%, use OpenCV output
5. Physically measure camera height above ground and pitch angle relative to base; compose `T_base_camera` matrix using optical camera convention (+x image right, +y image down, +z forward)
6. Unit test: a known 3D point (checkerboard corner at known world position) projected through K and mount transform lands within ±10 cm of expected BEV cell

**Deliverable**: `calibration/mini_camera_K.npy`, `calibration/mini_T_base_camera.npy`, `tests/test_calibration.py`.

**Acceptance**: `pytest tests/test_calibration.py` green.

**Risk**: Mount pose is easy to get wrong (pitch angle, camera height). Add a debug endpoint that overlays projected ground-plane grid on the front frame for visual check.

---

### Phase 3 — GPS → local goal module (Days 8–9)

**Tasks**
1. Create `autonav_urban_geo.py`:
   - `gps_bearing_and_distance(lat1, lon1, lat2, lon2) -> (bearing_rad, distance_m)` — equirectangular haversine
   - `compass_to_math_yaw(compass_deg) -> float` — convert `/data.orientation` (0–360 from north) to math yaw
   - `gps_to_local_goal(current_lat, current_lon, current_yaw_deg, target_lat, target_lon, virtual_range_m=10.0) -> (goal_x_m, goal_y_m, distance_m)`
   - `fuse_yaw(compass_deg, gyro_yaw_rad, gps_track_deg, speed_ms) -> float` — weighted fusion
2. Unit tests (`tests/test_urban_geo.py`):
   - Northward 100 m → bearing == 0
   - Eastward 100 m → bearing == π/2
   - Round-trip: `gps_to_local_goal` → back-project → matches within 1 m
   - Yaw fusion: static rover trusts compass, moving rover trusts GPS-track

**Deliverable**: module + passing pytest.

**Acceptance**: `pytest tests/test_urban_geo.py` green.

---

### Phase 4 — Config + runtime state scaffolding (Days 10–11)

**Tasks**
1. Write `configs/mini_urban.yaml`:
   - BEV: `resolution_m_per_px: 0.05`, `forward_range_m: 6.0`, `side_range_m: 2.5`, `max_ray_distance_m: 10.0`
   - Depth: `enabled: false`
   - Planner: `footprint_px: 6`, `unknown_cost: 0.30`, `threshold_cost: 0.55`, `number_of_points_to_filter: 40`, `alpha: 1.2`, `use_clustering: true`, `max_clusters: 4`, `cluster_angle_threshold_deg: 30`
   - `planning.mode: rgb`, `observation_fusion.enabled: false`
2. Write `autonav_urban_config.py`:
   - `UrbanRuntimeConfig` dataclass (gains, rates, thresholds)
3. Write `UrbanRuntimeState` dataclass with `asyncio.Lock` per bucket:
   - Telemetry bucket: `last_gps`, `last_yaw_deg`, `last_speed_ms`, `last_battery`
   - Perception bucket: `last_bev`, `last_observed_mask`, `last_bev_ts`, `last_camera_pose`
   - Planning bucket: `goal_x_m`, `goal_y_m`, `distance_to_next_m`, `last_path_xy_m`, `last_plan_ts`, `last_plan_meta`
   - Mission bucket: `checkpoints`, `current_seq`, `history`
   - Control bucket: `last_command`, `last_control_ts`
   - Meta: `running`, `mode`, `iterations`, `error_streak`, `last_error`, `log_dir`

**Deliverable**: config loads via `planner_config_from_dict`, state constructs, no runtime yet.

**Acceptance**: `python -c "from autonav_urban_config import UrbanRuntimeConfig; UrbanRuntimeConfig()"` succeeds.

---

### Phase 5 — Perception loop + BEV endpoint (Days 12–17)

Most visual milestone. Proves whole vision pipeline is live.

**Tasks**
1. In `autonav_urban.py`:
   - `class SAMTPService` — wraps `SAM_TP`; lazy load; expose `infer(rgb) -> logits`
   - `perception_loop(state, config, samtp)` async function:
     - Grab frame via `browser_service.get_frame_base64("front")`
     - Decode base64 → np.ndarray HxWx3 uint8
     - `logits = samtp.infer(rgb)`
     - `trav = logits_to_traversability(logits, "sigmoid")`
     - Read latest yaw + build `T_world_camera = pose_xy_yaw_to_matrix([0, 0, math_yaw]) @ T_base_camera`
     - `bev, observed, _ = project_score_to_bev(trav, K, T_world_camera, ...)`
     - Update state.perception bucket with lock
     - Sleep to target `perception_target_hz`
2. In `main.py`:
   - `GET /autonav-urban/bev` — returns `traversability_vis(state.last_bev)` as PNG stream
   - Add HTML panel to `index.html` that polls `/autonav-urban/bev` every 500 ms
3. Add `POST /autonav-urban/start` skeleton — only spawns perception loop for now

**Deliverable**: open http://localhost:8000, see live BEV panel beside front-camera feed.

**Acceptance**: walk in front of the rover, verify obstacles appear/disappear in BEV in real time.

**Risk**: MPS device selection. `SAM_TP` needs to accept a device param; if not, we set `PYTORCH_ENABLE_MPS_FALLBACK=1` and check throughput. If < 2 Hz on MPS, defer live testing to a GPU host.

---

### Phase 6 — Planning loop + plan endpoint (Days 18–19)

**Tasks**
1. In `autonav_urban.py`:
   - `planning_loop(state, config, planner_config)` async function:
     - Wait for fresh BEV (age < 500 ms)
     - Compute `(goal_x_m, goal_y_m)` from state (initially hardcoded 10 m forward)
     - `planned = plan_on_bev(bev, observed_mask, goal_x_m, goal_y_m, resolution_m_per_px, planner_config)`
     - Update state.planning bucket
     - Event-driven wake: replan when distance since last plan ≥ 1 m OR planner_failed
2. In `main.py`:
   - `GET /autonav-urban/plan` — returns JSON `{path: [[x, y], ...], goal_local: [x, y], status: ...}` + overlay PNG (from `planned.visualization`)
   - Add plan overlay to `index.html` panel

**Deliverable**: dashboard shows planned path drawn on cost map, updating on GPS/goal changes.

**Acceptance**: place a box in front of rover → path avoids it; move box → path updates.

---

### Phase 7 — Controller loop + dry-run mode (Days 20–24)

**Tasks**
1. Write `autonav_urban_controller.py`:
   - `pick_lookahead(path_xy_m, lookahead_m) -> (x, y)`
   - `pure_pursuit(target_xy, cfg) -> (linear, angular)`
   - `front_strip_hazard(bev, observed_mask, forward_m, half_width_m, threshold) -> bool`
   - `turn_in_place_needed(heading_err_rad, thresh) -> bool`
2. In `autonav_urban.py`:
   - `control_loop(state, config)` @ 10 Hz:
     - If `state.mode != "driving"` → POST /control {0, 0, 0}
     - Else pull latest path; compute PP; POST /control
     - Collision monitor pre-empts with hard stop when triggered
     - **Dry-run mode**: log commands to `autonav_logs/urban_<run_id>/tick_NNNN.json` without POSTing to /control
3. Extend `POST /autonav-urban/start` with `dry_run` flag
4. Wire up `POST /autonav-urban/stop`

**Deliverable**: put rover on sidewalk, hit `POST /autonav-urban/start {dry_run: true}`, walk in front. Verify:
- Planner picks paths around you
- Controller decisions in logs look sensible
- No actual control commands sent

**Acceptance**: 60 s dry-run log has no exceptions and `(linear, angular)` distributions look plausible (linear mostly 0.3–0.7, angular clipped).

---

### Phase 8 — Live single-checkpoint drive (Days 25–31) — **Policy Submission #2 target**

**Tasks**
1. Flip `dry_run` off in a controlled setting
2. Add `mission_loop(state, config)` @ 1 Hz:
   - Fetch `/checkpoints-list` on start
   - Compute goal from next checkpoint vs. current GPS
   - When `distance_to_next < 15 m`, call internal `/checkpoint-reached`
   - Handle 400 (`proximate_distance_to_checkpoint`) — trust backend distance
   - Advance sequence; if no `next_checkpoint_sequence` → mode = "done"
3. Field-tune: `MAX_LINEAR`, `MAX_ANGULAR`, `K_ang`, `lookahead_m`, `planner_replan_distance_m`
4. Add battery floor (15%), error-streak cutoff (3), GPS-jump detection
5. Fine-grained logging: per-tick `.json` + front frame `.jpg` + BEV `.png` + plan `.npy`

**Deliverable**: rover autonomously drives from A → B ~10 m away, `POST /checkpoint-reached` returns success.

**Acceptance**: 5 consecutive successful single-checkpoint drives on an empty flat sidewalk. Video captured.

**Target milestone**: submit this as Policy Submission #2 by mid-Aug.

---

### Phase 9 — Multi-checkpoint missions + guardrails (Days 32–38)

**Tasks**
1. Full mission-loop implementation:
   - Handle `next_checkpoint_sequence` chaining
   - Handle mission completion (`next_seq` null/0)
   - Handle re-fetch on backend anomaly
2. GPS-jump detection: reject Δlat/Δlon > threshold; hold mission logic
3. Compass ↔ GPS-track disagreement: at speed, trust GPS-track
4. RTK signal-loss handling: slow to 0.3 m/s until re-acquired
5. Overshoot recovery: verify GENIE picks back-turn paths correctly
6. Test on the real 3-checkpoint mission from `/missions` list
7. Iterate on `configs/mini_urban.yaml`: tune `unknown_cost`, `footprint_px`, `threshold_cost` on real footage

**Deliverable**: rover completes a real 3-checkpoint mission end-to-end.

**Acceptance**: score > 0 on at least one difficulty-1 or -2 mission. Video captured.

---

### Phase 10 — Recovery + robustness (Days 39–49)

**Tasks**
1. Write `autonav_urban_recovery.py`:
   - `OffRoadClassifier` — VLM prompt: "Is the rover on a sidewalk/road (ON) or off (OFF)? JSON: {status, confidence}"
   - FIFO buffer of 5 recent classifications
   - Majority-vote 2-of-3 latest → trigger recovery
   - Reuse `_resolve_provider()` and provider dispatch from existing `autonav_service.py`
2. Look-around primitive:
   - Reuse `main.py::_perform_turn(degrees)` (closed-loop IMU turn) from openClaw branch — port it forward to `main` first
   - `perform_look_around(headings=[0, 45, 90, 135, 180, 225, 270, 315])`
   - For each: turn to heading, capture frame
   - Ask VLM which frame looks most like sidewalk
   - Turn to best heading, resume planner
3. Wire triggers:
   - Planner returns `no_valid_paths_after_filtering`
   - Off-road classifier votes OFF twice
   - `speed < 0.05 m/s` for 3 s while commanding forward
4. Endurance run: 2 km continuous in a park, measure planner failure rate
5. Latency stress: proxy `POST /control` through artificial delay, verify graceful slowdown
6. Optional: enable `observation_fusion` (last 4 frames) if narrow FOV bites

**Deliverable**: rover recovers from being nudged onto grass; endurance run completes.

**Acceptance**: video of successful recovery. 2 km run has < 2 human-intervention events.

---

## 9. Files Added / Touched

### New files (~1,910 LOC of our code + ~10 MB vendored)

```
PLAN_AUTONAV_URBAN.md                        (this doc)
autonav_urban.py                             ~600  — 5 loops
autonav_urban_config.py                      ~80   — dataclasses
autonav_urban_geo.py                         ~120  — GPS/bearing/local goal
autonav_urban_controller.py                  ~180  — pure pursuit + collision
autonav_urban_recovery.py                    ~200  — VLM off-road recovery
configs/mini_urban.yaml                      ~80
calibration/mini_camera_K.npy                (~1 KB)
calibration/mini_T_base_camera.npy           (~1 KB)
scripts/download_samtp_ckpt.sh               ~30
third_party/genie_path_planner/**            (vendored, ~2,310 LOC)
third_party/sam2/**                          (vendored, ~8,000+ LOC)
third_party/sam2_ckpt/checkpoint_2.pt        (~50 MB, gitignored)
tests/test_urban_geo.py                      ~150
tests/test_urban_controller.py               ~120
tests/test_urban_planner_smoke.py            ~80
tests/test_calibration.py                    ~60
```

### Modified files

```
main.py                                      +~500 delta (5 new endpoints + state singleton)
index.html                                   +~120 delta (BEV + plan panels)
requirements.txt                             +6 lines
.gitignore                                   +3 lines (third_party/sam2_ckpt, autonav_logs, __pycache__)
```

### Untouched (do NOT modify)

- `autonav_service.py` (on feature/openClaw and Mini+Agent-Kit) — VLM-as-pilot maze autonav
- `browser_service.py`, `rtm_client.py`, `tts_service.py` — SDK core services
- All existing `/control`, `/data`, `/screenshot`, `/mission*`, `/checkpoint*`, `/speak` endpoints

---

## 10. Testing strategy

### Unit tests (CI-friendly, no rover, no GPU)

- `test_urban_geo.py` — bearing math, yaw fusion, round-trips
- `test_urban_controller.py` — pure-pursuit outputs, collision monitor triggers
- `test_calibration.py` — checkerboard corner reprojection sanity
- `test_urban_planner_smoke.py` — feed cached FrodoBots-2K frame + fake pose, verify `plan_on_bev` returns valid path structure

### Integration tests (needs GPU + checkpoint, run manually)

- `test_urban_perception_e2e.py` — SAM-TP on saved sidewalk frames, spot-check BEV against ground truth
- `test_urban_dry_run.py` — full loop in dry-run for 60 s, no exceptions, sensible commands

### Live tests (needs rover, staged)

1. `live/01_static_bev.sh` — rover stationary, watch BEV in dashboard
2. `live/02_dry_run_60s.sh` — dry-run for 60 s on sidewalk
3. `live/03_single_checkpoint.sh` — 10 m single-CP mission
4. `live/04_multi_checkpoint.sh` — 3-CP mission
5. `live/05_endurance_2km.sh` — 2 km continuous
6. `live/06_recovery.sh` — nudge to grass, verify recovery

Each live test produces a video + tagged log directory.

---

## 11. Deployment model

### Development
- Local Mac (`darwin`, MPS backend) — 2–3 Hz SAM-TP; fine for Phases 1–7
- Or Linux + NVIDIA GPU box — 10 Hz SAM-TP; needed for Phase 8+ live drives

### Competition
- Dedicated GPU host (AWS `g5.xlarge` with A10G, or on-prem Linux + RTX 3060+)
- Runs full `main.py` there
- Rover connects to it over 4G LTE
- Redundancy: primary + hot spare, DNS failover
- New env var: `PERCEPTION_HOST` — if set, `SAMTPService` calls out over HTTP instead of running in-process

### Config profiles
- `configs/mini_urban.yaml` — production
- `configs/mini_urban_mac_mps.yaml` — reduced BEV grid + lower perception rate for MPS dev
- `configs/mini_urban_debug.yaml` — verbose logging, all overlays saved

---

## 12. Risks and Mitigations

| Risk | Prob | Impact | Mitigation |
|---|---|---|---|
| Compass yaw drift breaks BEV projection | H | H | Yaw fusion module (Phase 3); GPS-track fallback when moving; verify on ground-plane overlay in dashboard |
| SAM-TP off-distribution on Pittsburgh sidewalks in October (leaves, wet, low sun) | M | H | Test on Pittsburgh dev frames early; add fallback: if trav map < 20% traversable, drive slower and shorten horizon |
| RTK GPS loses lock in urban canyon | M | M | Detect via `signal_level`; fall back to gyro-integrated dead-reckoning for up to 20 m |
| 4G latency spikes > 1 s | H | M | Controller uses only small deltas; auto-slow on high latency |
| PyTorch ↔ SAM-TP checkpoint incompatibility | L | H | Pin torch version; document; freeze once working |
| Battery dies mid-mission | Certain | L | Mission loop respects battery_floor=15% |
| Camera occluded (mud, spatter) | M | M | Rear-camera fallback flag; human-callback surface in state |
| Mini+ mount pose differs from FrodoBots-2K stock | M | M | Independent calibration path (Track B in Phase 2); ground-plane overlay for visual check |
| Existing autonav on other branches accidentally merged | L | M | Explicitly branch off `main`; keep parallel; never merge our branch into openClaw |
| Missed policy submission #2 window | M | H | Phase 8 is the target; do not add scope. Cut recovery + endurance if slipping |

---

## 13. Timeline (10 weeks from 2026-07-22)

| Week starting | Phase | Milestone |
|---|---|---|
| Jul 22 | 1, 2 | GENIE vendored + smoke test + calibration files |
| Jul 29 | 3, 4 | Geo module + tests + config + state scaffolding |
| Aug 05 | 5 | Live BEV visible in dashboard |
| Aug 12 | 6, 7 | Planning + controller + dry-run |
| **Aug 19** | **8** | **Single-CP live drive — Policy Submission #2** |
| Aug 26 | 9 | Multi-CP missions; guardrails |
| Sep 02 | 10a | Recovery module |
| Sep 09 | 10b | Endurance + latency hardening |
| Sep 16 | freeze | Dry runs; policy locked |
| Sep 23 | travel | On-site setup |
| Sep 27 | comp | ERC 2026 |

---

## 14. Rollback Plan

- All work on `feature/autonav-urban` — never merged to `main` until Phase 8 passes
- If Phase 8 fails: `main` remains clean; can defer or fall back to existing VLM-as-pilot autonav on `feature/openClaw`
- Each phase's deliverable is independently verifiable; can pause at any phase and still have a shippable partial system
- `autonav_logs/` are append-only; a bad run doesn't overwrite anything

---

## 15. Open Questions (must be resolved before Phase 1)

1. **Compute host for Phase 8+**: Mac MPS (dev-only), remote Linux GPU, or AWS? (Cost trade-off + latency to rover.)
2. **`torch` version pin**: which SAM-TP-compatible PyTorch version? Verify against `checkpoint_2.pt` at end of Phase 1.
3. **Branch flow**: PR from `feature/autonav-urban` → `main`, or keep on a separate deployment branch?
4. **Existing autonav consolidation**: leave `autonav_service.py` on `feature/openClaw` as-is forever, or port forward and rename to `autonav_maze.py` under `main` alongside the new `autonav_urban.py`?
5. **Are we competing as a team, or building SDK infra for others?** (See `[[project-erc-2026]]` — this determines scope.)

---

## 16. References

- ERC 2026 pptx: `/Users/dev/Downloads/ERC_2026_Info_Session (1).pptx`
- GeNIE paper: https://arxiv.org/abs/2506.17960
- GENIE-SAMTP repo: `/Users/dev/Documents/GENIE-SAMTP-master/` (local) · https://github.com/jiaming-ai/GENIE-SAMTP
- SAM-TP checkpoint: https://drive.google.com/drive/folders/190yHH-TcfQVoByZeB1809sPIR62CsBD1
- FrodoBots-Mini-4K dataset: https://huggingface.co/datasets/BitRobot/FrodoBots-Mini-4K
- Earth Rovers SDK: this repo — https://github.com/frodobots-org/earth-rovers-sdk

Related memory files (auto-loaded next session):
- `project_erc_2026.md`
- `reference_genie_samtp.md`
- `reference_autonav_locations.md`

---

*Last updated: 2026-07-22*
