# Earth Rovers SDK v3.1

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
    "vibration": 0.31
}
```

### GET /screenshot

With this endpoint you can retrieve the latest emitted frame and timestamp from the bot. The frame is a base64 encoded image. And the timestamp is the time when the frame was emitted (Unix Epoch UTC timestamp).
Inside the folder screenshots/ you can find the images.

```bash
curl --location 'http://localhost:8000/screenshot'
```

Example Response:

```JSON
{
    "front_video_frame": "base64_encoded_image",
    "rear_video_frame": "base64_encoded_image",
    "map_frame": "base64_encoded_image",
    "timestamp": 1720458328
}
```

