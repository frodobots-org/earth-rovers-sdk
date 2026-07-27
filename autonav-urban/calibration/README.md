# Mini+ Camera Calibration

Produces the two files `autonav_urban/bev.py` auto-loads:
- `mini_camera_K.npy` — camera **intrinsics** (the lens recipe)
- `mini_T_base_camera.npy` — camera **mount transform** (where/how it's mounted)

If these files are absent, the runtime silently uses **placeholders** (Stretch-era
defaults) and the BEV map is metrically wrong by ~10–30%. Producing them is the
Phase-2 blocker for accurate GeNIE-style navigation.

## WHEN does this happen? (one-time, NOT per mission)

Calibration is a **one-time setup**, done **before** you run autonomous missions —
like installing the model. You do NOT redo it each mission.

```
   ┌─ ONE TIME, up front ────────────────────────────────┐
   │  1. capture checkerboard photos   (camera live)      │
   │  2. compute intrinsics            (offline)          │
   │  3. build extrinsics              (offline)          │
   │  → two .npy files land in calibration/               │
   └──────────────────────────────────────────────────────┘
                        │  (files just sit there)
                        ▼
   EVERY mission afterwards:
     POST /start-mission → POST /autonav-urban/start
        └─ runtime calls load_camera_K() + load_T_base_camera() ONCE at startup,
           reads these files automatically. No calibration step per mission.
```

So the only place calibration touches the mission flow is **passively**: when
`/autonav-urban/start` builds the runtime, it loads these files once. Re-run
calibration only if you **remount/replace the camera**.

## Steps

### 1. Intrinsics — needs the camera live (~20 min)
Print a checkerboard (e.g. a 7×10-square board = **6×9 inner corners**). Bring the
rover camera online just to grab frames — you are not driving:
```bash
# terminal: server running, then start a session to get the video feed
curl -X POST http://localhost:8000/start-mission

cd autonav-urban/calibration
python capture_checkerboard.py --count 20     # ENTER to grab; move board around
python compute_intrinsics.py --captures captures --rows 6 --cols 9 --square-size 0.025
#   -> mini_camera_K.npy   (check reprojection error < 0.5 px)
```
Vary the board's angle, distance, and position (include the image corners). Then
you can `POST /end-mission`.

### 2. Extrinsics — offline, just measurements (~5 min)
Measure the camera's height above ground, its downward tilt, and how far ahead of
the rover's turning center it sits:
```bash
python build_extrinsics.py --height 0.15 --pitch 10 --forward 0.10
#   -> mini_T_base_camera.npy
```

### 3. Done
Both `.npy` files now sit in `calibration/`. The next `POST /autonav-urban/start`
picks them up automatically — no config edits, no code changes. Verify in the boot
log that it loaded real calibration (not the placeholder).

## Requirements
```bash
pip install opencv-python numpy
```
