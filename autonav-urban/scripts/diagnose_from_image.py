"""Run our SAM-TP wrapper on a STATIC image (no live rover needed).

Usage:
    cd /Users/dev/Documents/earth-rovers-sdk
    source venv39/bin/activate
    python autonav-urban/scripts/diagnose_from_image.py <path-to-image.png>

Saves outputs to autonav-urban/scripts/diagnose_out/<image_basename>_*.png
"""

import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

if len(sys.argv) < 2:
    print("Usage: python diagnose_from_image.py <path-to-image> [<path-to-checkpoint>]")
    sys.exit(1)

img_path = Path(sys.argv[1])
override_ckpt = Path(sys.argv[2]) if len(sys.argv) > 2 else None
if not img_path.exists():
    print(f"ERROR: {img_path} not found")
    sys.exit(1)

REPO_ROOT = Path("/Users/dev/Documents/earth-rovers-sdk")
sys.path.insert(0, str(REPO_ROOT / "autonav-urban"))
sys.path.insert(0, str(REPO_ROOT / "autonav-urban" / "third_party"))

OUT_DIR = REPO_ROOT / "autonav-urban" / "scripts" / "diagnose_out"
OUT_DIR.mkdir(parents=True, exist_ok=True)
stem = img_path.stem
if override_ckpt:
    stem = f"{stem}__{override_ckpt.stem}"

img = Image.open(img_path).convert("RGB")
frame_np = np.asarray(img, dtype=np.uint8)
print(f"Loaded: {img_path.name}  shape={frame_np.shape}  dtype={frame_np.dtype}")

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
import autonav_urban  # noqa: F401
from autonav_urban import CONFIGS_ROOT, THIRD_PARTY_ROOT
from autonav_urban.samtp import SAMTPModel

def jet_colormap(t):
    """JET colormap approximation matching matplotlib."""
    t = np.clip(np.asarray(t, dtype=np.float32), 0.0, 1.0)
    r = np.clip(1.5 - np.abs(4.0 * t - 3.0), 0.0, 1.0)
    g = np.clip(1.5 - np.abs(4.0 * t - 2.0), 0.0, 1.0)
    b = np.clip(1.5 - np.abs(4.0 * t - 1.0), 0.0, 1.0)
    return (np.stack([r, g, b], axis=-1) * 255).astype(np.uint8)
import yaml

with (CONFIGS_ROOT / "mini_urban.yaml").open("r") as f:
    cfg = yaml.safe_load(f) or {}
samtp_cfg = cfg.get("samtp", {}) or {}
cfg_path = str(THIRD_PARTY_ROOT / samtp_cfg.get(
    "config_path", "sam2/configs/sam2.1_inference_tiny/sam2.1_custom2.yaml"))
ckpt_path = str(override_ckpt) if override_ckpt else str(THIRD_PARTY_ROOT / samtp_cfg.get(
    "checkpoint_path", "sam2_ckpt/checkpoint_2.pt"))
print(f"Using checkpoint: {ckpt_path}")

t0 = time.time()
model = SAMTPModel(cfg_path=cfg_path, ckpt_path=ckpt_path, device=None)
print(f"Loaded SAM-TP on {model.device} ({time.time()-t0:.1f}s)")

t1 = time.time()
out = model.run_sam2_inference(frame_np)
print(f"Inference: {time.time()-t1:.2f}s")

from genie_path_planner.projection import logits_to_traversability
trav = logits_to_traversability(out["logits"],
                                 samtp_cfg.get("score_transform", "sigmoid"))

print(f"\nSAM-TP output stats:")
print(f"  Logits: min={out['logits'].min():+.2f}  mean={out['logits'].mean():+.2f}  max={out['logits'].max():+.2f}")
print(f"  Traversability: min={trav.min():.3f}  mean={trav.mean():.3f}  max={trav.max():.3f}")
print(f"  Fraction drivable (>0.5): {(trav > 0.5).mean():.3f}  ({(trav > 0.5).sum()} pixels)")

# 1) JET colormap - matches paper
jet = jet_colormap(trav)
Image.fromarray(jet).save(OUT_DIR / f"{stem}__jet.png")

# 2) Red/green like our dashboard
green = (trav * 255).astype(np.uint8)
red = ((1.0 - trav) * 255).astype(np.uint8)
zeros = np.zeros_like(green)
rg = np.stack([red, green, zeros], axis=-1)
Image.fromarray(rg).save(OUT_DIR / f"{stem}__redgreen.png")

# 3) Red overlay on frame (paper's front-camera style)
alpha = 0.5
red_mask = np.zeros_like(frame_np)
red_mask[..., 0] = 255
drivable = (trav > 0.5).astype(np.float32)[..., None] * alpha
overlay = (frame_np.astype(np.float32) * (1 - drivable) + red_mask * drivable).astype(np.uint8)
Image.fromarray(overlay).save(OUT_DIR / f"{stem}__overlay.png")

print(f"\nSaved 3 outputs to {OUT_DIR}/")
print(f"  {stem}__jet.png       - JET colormap (like paper)")
print(f"  {stem}__redgreen.png  - red/green (like our dashboard)")
print(f"  {stem}__overlay.png   - red overlay on frame (like paper's front cam)")
