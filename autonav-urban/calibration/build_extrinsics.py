"""Build the Mini+ camera mount transform T_base_camera from simple measurements.

WHEN: one-time, OFFLINE. No rover feed needed — just a ruler and a protractor.

Measure three things on your Mini+:
  --height  : camera lens height above the ground, in METERS (e.g. 0.15)
  --pitch   : how far the camera tilts DOWN from horizontal, in DEGREES (e.g. 10)
  --forward : how far the lens sits AHEAD of the rover's turning center, METERS (e.g. 0.10)

Writes mini_T_base_camera.npy (4x4), auto-loaded by autonav_urban/bev.py.

Convention (must match bev.py):
  base frame  : +x forward, +y left, +z up   (ROS)
  camera frame: +x right,  +y down, +z forward (optical)

Usage:
    python build_extrinsics.py --height 0.15 --pitch 10 --forward 0.10
"""
import argparse, math
import numpy as np

p = argparse.ArgumentParser()
p.add_argument("--height", type=float, required=True, help="lens height above ground (m)")
p.add_argument("--pitch", type=float, required=True, help="downward tilt from horizontal (deg)")
p.add_argument("--forward", type=float, default=0.10, help="lens offset ahead of rover center (m)")
p.add_argument("--out", default="mini_T_base_camera.npy")
args = p.parse_args()

pr = math.radians(args.pitch)
cp, sp = math.cos(pr), math.sin(pr)

# Columns = camera axes expressed in the base frame (identical construction to
# autonav_urban/bev.py::_placeholder_T_base_camera, but with YOUR measurements):
#   camera x (right)   = base -y                    = (0, -1,  0)
#   camera y (down)    = -base z, tilted by pitch   = (sp,  0, -cp)
#   camera z (forward) = base x, tilted by pitch    = (cp,  0, -sp)
r_base_cam = np.array(
    [
        [0.0,  sp,  cp],
        [-1.0, 0.0, 0.0],
        [0.0, -cp, -sp],
    ],
    dtype=np.float64,
)
T = np.eye(4, dtype=np.float64)
T[:3, :3] = r_base_cam
T[:3, 3] = np.array([args.forward, 0.0, args.height], dtype=np.float64)

np.save(args.out, T)
print("T_base_camera =\n", np.array2string(T, precision=4))
print(f"\nsaved {args.out}")
print(f"(height={args.height} m, pitch_down={args.pitch}°, forward={args.forward} m)")
print("Auto-loaded by autonav_urban/bev.py on the next /autonav-urban/start.")
