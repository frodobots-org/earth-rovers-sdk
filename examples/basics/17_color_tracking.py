"""
Color Card Tracking Example - Earth Rover SDK

Hold a colored card in front of (or behind) the rover's camera.
The rover uses both front and rear cameras to locate the card,
turns to center it, then drives toward it, stopping when the card
fills ~15% of the front frame.

Supported colors: red, green, blue, yellow, pink, skyblue

Usage:
    python examples/basics/17_color_tracking.py --color skyblue
    python examples/basics/17_color_tracking.py --color red --speed 0.3 --kp 0.8

Controls:
    q       — quit
    Ctrl+C  — stop and exit
"""

import argparse
import base64
import time

import cv2
import numpy as np
import requests

BASE_URL = "http://localhost:8000"

# ---------------------------------------------------------------------------
# HSV color ranges (H: 0-179, S: 0-255, V: 0-255 in OpenCV)
# Each entry is a list of (lower, upper) pairs; red needs two because its
# hue wraps around 0/179.
# ---------------------------------------------------------------------------
COLOR_RANGES: dict[str, list[tuple]] = {
    "red": [
        ((0, 120, 70), (10, 255, 255)),
        ((160, 120, 70), (179, 255, 255)),
    ],
    "green": [
        ((35, 80, 50), (85, 255, 255)),
    ],
    "blue": [
        ((90, 80, 50), (130, 255, 255)),
    ],
    "yellow": [
        ((18, 100, 80), (35, 255, 255)),
    ],
    "pink": [
        # Light pink / salmon (low-saturation red, high brightness)
        ((0, 40, 150), (10, 150, 255)),
        # Hot pink / magenta (hue toward purple-red end)
        ((140, 60, 100), (179, 255, 255)),
    ],
    "skyblue": [
        # Sky blue — very low saturation floor to catch pale/pastel blues
        ((85, 15, 100), (120, 200, 255)),
    ],
}

# ---------------------------------------------------------------------------
# Tunable defaults
# ---------------------------------------------------------------------------
KP_ANGULAR = 0.8         # proportional gain: angular correction per unit offset
MAX_FORWARD = 0.45       # max linear speed (rover units, 0–1)
STOP_FILL = 0.15         # stop when blob occupies this fraction of frame area
MIN_BLOB_AREA = 500      # px² — hard noise floor
MIN_DETECT_FILL = 0.015  # blob must be ≥1.5% of frame to count as a real card
SEARCH_ANGULAR = 0.35    # rotation speed when no target is visible
LOOP_HZ = 10             # control loop frequency

# Overlay colours (BGR)
_GREEN  = (0, 255, 0)
_WHITE  = (255, 255, 255)
_YELLOW = (0, 220, 255)
_RED    = (0, 0, 220)
_CYAN   = (255, 220, 0)

# Shared session — initialised in track_color()
_session: requests.Session | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def send_command(linear: float, angular: float, lamp: int = 0) -> None:
    """Send a movement command to the rover."""
    try:
        _session.post(
            f"{BASE_URL}/control",
            json={"command": {"linear": linear, "angular": angular, "lamp": lamp}},
            timeout=0.5,
        )
    except Exception:
        pass  # swallow transient errors; loop retries next tick


def stop() -> None:
    send_command(0.0, 0.0)


def _decode_b64_frame(b64: str) -> np.ndarray | None:
    """Decode a base64 image string to a BGR numpy array."""
    try:
        image_bytes = base64.b64decode(b64)
        image_array = np.frombuffer(image_bytes, dtype=np.uint8)
        return cv2.imdecode(image_array, cv2.IMREAD_COLOR)
    except Exception:
        return None


