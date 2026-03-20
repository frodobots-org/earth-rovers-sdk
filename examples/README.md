# Earth Rover SDK Examples

This folder contains Python examples demonstrating various capabilities of the Earth Rover SDK.

## Prerequisites

Make sure the SDK server is running on `http://localhost:8000` before running any examples.

```bash
# Install dependencies
pip install requests aiohttp numpy opencv-python

# For keyboard control example
pip install keyboard
```

## Examples Overview

### Basic Movement

| Example | Description |
|---------|-------------|
| `basics/01_basic_movement.py` | Fundamental controls: forward, backward, left, right turns |
| `basics/02_diagonal_movement.py` | Curved paths, slalom patterns, combined linear+angular |
| `basics/03_speed_control.py` | Speed levels, smooth acceleration/deceleration |
| `basics/04_lamp_control.py` | Lamp on/off, blinking patterns, SOS signal |

### Telemetry & Cameras

| Example | Description |
|---------|-------------|
| `basics/05_telemetry_monitoring.py` | Read battery, GPS, IMU, motor data |
| `basics/06_dual_camera_stream.py` | Display front and rear camera feeds |
| `basics/12_video_recording.py` | Record video to file, timelapse capture |

### Movement Patterns

| Example | Description |
|---------|-------------|
| `basics/07_square_pattern.py` | Drive in a square with 90-degree turns |
| `basics/08_circle_pattern.py` | Circles, figure-8, spirals, sine wave paths |

### Interactive Control

| Example | Description |
|---------|-------------|
| `basics/09_keyboard_teleop.py` | Real-time WASD keyboard control |
| `basics/13_async_control.py` | Async operations, concurrent monitoring |

### Mission & Autonomy

| Example | Description |
|---------|-------------|
| `basics/10_mission_control.py` | Start/end missions, checkpoints |
| `basics/11_intervention_tracking.py` | Track human intervention periods |
| `basics/14_obstacle_simulation.py` | Simulated obstacle avoidance |
| `basics/15_waypoint_navigation.py` | GPS-based waypoint navigation |

### Camera Feed (Legacy)

| Example | Description |
|---------|-------------|
| `legacy/front_example.py` | Display front camera feed |
| `legacy/rear_example.py` | Display rear camera feed |

### AI Agent (Openclaw)

| Example | Description |
|---------|-------------|
| `openclaw/` | Control your rover via Telegram using [Openclaw](https://openclaw.ai) as an AI agent gateway. Includes workspace config files and full setup guide. |

### Web UI Examples

| Example | Description |
|---------|-------------|
| `web/front_test.html` | Browser-based front camera test |
| `web/rear_test.html` | Browser-based rear camera test |
| `web/intervention_manager.html` | Web UI for intervention tracking |
| `web/test_with_controls_mini.html` | Web controls for mini rover |
| `web/test_with_controls_zero.html` | Web controls for zero rover |

## Quick Start

### Move Forward

```python
import requests

requests.post("http://localhost:8000/control", json={
    "command": {"linear": 0.5, "angular": 0, "lamp": 0}
})
```

### Read Telemetry

```python
import requests

data = requests.get("http://localhost:8000/data").json()
print(f"Battery: {data['battery']}%")
print(f"Position: ({data['latitude']}, {data['longitude']})")
```

### Get Camera Frame

```python
import requests
import base64

response = requests.get("http://localhost:8000/v2/screenshot").json()
front_image = base64.b64decode(response["front_frame"])
```

## Control Reference

### Movement Values

- `linear`: -1.0 (full reverse) to 1.0 (full forward)
- `angular`: -1.0 (full left) to 1.0 (full right)
- `lamp`: 0 (off) or 1 (on)

### Combining Controls

```python
# Curve forward-right
{"linear": 0.5, "angular": 0.3, "lamp": 0}

# Spin in place
{"linear": 0, "angular": 0.5, "lamp": 0}

# Move forward with lamp on
{"linear": 0.5, "angular": 0, "lamp": 1}
```

## Tips

1. Always call `stop()` or send `{"linear": 0, "angular": 0}` when done
2. Use moderate speeds (0.3-0.5) for testing
3. The `/v2/screenshot` endpoint is 15x faster than legacy endpoints
4. For async operations, use `aiohttp` for better performance
