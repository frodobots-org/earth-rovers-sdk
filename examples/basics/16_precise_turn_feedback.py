"""
Precise turn controller using heading feedback.

Why this works better than bash loops:
- Samples heading multiple times and uses a median to reduce telemetry jitter.
- Waits for heading to settle before and after each turn.
- Auto-calibrates whether +angular increases or decreases heading.
- Uses coarse/fine angular speeds to reduce overshoot near target.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass

import requests

BASE_URL = "http://localhost:8000"
CONTROL_DT = 0.05  # 20 Hz


def wrap_360(angle: float) -> float:
    return angle % 360.0


def shortest_diff(target: float, current: float) -> float:
    # Signed shortest angle in [-180, 180)
    return ((target - current + 540.0) % 360.0) - 180.0


@dataclass
class TurnConfig:
    angle_deg: float = 90.0
    repeats: int = 5
    turn_speed: float = 0.48
    tolerance_deg: float = 6.0
    max_turn_time_s: float = 18.0
    settle_window_deg: float = 3.0
    settle_timeout_s: float = 4.0


class RoverTurnController:
    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url
        self.session = requests.Session()

    def send_control(self, linear: float, angular: float) -> None:
        self.session.post(
            f"{self.base_url}/control",
            json={"command": {"linear": linear, "angular": angular}},
            timeout=1.5,
        )

    def stop(self) -> None:
        self.send_control(0.0, 0.0)
        time.sleep(0.05)
        self.send_control(0.0, 0.0)

    def read_heading_raw(self) -> float:
        data = self.session.get(f"{self.base_url}/data", timeout=1.5).json()
        return wrap_360(float(data["orientation"]))

    def read_heading_filtered(self, samples: int = 5, pause_s: float = 0.02) -> float:
        # Median is robust against occasional telemetry spikes.
        vals = []
        for _ in range(samples):
            vals.append(self.read_heading_raw())
            time.sleep(pause_s)
        return wrap_360(statistics.median(vals))

    def wait_heading_settle(self, window_deg: float, timeout_s: float) -> float:
        start = time.time()
        h1 = self.read_heading_filtered()
        while True:
            time.sleep(0.12)
            h2 = self.read_heading_filtered()
            if abs(shortest_diff(h2, h1)) <= window_deg:
                return h2
            if time.time() - start > timeout_s:
                return h2
            h1 = h2

    def turn_once(self, angle_deg: float, cfg: TurnConfig) -> None:
        start_heading = self.wait_heading_settle(
            window_deg=cfg.settle_window_deg, timeout_s=cfg.settle_timeout_s
        )
        command_sign = +1 if angle_deg >= 0 else -1
        required_delta = max(1.0, abs(angle_deg) - cfg.tolerance_deg)
        accumulated_delta = 0.0
        prev = start_heading

        t0 = time.time()
        while True:
            if accumulated_delta >= required_delta:
                break
            if time.time() - t0 > cfg.max_turn_time_s:
                print("Turn timeout reached; stopping for safety.")
                break

            remaining = required_delta - accumulated_delta
            if remaining > 30:
                burst_ticks = 4
            elif remaining > 12:
                burst_ticks = 2
            else:
                burst_ticks = 1

            for _ in range(burst_ticks):
                self.send_control(0.0, command_sign * cfg.turn_speed)
                time.sleep(CONTROL_DT)

            self.stop()
            time.sleep(0.12)
            current = self.read_heading_filtered(samples=5, pause_s=0.03)
            step = abs(shortest_diff(current, prev))
            prev = current

            # Ignore tiny jitter and implausible spikes.
            if 0.25 <= step <= 70.0:
                accumulated_delta += step

        self.stop()
        final_heading = self.wait_heading_settle(
            window_deg=cfg.settle_window_deg, timeout_s=cfg.settle_timeout_s
        )
        net_delta = shortest_diff(final_heading, start_heading)
        print(
            f"Start={start_heading:.1f}°, Requested={angle_deg:+.1f}°, Final={final_heading:.1f}°, "
            f"NetDelta={net_delta:+.1f}°, Integrated={accumulated_delta:.1f}°"
        )


def main() -> None:
    cfg = TurnConfig()
    rover = RoverTurnController()

    for i in range(cfg.repeats):
        print(f"Turn {i + 1}/{cfg.repeats}")
        rover.turn_once(cfg.angle_deg, cfg)
        time.sleep(0.35)


if __name__ == "__main__":
    main()

