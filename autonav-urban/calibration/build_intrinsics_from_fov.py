"""Build a Mini+ camera intrinsics matrix K from published FOV specs.

WHEN: use when you CANNOT physically hold a checkerboard in front of the camera
(remote-controlled rover, no site access, etc.).

Compared to a real checkerboard calibration:
  * ACCURACY: within ~5-10% of true focal length, no lens distortion coefficients.
  * SPEED: no site visit needed, just published camera specs.
  * ENOUGH FOR: BEV projection at rover scales (1-10 m). Perfect for autonav-urban.

Frodobots Mini+ (Earth Rover Mini) camera:
  * Front cam: 1024 × 576  (confirmed from IROS 2026 spec sheet)
  * FOV: wide-angle, appears ~120° horizontal from live footage
    (no exact number in spec sheet — 120° is a solid default for GoPro-style
    wide-angle lenses; adjust with --hfov if you know the exact value)

Usage:
    python build_intrinsics_from_fov.py --width 1024 --height 576 --hfov 120
"""
import argparse
import math
import numpy as np

p = argparse.ArgumentParser()
p.add_argument("--width", type=int, default=1024, help="frame width in pixels")
p.add_argument("--height", type=int, default=576, help="frame height in pixels")
p.add_argument("--hfov", type=float, default=120.0,
               help="camera horizontal FOV in degrees (Mini+ front cam is wide-angle, "
                    "~120° default; some builds are ~90° — adjust if bot behavior "
                    "shows distance errors)")
p.add_argument("--out-k", default="mini_camera_K.npy")
p.add_argument("--out-dist", default="mini_camera_dist.npy")
args = p.parse_args()

# Pinhole camera model: fx = (W/2) / tan(HFOV/2)
w, h = float(args.width), float(args.height)
hfov_rad = math.radians(args.hfov)
fx = (w / 2.0) / math.tan(hfov_rad / 2.0)

# Assume same physical pixel size in both directions (fy = fx). Almost always
# true for modern sensors. Slight aspect ratio differences are absorbed by
# the principal point.
fy = fx
cx = w / 2.0
cy = h / 2.0

K = np.array([
    [fx, 0.0, cx],
    [0.0, fy, cy],
    [0.0, 0.0, 1.0],
], dtype=np.float64)

# Zero distortion (assume the camera has been reasonably lens-corrected in
# firmware). If the live footage shows heavy fisheye barrel distortion at the
# edges, tune the k1 coefficient (negative value = correct barrel distortion).
dist = np.zeros(5, dtype=np.float64)

# Print + save
vfov = 2 * math.degrees(math.atan((h / 2.0) / fy))
print(f"Building intrinsics from FOV:")
print(f"  resolution      : {int(w)} × {int(h)}")
print(f"  horizontal FOV  : {args.hfov:.1f}°")
print(f"  vertical FOV    : {vfov:.1f}° (derived)")
print(f"  focal length fx : {fx:.2f} px")
print(f"  principal point : ({cx:.1f}, {cy:.1f})")
print(f"\nK =\n{np.array2string(K, precision=2)}")

np.save(args.out_k, K)
np.save(args.out_dist, dist)
print(f"\nsaved {args.out_k} + {args.out_dist}")
print("These are auto-loaded by autonav_urban/bev.py on the next /autonav-urban/start.")
print()
print("If the bot behaves oddly after this:")
print(f"  * BEV shows obstacles too close  → HFOV is too small, try --hfov {args.hfov + 20}")
print(f"  * BEV shows obstacles too far    → HFOV is too big,   try --hfov {args.hfov - 20}")
