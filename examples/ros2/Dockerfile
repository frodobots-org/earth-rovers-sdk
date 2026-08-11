# ROS 2 environment for the Earth Rovers SDK examples, for hosts without a
# native ROS install (macOS, Windows). The SDK server itself stays on the
# host — reach it from inside the container at http://host.docker.internal:8000.
#
#   docker build -t er-ros2 examples/ros2
#   docker run -it --rm -v "$PWD/examples/ros2:/ws" er-ros2 \
#     python3 /ws/er_poll_benchmark.py --ros-args \
#       -p sdk_url:=http://host.docker.internal:8000
FROM ros:humble

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ros-humble-cv-bridge \
        python3-opencv \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

# requests ships with the base image; websocket-client is for the bridge's
# /ws/data telemetry loop.
RUN pip3 install --no-cache-dir requests websocket-client

WORKDIR /ws
