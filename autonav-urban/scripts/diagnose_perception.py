"""Diagnose: does OUR SAM-TP wrapper produce the same output on a Mini+ frame
as the paper's reference script? If not, our pipeline has a bug.

Usage:
    # Run this while the server is running (rover connected)
    cd /Users/dev/Documents/earth-rovers-sdk
    source venv39/bin/activate
    python autonav-urban/scripts/diagnose_perception.py

Saves 4 files to autonav-urban/scripts/diagnose_out/:
    frame.png          - the raw front-camera frame
    ours_trav.png      - our SAM-TP output visualization
    ours_bev.png       - our BEV projection
    ours_plan.png      - our plan overlay
"""

import os
import sys
import io
import base64
import time
from pathlib import Path

import numpy as np
import requests
from PIL import Image

REPO_ROOT = Path("/Users/dev/Documents/earth-rovers-sdk")
sys.path.insert(0, str(REPO_ROOT / "autonav-urban"))
sys.path.insert(0, str(REPO_ROOT / "autonav-urban" / "third_party"))

OUT_DIR = REPO_ROOT / "autonav-urban" / "scripts" / "diagnose_out"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# 1) Grab a live frame from the running server
print("Fetching current front frame from live rover ...")
r = requests.get("http://localhost:8000/v2/front", timeout=10)
r.raise_for_status()
data = r.json()
frame_b64 = data.get("front_frame")
if not frame_b64:
    print("ERROR: /v2/front returned no frame. Is the server running + rover connected?")
    sys.exit(1)

if "," in frame_b64:
    frame_b64 = frame_b64.split(",", 1)[1]
raw = base64.b64decode(frame_b64)
img = Image.open(io.BytesIO(raw)).convert("RGB")
frame_np = np.asarray(img, dtype=np.uint8)
print(f"Frame shape: {frame_np.shape}, dtype: {frame_np.dtype}")

img.save(OUT_DIR / "frame.png")
print(f"Saved: {OUT_DIR / 'frame.png'}")

# 2) Run our SAM-TP wrapper on it
print("\nRunning OUR SAM-TP wrapper ...")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
import autonav_urban  # noqa: F401 — sets sys.path for vendored sam2
from autonav_urban import CONFIGS_ROOT, THIRD_PARTY_ROOT
from autonav_urban.samtp import SAMTPModel, jet_colormap
import yaml

with (CONFIGS_ROOT / "mini_urban.yaml").open("r") as f:
    cfg = yaml.safe_load(f) or {}
samtp_cfg = cfg.get("samtp", {}) or {}
cfg_path = str(THIRD_PARTY_ROOT / samtp_cfg.get(
    "config_path", "sam2/configs/sam2.1_inference_tiny/sam2.1_custom2.yaml",
))
ckpt_path = str(THIRD_PARTY_ROOT / samtp_cfg.get(
    "checkpoint_path", "sam2_ckpt/checkpoint_2.pt",
))

t0 = time.time()
model = SAMTPModel(cfg_path=cfg_path, ckpt_path=ckpt_path, device=None)
print(f"Loaded SAM-TP on {model.device} ({time.time()-t0:.1f}s)")

t1 = time.time()
out = model.run_sam2_inference(frame_np)
print(f"Inference: {time.time()-t1:.2f}s")

# 3) Post-process the same way runtime does
from genie_path_planner.projection import logits_to_traversability
trav = logits_to_traversability(out["logits"], samtp_cfg.get("score_transform", "sigmoid"))
print(f"Traversability shape: {trav.shape}, range: [{trav.min():.3f}, {trav.max():.3f}]")
print(f"Fraction drivable (>0.5): {(trav > 0.5).mean():.3f}")

# 4) Save visualization in both color schemes so you can compare with paper
jet = jet_colormap(trav)
Image.fromarray(jet).save(OUT_DIR / "ours_trav_jet.png")
print(f"Saved: {OUT_DIR / 'ours_trav_jet.png'} (JET colormap, matches paper)")

# Also save red/green like our dashboard shows
green = (trav * 255).astype(np.uint8)
red = ((1.0 - trav) * 255).astype(np.uint8)
zeros = np.zeros_like(green)
rg_vis = np.stack([red, green, zeros], axis=-1)
Image.fromarray(rg_vis).save(OUT_DIR / "ours_trav_redgreen.png")
print(f"Saved: {OUT_DIR / 'ours_trav_redgreen.png'} (red/green like dashboard)")

# 5) Blend with original image (like the paper's front-cam overlay)
alpha = 0.5
overlay = (frame_np.astype(np.float32) * (1 - alpha) + jet.astype(np.float32) * alpha).astype(np.uint8)
Image.fromarray(overlay).save(OUT_DIR / "ours_trav_overlay.png")
print(f"Saved: {OUT_DIR / 'ours_trav_overlay.png'} (SAM-TP overlaid on frame)")

print("\n--- DONE ---")
print(f"Open {OUT_DIR} and compare ours_trav_jet.png to the paper's demos.")
print("If theirs is a clean narrow path and ours is a big green blob,")
print("SAM-TP itself is over-predicting drivable on this scene.")
print("If theirs is similar to ours, the paper's demos are cherry-picked.")
