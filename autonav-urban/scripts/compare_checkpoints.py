"""Run TWO SAM-TP checkpoints on the same image, side-by-side + diff.

Usage:
    python autonav-urban/scripts/compare_checkpoints.py <image> [<baseline_ckpt>] [<test_ckpt>]

Defaults:
    baseline_ckpt = paper's checkpoint (autonav-urban/third_party/sam2_ckpt/checkpoint_2.pt)
    test_ckpt     = /Users/dev/Downloads/smoke_test.pt

Saves outputs to autonav-urban/scripts/diagnose_out/compare_<image_stem>.png
and prints per-pixel difference stats.
"""

import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

if len(sys.argv) < 2:
    print("Usage: python compare_checkpoints.py <image> [<baseline_ckpt>] [<test_ckpt>]")
    sys.exit(1)

img_path = Path(sys.argv[1])
if not img_path.exists():
    print(f"ERROR: {img_path} not found")
    sys.exit(1)

REPO_ROOT = Path("/Users/dev/Documents/earth-rovers-sdk")
sys.path.insert(0, str(REPO_ROOT / "autonav-urban"))
sys.path.insert(0, str(REPO_ROOT / "autonav-urban" / "third_party"))

DEFAULT_BASELINE = REPO_ROOT / "autonav-urban" / "third_party" / "sam2_ckpt" / "checkpoint_2.pt"
DEFAULT_TEST = Path("/Users/dev/Downloads/smoke_test.pt")

baseline_ckpt = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_BASELINE
test_ckpt = Path(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_TEST

for p in (baseline_ckpt, test_ckpt):
    if not p.exists():
        print(f"ERROR: {p} not found")
        sys.exit(1)

OUT_DIR = REPO_ROOT / "autonav-urban" / "scripts" / "diagnose_out"
OUT_DIR.mkdir(parents=True, exist_ok=True)

img = Image.open(img_path).convert("RGB")
frame_np = np.asarray(img, dtype=np.uint8)
H, W = frame_np.shape[:2]
print(f"Image: {img_path.name}  shape={frame_np.shape}")

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
import autonav_urban  # noqa: F401
from autonav_urban import CONFIGS_ROOT, THIRD_PARTY_ROOT
from autonav_urban.samtp import SAMTPModel
import yaml

with (CONFIGS_ROOT / "mini_urban.yaml").open("r") as f:
    cfg = yaml.safe_load(f) or {}
samtp_cfg = cfg.get("samtp", {}) or {}
cfg_path = str(THIRD_PARTY_ROOT / samtp_cfg.get(
    "config_path", "sam2/configs/sam2.1_inference_tiny/sam2.1_custom2.yaml"))

from genie_path_planner.projection import logits_to_traversability
transform = samtp_cfg.get("score_transform", "sigmoid")


def jet(t):
    t = np.clip(np.asarray(t, dtype=np.float32), 0.0, 1.0)
    r = np.clip(1.5 - np.abs(4.0 * t - 3.0), 0.0, 1.0)
    g = np.clip(1.5 - np.abs(4.0 * t - 2.0), 0.0, 1.0)
    b = np.clip(1.5 - np.abs(4.0 * t - 1.0), 0.0, 1.0)
    return (np.stack([r, g, b], axis=-1) * 255).astype(np.uint8)


def run(name, ckpt):
    print(f"\n=== {name}: {ckpt.name} ({ckpt.stat().st_size // (1024*1024)} MB) ===")
    t0 = time.time()
    m = SAMTPModel(cfg_path=cfg_path, ckpt_path=str(ckpt), device=None)
    print(f"  loaded on {m.device} ({time.time()-t0:.1f}s)")
    t1 = time.time()
    out = m.run_sam2_inference(frame_np)
    print(f"  inference {time.time()-t1:.2f}s")
    trav = logits_to_traversability(out["logits"], transform)
    print(f"  logits: min={out['logits'].min():+.2f}  mean={out['logits'].mean():+.2f}  max={out['logits'].max():+.2f}")
    print(f"  trav:   min={trav.min():.3f}  mean={trav.mean():.3f}  max={trav.max():.3f}  drivable_frac={(trav > 0.5).mean():.3f}")
    return trav


trav_base = run("BASELINE (paper)", baseline_ckpt)
trav_test = run("TEST      ", test_ckpt)

# Difference stats
diff = trav_test - trav_base
abs_diff = np.abs(diff)
max_diff = abs_diff.max()
mean_diff = abs_diff.mean()
n_changed = int((abs_diff > 0.05).sum())
frac_changed = n_changed / abs_diff.size

print(f"\n=== DIFFERENCE ===")
print(f"  max |trav_test - trav_base| = {max_diff:.3f}")
print(f"  mean |trav_test - trav_base| = {mean_diff:.4f}")
print(f"  pixels changed by >0.05 = {n_changed} ({frac_changed*100:.2f}%)")
print(f"  drivable-fraction shift: baseline={(trav_base > 0.5).mean():.3f} → test={(trav_test > 0.5).mean():.3f}  (Δ={((trav_test > 0.5).mean() - (trav_base > 0.5).mean())*100:+.2f}%)")

if mean_diff < 0.005 and n_changed < 100:
    verdict = "IDENTICAL — the test checkpoint has NOT been meaningfully trained yet."
elif mean_diff < 0.05:
    verdict = "SIMILAR — very small changes. Likely a smoke test with ≤1 epoch."
elif mean_diff < 0.15:
    verdict = "NOTICEABLE — the checkpoint has been trained for several epochs."
else:
    verdict = "SIGNIFICANT — the checkpoint has diverged meaningfully from baseline."
print(f"\nVERDICT: {verdict}")

# Build a side-by-side composite: [frame | baseline overlay | test overlay | diff heatmap]
def overlay(rgb, trav, alpha=0.5):
    red = np.zeros_like(rgb, dtype=np.float32)
    red[..., 0] = 255
    m = (trav > 0.5).astype(np.float32)[..., None] * alpha
    return (rgb.astype(np.float32) * (1 - m) + red * m).astype(np.uint8)


ov_base = overlay(frame_np, trav_base)
ov_test = overlay(frame_np, trav_test)
# Diff visualization: signed, red = TEST says more obstacle, blue = TEST says more drivable
diff_vis = np.zeros((H, W, 3), dtype=np.float32)
diff_vis[..., 0] = np.clip(-diff, 0, 1) * 255   # red where test says LESS drivable
diff_vis[..., 2] = np.clip(diff, 0, 1) * 255    # blue where test says MORE drivable
diff_vis = diff_vis.astype(np.uint8)

# Concatenate horizontally with labels
gap = 8
composite = np.ones((H + 40, W * 4 + gap * 3, 3), dtype=np.uint8) * 20
composite[40:40+H, 0:W] = frame_np
composite[40:40+H, W+gap:2*W+gap] = ov_base
composite[40:40+H, 2*W+2*gap:3*W+2*gap] = ov_test
composite[40:40+H, 3*W+3*gap:4*W+3*gap] = diff_vis

pil = Image.fromarray(composite)
draw = ImageDraw.Draw(pil)
for i, label in enumerate(["FRAME", f"BASELINE ({baseline_ckpt.name})", f"TEST ({test_ckpt.name})", "DIFF (red=test↓ blue=test↑)"]):
    draw.text((i * (W + gap) + 8, 8), label, fill=(240, 240, 240))
out_path = OUT_DIR / f"compare_{img_path.stem}.png"
pil.save(out_path)
print(f"\nSaved side-by-side comparison: {out_path}")
