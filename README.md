# Earth Rovers SDK v2.0

## Requirements
In order to use or run this SDK you need to have an account registered with Frodobots. This is meant for research purposes, if you are interested please reach us here: [Frodobots Discord](https://discord.com/invite/AUegJCJwyb)

- Python 3.6 or higher
- Frodobots API key

## Getting Started

1. Write once your .env variables provided by Frodobots team your SDK API key and the name of the bot you've got.

```bash
SDK_API_TOKEN=
BOT_NAME=
```

2. Install the SDK
```bash
pip3 install -r requirements.txt
```

3. Run the SDK
```bash
uvicorn main:app --reload
```

4. Now you can check the live streaming of the bot in the following URL: http://localhost:8000

## Documentation

This SDK is meant to control the bot and at the same time monitor its status. The SDK has the following open endpoints:

### /control

With this endpoint you can send linear and angular values to move the bot. The values are between -1 and 1.

```bash
curl --location 'http://localhost:8000/control' \
--header 'Content-Type: application/json' \
--data '{
    "command": { "linear": 1, "angular": 1 }
}'
```

### /data

With this endpoint you can retrieve the data of the bot.

[TBD]

### /screenshot

With this endpoint you can retrieve the latest emitted frame from the bot.

[TBD]
