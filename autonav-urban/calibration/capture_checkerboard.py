"""Capture checkerboard frames from the live Mini+ front camera (for intrinsics).

WHEN: one-time, before any autonomous missions. Needs the camera feed live, so
start a session first (POST /start-mission) — you are NOT driving, just grabbing
photos of a checkerboard held in front of the camera.

Usage (server running on :8000, mission started, checkerboard in hand):
    python capture_checkerboard.py --count 20
Press ENTER to grab each frame. Move the board around between shots: different
angles, distances, and corners of the view. Aim for ~20 good frames.
"""
import argparse, base64, os, sys, time
import urllib.request, json

p = argparse.ArgumentParser()
p.add_argument("--url", default="http://localhost:8000/v2/front")
p.add_argument("--out", default="captures")
p.add_argument("--count", type=int, default=20)
args = p.parse_args()

os.makedirs(args.out, exist_ok=True)


def grab():
    with urllib.request.urlopen(args.url, timeout=10) as r:
        body = json.loads(r.read().decode())
    b64 = body.get("front_frame")
    if not b64:
        raise RuntimeError(f"no front_frame in response: {list(body)}")
    return base64.b64decode(b64)


print(f"Capturing to {args.out}/  (target {args.count}). "
      "Hold the checkerboard in view; press ENTER to grab, 'q'+ENTER to quit.")
n = len([f for f in os.listdir(args.out) if f.endswith('.jpg')])
while n < args.count:
    cmd = input(f"[{n}/{args.count}] ENTER=grab, q=quit > ").strip().lower()
    if cmd == "q":
        break
    try:
        jpg = grab()
    except Exception as e:
        print(f"  grab failed: {e} (is the mission started + camera live?)")
        continue
    path = os.path.join(args.out, f"cal_{n:03d}.jpg")
    with open(path, "wb") as f:
        f.write(jpg)
    n += 1
    print(f"  saved {path} ({len(jpg)//1024} KB)")

print(f"Done. {n} frames in {args.out}/. Next: python compute_intrinsics.py --captures {args.out}")
