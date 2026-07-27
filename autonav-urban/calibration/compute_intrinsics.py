"""Compute Mini+ camera intrinsics from checkerboard captures (OpenCV).

WHEN: one-time, OFFLINE (on your laptop) — no rover needed once frames are captured.

Prints the intrinsics matrix K + the reprojection error (lower = better; aim < 0.5 px)
and writes:
  mini_camera_K.npy     (3x3 intrinsics — auto-loaded by autonav_urban/bev.py)
  mini_camera_dist.npy  (lens distortion coefficients)

Usage:
    pip install opencv-python numpy
    python compute_intrinsics.py --captures captures --rows 6 --cols 9 --square-size 0.025
`--rows`/`--cols` are the number of INNER corners (a standard 7x10-square board = 6x9 inner).
`--square-size` is the printed square edge length in METERS.
"""
import argparse, glob, os, sys
import numpy as np

try:
    import cv2
except ImportError:
    sys.exit("OpenCV not installed. Run: pip install opencv-python")

p = argparse.ArgumentParser()
p.add_argument("--captures", default="captures")
p.add_argument("--rows", type=int, default=6, help="inner corners per column")
p.add_argument("--cols", type=int, default=9, help="inner corners per row")
p.add_argument("--square-size", type=float, default=0.025, help="square edge length, meters")
p.add_argument("--out-k", default="mini_camera_K.npy")
p.add_argument("--out-dist", default="mini_camera_dist.npy")
args = p.parse_args()

pattern = (args.rows, args.cols)
# 3D object points for one board (z=0 plane), scaled to real meters
objp = np.zeros((args.rows * args.cols, 3), np.float32)
objp[:, :2] = np.mgrid[0:args.cols, 0:args.rows].T.reshape(-1, 2)
objp *= args.square_size

obj_points, img_points = [], []
imgs = sorted(glob.glob(os.path.join(args.captures, "*.jpg")))
if not imgs:
    sys.exit(f"No .jpg captures in {args.captures}/ — run capture_checkerboard.py first.")

img_size = None
found = 0
criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)
for fp in imgs:
    img = cv2.imread(fp)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    img_size = gray.shape[::-1]
    ok, corners = cv2.findChessboardCorners(gray, (args.cols, args.rows), None)
    if not ok:
        print(f"  no board found in {os.path.basename(fp)} (skip)")
        continue
    corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), criteria)
    obj_points.append(objp)
    img_points.append(corners)
    found += 1
    print(f"  ok: {os.path.basename(fp)}")

print(f"\nfound board in {found}/{len(imgs)} frames")
if found < 8:
    sys.exit("Need >=8 good frames. Capture more, varying angle/distance.")

rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(obj_points, img_points, img_size, None, None)
print(f"\nresolution : {img_size[0]}x{img_size[1]}")
print(f"reprojection error (RMS): {rms:.3f} px  {'GOOD' if rms < 0.5 else 'OK' if rms < 1.0 else 'HIGH — recapture'}")
print("K =\n", np.array2string(K, precision=2))
np.save(args.out_k, K.astype(np.float64))
np.save(args.out_dist, dist.astype(np.float64))
print(f"\nsaved {args.out_k} + {args.out_dist}")
print("These are auto-loaded by autonav_urban/bev.py on the next /autonav-urban/start.")
