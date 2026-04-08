"""
Color Card Tracking Example - Earth Rover SDK

Hold a colored card in front of the rover's camera.
The rover will turn to center it, then drive toward it,
stopping when the card fills ~15% of the frame.

Supported colors: red, green, blue, yellow, pink

Usage:
    python examples/basics/17_color_tracking.py --color red
    python examples/basics/17_color_tracking.py --color blue --speed 0.3 --kp 0.4

Controls:
    q  — quit
    Ctrl+C — stop and exit
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
}

# ---------------------------------------------------------------------------
# Tunable defaults
# ---------------------------------------------------------------------------
KP_ANGULAR = 0.8       # proportional gain: angular correction per unit offset
MAX_FORWARD = 0.45     # max linear speed (rover units, 0–1)
STOP_FILL = 0.15       # stop when blob occupies this fraction of frame area
MIN_BLOB_AREA = 500    # px² — ignore blobs smaller than this (noise filter)
SEARCH_ANGULAR = 0.35  # rotation speed when no target is visible
LOOP_HZ = 10           # control loop frequency

# Overlay colours (BGR)
_GREEN = (0, 255, 0)
_WHITE = (255, 255, 255)
_YELLOW = (0, 220, 255)
_RED = (0, 0, 220)

# Shared session — set in track_color(), used by send_command / fetch_front_frame
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
        pass  # swallow transient errors; loop will retry next tick


def stop() -> None:
    send_command(0.0, 0.0)


def fetch_front_frame() -> np.ndarray | None:
    """Fetch the front camera frame and return it as a BGR numpy array."""
    try:
        resp = _session.get(f"{BASE_URL}/v2/front", timeout=1.0)
        data = resp.json()
        b64 = data.get("front_frame")
        if not b64:
            return None
        image_bytes = base64.b64decode(b64)
        image_array = np.frombuffer(image_bytes, dtype=np.uint8)
        return cv2.imdecode(image_array, cv2.IMREAD_COLOR)
    except Exception:
        return None


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


def find_largest_blob(mask: np.ndarray) -> tuple[int, int, float] | None:
    """
    Find the largest blob in a binary mask.
    Returns (cx, cy, area) in pixels, or None if no blob exceeds MIN_BLOB_AREA.
    """
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    largest = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(largest)
    if area < MIN_BLOB_AREA:
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

        # Draw contours from mask
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(out, contours, -1, _GREEN, 2)

        # Centroid dot
        cv2.circle(out, (cx_blob, cy_blob), 8, _GREEN, -1)

        # Line from frame centre to blob centroid
        cv2.line(out, (cx_frame, cy_frame), (cx_blob, cy_blob), _YELLOW, 1)

        fill_str = f"{fill_pct:.1f}%"
    else:
        fill_str = "—"

    # Status text (top-left)
    color_label = color_name.upper()
    state_color = _GREEN if state == "TRACKING" else (_YELLOW if state == "SEARCHING" else _RED)
    lines = [
        (f"Target: {color_label}  Fill: {fill_str}", _WHITE),
        (f"State: {state}  Lin: {linear:+.2f}  Ang: {angular:+.2f}", state_color),
        ("'q' to quit", _WHITE),
    ]
    for i, (text, color) in enumerate(lines):
        cv2.putText(out, text, (10, 25 + i * 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)

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
    print("Hold the card in front of the camera. Press 'q' or Ctrl+C to stop.\n")

    state = "SEARCHING"
    linear = 0.0
    angular = search_angular
    mask = None
    blob = None
    frame_area = 1  # safe default before first frame

    try:
        while True:
            t_start = time.time()

            frame = fetch_front_frame()
            if frame is None:
                send_command(0.0, 0.0)
                time.sleep(loop_dt)
                continue

            frame_h, frame_w = frame.shape[:2]
            frame_area = frame_h * frame_w
            cx_frame = frame_w / 2

            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
            mask = build_color_mask(hsv, color_name)
            blob = find_largest_blob(mask)

            if blob is None:
                state = "SEARCHING"
                linear = 0.0
                angular = search_angular
            else:
                cx_blob, cy_blob, blob_area = blob
                fill_ratio = blob_area / frame_area

                if fill_ratio >= stop_fill:
                    state = "ARRIVED"
                    linear = 0.0
                    angular = 0.0
                else:
                    state = "TRACKING"
                    offset_norm = (cx_blob - cx_frame) / (frame_w / 2)

                    # Dead zone: ignore tiny offsets to reduce jitter
                    if abs(offset_norm) < 0.05:
                        offset_norm = 0.0
                    angular = clamp(kp_angular * offset_norm, -1.0, 1.0)

                    # Suppress forward speed when card is off-center.
                    # center_factor → 1 when centred, → 0 when |offset| ≥ 0.4
                    # This makes the rover turn first, then drive forward.
                    center_factor = clamp(1.0 - abs(offset_norm) / 0.4, 0.0, 1.0)
                    linear = clamp(
                        max_forward * (1.0 - fill_ratio / stop_fill) * center_factor,
                        0.0,
                        max_forward,
                    )

            send_command(linear, angular)

            # Display
            display = draw_overlay(frame, mask if mask is not None else np.zeros_like(frame[:, :, 0]),
                                   blob, frame_area, color_name, state, linear, angular)
            cv2.imshow("Color Tracking", display)

            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                print("Quit by user.")
                break

            if state == "ARRIVED":
                fill_pct = blob[2] / frame_area * 100 if blob else 0
                print(f"Target reached! Fill: {fill_pct:.1f}%")
                time.sleep(1.0)
                break

            # Pace to LOOP_HZ
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
        description="Drive the rover toward a colored card using HSV color tracking."
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
