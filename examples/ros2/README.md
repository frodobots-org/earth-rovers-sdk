# ROS2 Bridge for the Earth Rovers SDK

A single-node bridge (`earth_rover_bridge.py`) that maps the SDK's API onto
standard ROS2 topics, so your stack talks plain ROS:

| ROS2 topic | Type | Direction | SDK source |
|---|---|---|---|
| `/cmd_vel` | `geometry_msgs/Twist` | subscribe | `POST /control` |
| `/earth_rover/front/image_raw` | `sensor_msgs/Image` | publish | `GET /feed` (MJPEG) |
| `/earth_rover/gps` | `sensor_msgs/NavSatFix` | publish | `WS /ws/data` |
| `/earth_rover/imu` | `sensor_msgs/Imu` | publish | `WS /ws/data` |
| `/earth_rover/battery` | `sensor_msgs/BatteryState` | publish | `WS /ws/data` |
| `/earth_rover/heading` | `std_msgs/Float32` (degrees) | publish | `WS /ws/data` |

## Behavior notes

- **`cmd_vel` is latest-wins at 10 Hz**: the bridge keeps only the newest Twist
  and posts it to `/control` at a fixed rate — bursts of commands never queue up.
- **Safety stop**: if no `cmd_vel` arrives for 0.5 s, stop commands are retried
  until the SDK acknowledges one. Shutdown also makes three best-effort stop
  attempts. Standard teleop/nav stacks that publish continuously work unchanged.
- **Twist mapping**: `linear.x` and `angular.z` are passed through clamped to
  the SDK's `-1..1` range. Treat them as normalized effort, not m/s / rad/s.
- Video, telemetry and control run concurrently. HTTP control has its own fixed-
  rate worker, so a slow request cannot block the ROS executor or `cmd_vel`.
- Camera and sensor topics use best-effort, depth-one QoS to prevent stale data
  from accumulating behind a slow subscriber.
- Everything reconnects automatically if the SDK restarts.

## Setup

Requires a ROS2 distro (Humble or newer recommended) with `rclpy` and
`cv_bridge`, plus:

```bash
pip install requests websocket-client opencv-python
```

## Run

1. Start the SDK (and the mission, if you use one):

```bash
hypercorn main:app
curl -X POST http://localhost:8000/start-mission   # if MISSION_SLUG is set
```

2. Run the bridge:

```bash
python3 earth_rover_bridge.py
# SDK on another machine:
python3 earth_rover_bridge.py --ros-args -p sdk_url:=http://192.168.1.50:8000
```

3. Drive and watch:

```bash
ros2 run teleop_twist_keyboard teleop_twist_keyboard
ros2 run rqt_image_view rqt_image_view /earth_rover/front/image_raw
ros2 topic echo /earth_rover/gps
```

## Consuming the feed without ROS

`/feed` is a plain MJPEG stream — one line in OpenCV from any process:

```python
import cv2
cap = cv2.VideoCapture("http://localhost:8000/feed?view=front&fps=15")
while True:
    ok, frame = cap.read()
```
