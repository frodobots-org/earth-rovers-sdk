# Earth Rovers SDK v4.0

## Requirements

In order to use or run this SDK you need to have an account registered with Frodobots. This is meant for research purposes, if you are interested please reach us here: [Frodobots Discord](https://discord.com/invite/AUegJCJwyb)

- Python 3.9 or higher
- Frodobots API key
- Google Chrome installed

## Getting Started

1. Write once your .env variables provided by Frodobots team your SDK API key and the name of the bot you've got.

```bash
SDK_API_TOKEN=
BOT_SLUG=
CHROME_EXECUTABLE_PATH=
# Default value is MAP_ZOOM_LEVEL=18 https://wiki.openstreetmap.org/wiki/Zoom_levels
MAP_ZOOM_LEVEL=
MISSION_SLUG=
```

2. Install the SDK

```bash
pip3 install -r requirements.txt
```

3. Run the SDK

```bash
hypercorn main:app --reload
```

4. Now you can check the live streaming of the bot in the following URL: http://localhost:8000

## Documentation

This SDK is meant to control the bot and at the same time monitor its status. The SDK has the following open endpoints:

### POST /control

With this endpoint you can send linear and angular values to move the bot. The values are between -1 and 1.

```bash
curl --location 'http://localhost:8000/control' \
--header 'Content-Type: application/json' \
--data '{
    "command": { "linear": 1, "angular": 1 }
}'
```

Example response:

```JSON
{
    "message": "Command sent successfully"
}
```

### GET /data

With this endpoint you can retrieve the latest data from the bot. (e.g. battery level, position, etc.)

```bash
curl --location 'http://localhost:8000/data'
```

Example Response:

```JSON
{
    "battery": 100,
    "signal_level": 5,
    "orientation": 128,
    "lamp": 0,
    "speed": 0,
    "gps_signal": 31.25,
    "latitude": 22.753774642944336,
    "longitude": 114.09095001220703,
    "vibration": 0.31,
    "timestamp": 1724189733.208559,
    "accel": [0.604, -0.853,0.076],
    "gyro": [3.595, -3.885,-0.557],
    "mag": [-75, 195,390],
    "rpm": [0,0, 0, 0]
}
```

### GET /screenshot

With this endpoint you can retrieve the latest emitted frame and timestamp from the bot. The frame is a base64 encoded image. And the timestamp is the time when the frame was emitted (Unix Epoch UTC timestamp).
Inside the folder screenshots/ you can find the images.

This endpoint accepts a list of view types as a query parameter (view_types). Valid view types are rear, map, and front. If no view types are provided, it will return all three by default.

```bash
curl --location 'http://localhost:8000/screenshot?view_types=rear,map,front'
```

Example Response:

```JSON
{
    "front_video_frame": "base64_encoded_image",
    "rear_video_frame": "base64_encoded_image",
    "map_frame": "base64_encoded_image",
    "timestamp": 1724189733.208559
}
```

```bash
curl --location 'http://localhost:8000/screenshot?view_types=rear'
```

Example Response:

```JSON
{
    "rear_video_frame": "base64_encoded_image",
    "timestamp": 1724189733.208559
}
```

## Missions API

In order to start a mission you need to call the /start-mission endpoint. This endpoint will let you know if the bot is available or not for the mission.

To enable the missions API you need to set the MISSION_SLUG environment variable to the slug of the mission you want to start.

```bash
MISSION_SLUG=mission-1
```

If you just want to experiment with the bot without starting a mission you need to unset the MISSION_SLUG environment variable.
```bash
MISSION_SLUG=
```

`Note: Bots that are controlled by other players are not available for missions.`

### POST /start-mission
```bash
curl -X POST http://localhost:8000/start-mission
```

Example Response:
```JSON
{
    "message": "Mission started successfully"
}
```

### POST /checkpoints-list

With this endpoint you can retrieve the list of checkpoints for the mission. And the latest checkpoint that was scanned by the bot. If you scan the first checkpoint, the latest_scanned_checkpoint will be 1. If you scan the last checkpoint, the latest_scanned_checkpoint will be the highest sequence number and the mission will be completed.

```bash
curl --location 'http://localhost:8000/checkpoints-list'
```

Example Response:
```JSON
{
    "checkpoints_list": [
        {
            "id": 4818,
            "sequence": 1,
            "latitude": "30.48243713",
            "longitude": "114.3026428"
        },
        {
            "id": 4819,
            "sequence": 2,
            "latitude": "30.48268318",
            "longitude": "114.3026047"
        },
        {
            "id": 4820,
            "sequence": 3,
            "latitude": "30.48243713",
            "longitude": "114.3026428"
        }
    ],
    "latest_scanned_checkpoint": 0
}
```

### POST /checkpoint-reached

With this endpoint you can send the checkpoint that was scanned by the bot.

```bash
curl -X POST 'http://localhost:8000/checkpoint-reached' \
--header 'Content-Type: application/json' \
--data '{}'
```

Example Response:
```JSON
{
    "message": "Checkpoint reached successfully"
}
```

### POST /end-mission

With this endpoint you can force the mission to end in case you face some errors. Note that once you run this endpoint, the bot will be disconnected and will be available again for other players to use.

`⚠️  This endpoint should only be used in case of emergency. If you run this endpoint you will lose all your progress during the mission.`


```bash
curl --location 'http://localhost:8000/end-mission' \
--header 'Content-Type: application/json' \
--data '{}'
```

Example Response:
```JSON
{
    "message": "Mission ended successfully"
}
```


# Latest updates
- v.4.0: Added the ability to start a mission. Improved screenshots timings. Timestamps accuracy improved.
- v3.3: Improved control speed.
- v3.2: Added the ability to control the zoom level of the map.
- v3.1: Ability to retrieve rear camera frame and map screenshot. Bug fixes.

## Contributions

- [Michael Cho](mailto:michael.cho@frodobots.com)
- [Santiago Pravisani](mailto:santiago.pravisani@frodobots.com)
- [Esteban Fuhrmann](mailto:esteban.fuhrmann@frodobots.com)

## Join our Discord

- [Frodobots Discord](https://discord.com/invite/AUegJCJwyb)