def fetch_both_frames() -> tuple[np.ndarray | None, np.ndarray | None]:
    """Fetch front and rear frames in a single request. Returns (front, rear)."""
    try:
        resp = _session.get(f"{BASE_URL}/v2/screenshot", timeout=1.5)
        data = resp.json()
        front = _decode_b64_frame(data.get("front_frame") or "")
        rear  = _decode_b64_frame(data.get("rear_frame") or "")
        return front, rear
    except Exception:
        return None, None


def build_color_mask(hsv_frame: np.ndarray, color_name: str) -> np.ndarray:
    """Build a binary mask for the target color using HSV ranges."""
    pairs = COLOR_RANGES[color_name]
    mask = np.zeros(hsv_frame.shape[:2], dtype=np.uint8)
    for lower, upper in pairs:
        mask = cv2.bitwise_or(
            mask,
            cv2.inRange(hsv_frame, np.array(lower), np.array(upper)),
        )
    # Morphological open (removes noise) then close (fills gaps)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    return mask


def find_largest_blob(mask: np.ndarray, frame_area: int) -> tuple[int, int, float] | None:
    """Find the largest blob in a binary mask.

    Returns (cx, cy, area) in pixels, or None if blob is too small to be a card.
    Two filters:
      - MIN_BLOB_AREA px²: hard noise floor
      - MIN_DETECT_FILL: blob must fill at least 1.5% of the frame
    """
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    largest = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(largest)
    if area < MIN_BLOB_AREA:
        return None
    if frame_area > 0 and (area / frame_area) < MIN_DETECT_FILL:
        return None
    M = cv2.moments(largest)
    if M["m00"] == 0:
        return None
    cx = int(M["m10"] / M["m00"])
    cy = int(M["m01"] / M["m00"])
    return cx, cy, area


