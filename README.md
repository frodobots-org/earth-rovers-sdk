<p align="center">
  <img src="https://cdn.prod.website-files.com/66042185882fa3428f4dd6f1/662bee5b5ef7ed094186a56a_frodobots_ai_logo-p-500.png" alt="Earth Rovers SDK Logo" width="140">
  <h3 align="center">Frodobots AI</h3>
  <br>
</p>

# Earth Rovers SDK v5.0

## Requirements

1. Acquire one of our Earth Rovers in here: [Earth Rovers Shop](https://shop.frodobots.com/).

2. Complete your Bot activation.

3. After completing your bot activation. Get your SDK Access token in [here](https://my.frodobots.com/owner/settings).

## Software Requirements

- Python 3.9 or higher
- Frodobots API key
- Google Chrome 143+ (or any modern browser) installed

## Hardware Specs

<div style="display: flex; flex-direction: row; justify-content: center; align-items: center; gap: 20px;">
  <img src="assets/v5.2.png" alt="Hardware Specifications" width="200">
  <img src="assets/axis.jpg" alt="Axis Camera" width="200">
</div>

For full details on the hardware specifications, please refer to the [Frodobots Hardware Specifications](https://docs.google.com/document/d/1Px-rNy0wQeG74mWcReiV4dEk5u4nfMPTVh-C4pXoieY).

More details about the bot sensors and actuators can be found [here](https://colab.research.google.com/#fileId=https%3A//huggingface.co/datasets/frodobots/FrodoBots-2K/blob/main/helpercode.ipynb).

## Getting Started

1. Write once your .env variables provided by Frodobots team your SDK API key and the name of the bot you've got.

```bash
SDK_API_TOKEN=
BOT_SLUG=
CHROME_EXECUTABLE_PATH=
# Default value is MAP_ZOOM_LEVEL=18 https://wiki.openstreetmap.org/wiki/Zoom_levels
MAP_ZOOM_LEVEL=
MISSION_SLUG=
# Image quality between 0.1 and 1.0 (default: 0.8)
# Recommended: 0.8 for better performance
IMAGE_QUALITY=0.8
# Image format: jpeg, png or webp (default: png)
# Recommended: jpeg for better performance and lower bandwidth usage
IMAGE_FORMAT=jpeg
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

With this endpoint you can send linear and angular values to move the bot, and control the lamp. The linear and angular values are between -1 and 1. The lamp value is 0 (off) or 1 (on).

```bash
curl --location 'http://localhost:8000/control' \
--header 'Content-Type: application/json' \
--data '{
    "command": { "linear": 1, "angular": 1, "lamp": 0 }
}'
```

**Parameters:**

- `linear`: Movement speed forward/backward (-1 to 1)
- `angular`: Rotation speed left/right (-1 to 1)
- `lamp`: Lamp control (0 = off, 1 = on)

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
    "accels": [
        [0.998,0.003,0.005,1725434620.858],
        [1,0.002,0.005,1725434620.964],
        [1,0.002,0.005,1725434620.964],
        [1,0.003,0.004,1725434621.079],
        [0.997,0.003,0.008,1725434621.192],
        [0.998,0.003,0.002,1725434621.294]
    ],
    "gyros": [
        [0.521,0.023,0.716,1725434620.913],
        [0.552,0.023,0.732,1725434621.02],
        [0.483,0.015,0.732,1725434621.122],
        [0.407,-0.007,0.747,1725434621.239],
        [0.453,0.061,0.724,1725434621.343]
    ],
    "mags": [
        [-1002,967,12,1725434621.194]
    ],

    "rpms": [
        [0,0,0,0,1725434567.194],
        [0,0,0,0,1725434567.218],
        [0,0,0,0,1725434597.682],
        [0,0,0,0,1725434597.701],
        [0,0,0,0,1725434597.726]
    ],
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
    "front_frame": "base64_encoded_image",
    "rear_frame": "base64_encoded_image",
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

### GET /v2/screenshot

With this endpoint you can retrieve the latest emitted frame and timestamp from the bot. The frame is a base64 encoded image. And the timestamp is the time when the frame was emitted (Unix Epoch UTC timestamp).

This endpoint retrieves the latest emitted frame (both front and rear) and the corresponding timestamp. The frame is provided as a base64 encoded image, and the timestamp is given in Unix Epoch format (Unix Epoch UTC timestamp).

Unlike the standard screenshot method, this version returns frames 15 times faster and always includes both the front and rear frames.

You can parametrize the image quality between 0.1 and 1.0, and the format between jpeg, png and webp, using the IMAGE_QUALITY and IMAGE_FORMAT environment variables.

```bash
curl --location 'http://localhost:8000/v2/screenshot'
```

Example Response:

```JSON
{
    "front_frame": "base64_encoded_image",
    "rear_frame": "base64_encoded_image",
    "timestamp": 1724189733.208559
}
```

### GET /v2/front

This endpoint allows you to retrieve the latest frame emitted from the bot's front camera. The frame is provided as a base64 encoded image.

You can parametrize the image quality between 0.1 and 1.0, and the format between jpeg, png and webp, using the IMAGE_QUALITY and IMAGE_FORMAT environment variables.

```bash
curl --location 'http://localhost:8000/v2/front'
```

Example Response:

```JSON
{
    "front_frame": "base64_encoded_image",
    "timestamp": 1724189733.208559
}
```

### GET /v2/rear

This endpoint allows you to retrieve the latest frame emitted from the bot's rear camera. The frame is provided as a base64 encoded image.

You can parametrize the image quality between 0.1 and 1.0, and the format between jpeg, png and webp, using the IMAGE_QUALITY and IMAGE_FORMAT environment variables.

```bash
curl --location 'http://localhost:8000/v2/rear'
```

Example Response:

```JSON
{
    "rear_frame": "base64_encoded_image",
    "timestamp": 1724189733.208559
}
```

## Missions API

In order to start a mission you need to call the /start-mission endpoint. This endpoint will let you know if the bot is available or not for the mission.

To enable the missions API you need to set the MISSION_SLUG environment variable to the slug of the mission you want to start.

```bash
MISSION_SLUG=mission-1
```

If you just want to experiment with the bot without starting a mission you need to remove the MISSION_SLUG environment variable.

`Note: Bots that are controlled by other players are not available for missions.`

### POST /start-mission

```bash
curl --location --request POST 'http://localhost:8000/start-mission'
```

Successful Response (Code: 200)

```JSON
{
    "message": "Mission started successfully"
}
```

Unsuccessful Response (Code: 400)

```JSON
{
    "detail": "Bot unavailable for SDK"
}
```

### POST /checkpoints-list

### GET /checkpoints-list

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

Successful Response (Code: 200)

```JSON
{
    "message": "Checkpoint reached successfully",
    "next_checkpoint_sequence": 2
}
```

Unsuccessful Response (Code: 400)

```JSON
{
    "detail": {
        "error": "Bot is not within XX meters from the checkpoint",
        "proximate_distance_to_checkpoint": 16.87
    }
}
```

### POST /end-mission

With this endpoint you can force the mission to end in case you face some errors. Note that once you run this endpoint, the bot will be disconnected and will be available again for other players to use.

In case you get stucked and don't want to lose your progress, you can use the /start-mission endpoint to refresh it.

`⚠️  This endpoint should only be used in case of emergency. If you run this endpoint you will lose all your progress during the mission.`

```bash
curl --location --request POST 'http://localhost:8000/end-mission'
```

Example Response:

```JSON
{
    "message": "Mission ended successfully"
}
```

### GET /missions-history

With this endpoint you can retrieve the missions history of the bot you've been riding.

```bash
curl --location 'http://localhost:8000/missions-history'
```

Example Response:

```JSON
{
    "mission_rides": [
        {
            "id": 86855,
            "mission_slug": "mission-1",
            "success": true,
            "latest_scanned_checkpoint": 3,
            "status": "active",
            "start_time": "2024-09-02T07:38:46.755Z",
            "end_time": "2024-09-02T07:45:46.755Z"
        },
        // ...
    ]
}
```

## Interventions API

The Interventions API allows you to manage interventions during bot rides. An intervention represents a period where the bot requires special attention or handling.

### POST /interventions/start

Start a new intervention for the current bot ride. The bot's current position (latitude and longitude) will be automatically recorded.

```bash
curl -X POST 'http://localhost:8000/interventions/start'
```

Successful Response (Code: 200)

```JSON
{
    "message": "Intervention started successfully",
    "intervention_id": "123e4567-e89b-12d3-a456-426614174000"
}
```

### POST /interventions/end

End an active intervention for the current bot ride. The bot's current position (latitude and longitude) will be automatically recorded.

```bash
curl -X POST 'http://localhost:8000/interventions/end'
```

Successful Response (Code: 200)

```JSON
{
    "message": "Intervention ended successfully"
}
```

Unsuccessful Response (Code: 400)

```JSON
{
    "detail": "No active intervention found"
}
```

### GET /interventions/history

Retrieve the history of interventions for the current bot.

```bash
curl --location 'http://localhost:8000/interventions/history'
```

Example Response:

```JSON
{
    "interventions": [
        {
            "ride_id": "123",
            "start_time": "2024-01-01T12:00:00Z",
            "end_time": "2024-01-01T12:30:00Z",
            "mission_name": "Mission 1",
            "mission_slug": "mission-1",
            "bot_name": "Bot 1",
            "bot_slug": "bot-1"
        }
    ]
}
```

# Latest updates

- v.5.0:

  - Updated video streaming SDK for Chrome 143+ compatibility
  - Updated real-time messaging SDK to latest stable version
  - Fixed video subscription errors during stream initialization
  - Added subscription queue to prevent race conditions
  - Improved error handling for video stream subscriptions

- v.4.9:

  - Added Interventions API with endpoints for starting, ending and retrieving intervention history
  - New endpoints: /interventions/start, /interventions/end, /interventions/history
  - Added timestamp to /v2/front and /v2/rear endpoints

- v.4.8:
  - Added compatibility for mini and zero bots
  - Added HTML examples for bot control and video streaming (20 FPS)

<div style="display: flex; flex-direction: row; justify-content: center; align-items: center; gap: 20px;">
  <img src="screenshots/zero.jpg" alt="Zero Bot" width="900">
  <img src="screenshots/mini.jpg" alt="Mini Bot" width="900">
</div>

- v.4.7:
  - Optimized frame capture system to reduce CPU and memory usage
  - Removed continuous frame capture loop, now frames are captured on-demand
  - Improved resource management for video streaming
  - Better handling of system resources during long-running sessions
- v.4.6: Added image quality and format configuration options for better performance
- v.4.5: Minor Bugfixes.
- v.4.4: Minor Bugfixes. Spectate Rides.
- v.4.3: Missions history and more information on checkpoint reached. Improved /data RTM messages
- v.4.2: Updated Readme.md
- v.4.1: End mission.
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

