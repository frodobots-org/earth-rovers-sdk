# autonav-urban

GENIE-SAMTP-based autonomous urban navigation for the Earth Rover Mini+. This folder is a self-contained subproject inside `earth-rovers-sdk` — nothing outside `autonav-urban/` is modified except `main.py`, `requirements.txt`, and `.gitignore`.

Full plan lives in the repo-root [`PLAN_AUTONAV_URBAN.md`](../PLAN_AUTONAV_URBAN.md).

## Layout

```
autonav-urban/
├── autonav_urban/            Python package (importable)
│   ├── __init__.py           Sets up third_party import path
│   ├── config.py             UrbanRuntimeConfig + UrbanRuntimeState
│   ├── geo.py                GPS → local goal, bearing, yaw fusion
│   ├── controller.py         Pure pursuit + collision monitor
│   ├── recovery.py           VLM off-road detection + 360° look-around
│   └── runtime.py            5-loop asyncio orchestrator
├── configs/
│   └── mini_urban.yaml       Planner config tuned for Mini+
├── calibration/              Camera K + T_base_camera .npy (added in Phase 2)
├── scripts/
│   └── download_samtp_ckpt.sh
├── third_party/              Vendored GENIE code (unmodified)
│   ├── genie_path_planner/
│   └── sam2/
│   └── sam2_ckpt/            checkpoint_2.pt (gitignored)
├── tests/                    pytest suite
│   └── test_geo.py
└── autonav_logs/             Runtime per-tick logs (gitignored)
```

## Quick start (Phase 1 setup)

```bash
# From repo root (earth-rovers-sdk/), with venv39 active:
pip install -r requirements.txt

# Download the SAM-TP checkpoint (~50 MB):
bash autonav-urban/scripts/download_samtp_ckpt.sh

# Verify imports:
cd autonav-urban
python -c "import autonav_urban; from genie_path_planner.planner import plan_on_bev; print('ok')"

# Run tests:
python -m pytest tests/ -v
```

## Design summary

- **Perception**: SAM-TP (SAM2-tiny with a learnable traversability token) runs on the front camera at 6–10 Hz. Local machine uses PyTorch MPS backend (~3 Hz); production expects a Linux + NVIDIA host at 10 Hz.
- **BEV projection**: 5 cm/px, 6 m forward × 5 m wide window, RGB-only (Mini+ has no depth sensor). Camera pose is composed each tick from `IMU yaw × T_base_camera`.
- **Planner**: GENIE's polynomial path bank + cluster-and-goal-align selection. 30 cm robot footprint. Runs at ~1 Hz, re-plans every 1 m of travel.
- **Controller**: pure pursuit at 10 Hz. Turns in place when heading error > 45°. Collision monitor watches the front strip of the BEV and hard-stops on hazard.
- **Mission**: reads `/checkpoints-list` from the FrodoBots SDK, computes local goal via `gps_to_local_goal()`, calls `/checkpoint-reached` when within 15 m.
- **Recovery**: VLM classifies on-road/off-road with FIFO majority vote. Triggers 360° look-around at 8 headings; VLM picks best heading.

## What we vendor from GENIE-SAMTP

- `third_party/genie_path_planner/` — pure NumPy planner (2,310 LOC) — used unmodified
- `third_party/sam2/` — SAM2 model code (~8,000 LOC) — used unmodified
- `third_party/sam2_ckpt/checkpoint_2.pt` — 50 MB, downloaded via `scripts/download_samtp_ckpt.sh`

Runtime import surface into GENIE is 5 lines:

```python
from sam2.sam_tp import SAM_TP
from genie_path_planner.pipeline import load_samtp_model, planner_config_from_dict
from genie_path_planner.planner import plan_on_bev
from genie_path_planner.projection import project_score_to_bev, logits_to_traversability, traversability_vis
from genie_path_planner.geometry import pose_xy_yaw_to_matrix
```

## Phase status

| Phase | Status | Deliverable |
|---|---|---|
| 1 — Vendor GENIE + env | DONE | `import genie_path_planner.planner` works from venv39 |
| 2 — Mini+ calibration | pending | `calibration/mini_camera_K.npy` + `mini_T_base_camera.npy` |
| 3 — Geo module | initial | `pytest tests/test_geo.py` green (7/7) |
| 4 — Config + state | initial | `UrbanRuntimeConfig` + `UrbanRuntimeState` construct |
| 5 — Perception loop | pending | Live BEV visible in dashboard |
| 6 — Planning loop | pending | Live plan overlay on BEV |
| 7 — Controller + dry-run | pending | 60 s dry-run log |
| 8 — Live single-CP drive | pending | Rover A → B autonomous |
| 9 — Multi-CP missions | pending | Real mission completed |
| 10 — Recovery + endurance | pending | Recovers from off-road |