def draw_overlay(
    frame: np.ndarray,
    mask: np.ndarray,
    blob: tuple[int, int, float] | None,
    frame_area: int,
    color_name: str,
    state: str,
    linear: float,
    angular: float,
    label: str = "",
) -> np.ndarray:
    """Return an annotated copy of the frame for display."""
    out = frame.copy()
    h, w = out.shape[:2]
    cx_frame, cy_frame = w // 2, h // 2

    # Crosshair at frame centre
    cv2.line(out, (cx_frame, 0), (cx_frame, h), _WHITE, 1)
    cv2.line(out, (0, cy_frame), (w, cy_frame), _WHITE, 1)

    if blob is not None:
        cx_blob, cy_blob, blob_area = blob
        fill_pct = blob_area / frame_area * 100

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(out, contours, -1, _GREEN, 2)
        cv2.circle(out, (cx_blob, cy_blob), 8, _GREEN, -1)
        cv2.line(out, (cx_frame, cy_frame), (cx_blob, cy_blob), _YELLOW, 1)
        fill_str = f"{fill_pct:.1f}%"
    else:
        fill_str = "—"

    tracking = "TRACKING" in state
    state_color = _GREEN if tracking else (_YELLOW if "SEARCH" in state else _CYAN)
    lines = [
        (f"{label}  {color_name.upper()}  Fill: {fill_str}", _WHITE),
        (f"{state}  Lin: {linear:+.2f}  Ang: {angular:+.2f}", state_color),
        ("'q' to quit", _WHITE),
    ]
    for i, (text, color) in enumerate(lines):
        cv2.putText(out, text, (10, 25 + i * 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)
    return out


# ---------------------------------------------------------------------------
# Main tracking loop
# ---------------------------------------------------------------------------

def track_color(
    color_name: str,
    max_forward: float,
    kp_angular: float,
    stop_fill: float,
    search_angular: float,
) -> None:
    global _session
    _session = requests.Session()

    loop_dt = 1.0 / LOOP_HZ

    print(f"=== Color Tracking: {color_name.upper()} ===")
    print(f"  Speed: {max_forward:.2f}  KP: {kp_angular:.2f}  Stop fill: {stop_fill:.0%}")
    print("Hold the card in front of (or behind) the rover. Press 'q' or Ctrl+C to stop.\n")

    state = "SEARCHING"
    linear = 0.0
    angular = search_angular
    fill_ratio = 0.0
    offset_norm = 0.0
    active_cam = "front"
    last_seen_angular = search_angular
    lost_ticks = 0
    fc = 0  # frame counter for periodic printing

    # placeholders so draw_overlay always has valid args
    front_frame_disp: np.ndarray | None = None
    rear_frame_disp:  np.ndarray | None = None
    front_mask_disp:  np.ndarray | None = None
    rear_mask_disp:   np.ndarray | None = None
    front_blob_disp = None
    rear_blob_disp  = None
    rh = rw = 1

    try:
        while True:
            t_start = time.time()

            front_frame, rear_frame = fetch_both_frames()
            if front_frame is None and rear_frame is None:
                send_command(0.0, 0.0)
                time.sleep(loop_dt)
                continue

            # -- Detect on front camera --
            front_blob = None
            front_mask_out = np.zeros((1, 1), dtype=np.uint8)
            fh = fw = 1
            if front_frame is not None:
                fh, fw = front_frame.shape[:2]
                front_hsv = cv2.cvtColor(front_frame, cv2.COLOR_BGR2HSV)
                front_mask_out = build_color_mask(front_hsv, color_name)
                front_blob = find_largest_blob(front_mask_out, fh * fw)

            # -- Detect on rear camera --
            rear_blob = None
            rear_mask_out = np.zeros((1, 1), dtype=np.uint8)
            if rear_frame is not None:
                rh, rw = rear_frame.shape[:2]
                rear_hsv = cv2.cvtColor(rear_frame, cv2.COLOR_BGR2HSV)
                rear_mask_out = build_color_mask(rear_hsv, color_name)
                rear_blob = find_largest_blob(rear_mask_out, rh * rw)

            # Save for display
            front_frame_disp = front_frame
            rear_frame_disp  = rear_frame
            front_mask_disp  = front_mask_out
            rear_mask_disp   = rear_mask_out
            front_blob_disp  = front_blob
            rear_blob_disp   = rear_blob

            # -- Choose camera: front takes priority --
            if front_blob is not None:
                active_cam = "front"
                blob = front_blob
                frame_h, frame_w = fh, fw
            elif rear_blob is not None:
                active_cam = "rear"
                blob = rear_blob
                frame_h, frame_w = rh, rw
            else:
                active_cam = "front"
                blob = None
                frame_h, frame_w = fh, fw

            frame_area = frame_h * frame_w
            cx_frame = frame_w / 2

            # -- Control --
            if blob is None:
                lost_ticks += 1
                fill_ratio = 0.0
                offset_norm = 0.0
                if lost_ticks <= int(LOOP_HZ * 1.0):
                    state = "LAST_SEEN"
                    linear = 0.0
                    angular = last_seen_angular
                else:
                    state = "SEARCHING"
                    linear = 0.0
                    angular = search_angular
            else:
                lost_ticks = 0
                cx_blob, cy_blob, blob_area = blob
                fill_ratio = blob_area / frame_area

                if fill_ratio >= stop_fill:
                    state = "ARRIVED"
                    linear = 0.0
                    angular = 0.0
                    offset_norm = 0.0
                else:
                    offset_norm = (cx_blob - cx_frame) / (frame_w / 2)
                    if abs(offset_norm) < 0.05:
                        offset_norm = 0.0

                    if active_cam == "front":
                        # Front camera is mirrored: negate offset to turn correctly
                        state = "TRACKING(F)"
                        angular = clamp(-kp_angular * offset_norm, -1.0, 1.0)
                        center_factor = clamp(1.0 - abs(offset_norm), 0.0, 1.0)
                        linear = clamp(
                            max_forward * (1.0 - fill_ratio / stop_fill) * center_factor,
                            0.0, max_forward,
                        )
                    else:
                        # Rear camera: spin in place toward the card, no forward motion
                        state = "TRACKING(R)"
                        angular = clamp(kp_angular * offset_norm, -1.0, 1.0)
                        linear = 0.0

                    last_seen_angular = angular

            send_command(linear, angular)

            # Periodic terminal log
            fc += 1
            if fc % 10 == 0:
                print(f"  [{state}]  cam={active_cam}  fill={fill_ratio*100:.1f}%"
                      f"  offset={offset_norm:+.2f}  lin={linear:.2f}  ang={angular:+.2f}")

            # -- Display: side-by-side front + rear --
            base_frame = front_frame_disp if front_frame_disp is not None else \
                         (rear_frame_disp if rear_frame_disp is not None else
                          np.zeros((240, 320, 3), dtype=np.uint8))
            bh, bw = base_frame.shape[:2]

            ann_front = draw_overlay(
                front_frame_disp if front_frame_disp is not None else np.zeros((bh, bw, 3), dtype=np.uint8),
                front_mask_disp if front_mask_disp is not None else np.zeros((bh, bw), dtype=np.uint8),
                front_blob_disp, fh * fw, color_name,
                state if active_cam == "front" else "IDLE",
                linear if active_cam == "front" else 0.0,
                angular if active_cam == "front" else 0.0,
                label="[FRONT]",
            )
            if rear_frame_disp is not None:
                ann_rear = draw_overlay(
                    rear_frame_disp,
                    rear_mask_disp if rear_mask_disp is not None else np.zeros((rh, rw), dtype=np.uint8),
                    rear_blob_disp, rh * rw, color_name,
                    state if active_cam == "rear" else "IDLE",
                    linear if active_cam == "rear" else 0.0,
                    angular if active_cam == "rear" else 0.0,
                    label="[REAR]",
                )
                # Resize rear to match front height
                af_h, af_w = ann_front.shape[:2]
                ar_h, ar_w = ann_rear.shape[:2]
                if af_h != ar_h:
                    ann_rear = cv2.resize(ann_rear, (int(ar_w * af_h / ar_h), af_h))
                display = np.hstack([ann_front, ann_rear])
            else:
                display = ann_front

            cv2.imshow("Color Tracking — Dual Cam", display)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                print("Quit by user.")
                break

            if state == "ARRIVED":
                fill_pct = blob[2] / frame_area * 100 if blob else 0
                print(f"Target reached! Fill: {fill_pct:.1f}%")
                time.sleep(1.0)
                break

            elapsed = time.time() - t_start
            time.sleep(max(0.0, loop_dt - elapsed))

    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        stop()
        cv2.destroyAllWindows()
        _session.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Drive the rover toward a colored card using dual-camera HSV tracking."
    )
    parser.add_argument(
        "--color",
        choices=list(COLOR_RANGES.keys()),
        default="red",
        help="Target card color (default: red)",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=MAX_FORWARD,
        metavar="SPEED",
        help=f"Max forward speed 0–1 (default: {MAX_FORWARD})",
    )
    parser.add_argument(
        "--kp",
        type=float,
        default=KP_ANGULAR,
        metavar="GAIN",
        help=f"Angular proportional gain (default: {KP_ANGULAR})",
    )
    parser.add_argument(
        "--stop-fill",
        type=float,
        default=STOP_FILL,
        dest="stop_fill",
        metavar="FRAC",
        help=f"Blob fill fraction to stop at, 0–1 (default: {STOP_FILL})",
    )
    parser.add_argument(
        "--search",
        type=float,
        default=SEARCH_ANGULAR,
        metavar="SPEED",
        help=f"Rotation speed while searching (default: {SEARCH_ANGULAR})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    track_color(
        color_name=args.color,
        max_forward=args.speed,
        kp_angular=args.kp,
        stop_fill=args.stop_fill,
        search_angular=args.search,
    )


if __name__ == "__main__":
    main()
