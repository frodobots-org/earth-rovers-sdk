import base64
import functools
import html
import json
import logging
import os
from datetime import datetime

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from browser_service import BrowserService
from rtm_client import RtmClient

load_dotenv()

# Configurar el logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("http_logger")

app = FastAPI()


# Middleware para registrar solicitudes y respuestas
def log_request(method):
    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        debug_mode = os.getenv("DEBUG") == "true"  # Verifica si DEBUG está en true
        if debug_mode:
            params = kwargs.get("params", {})
            json_data = kwargs.get("json", {})
            data = kwargs.get("data", {})
            logger.info(
                "=== External Request ===\nMethod: %s\nURL: %s\nParams: %s\nJSON: %s\nData: %s",
                method.__name__.upper(),
                args[0],
                params,
                json_data,
                data,
            )

        response = method(*args, **kwargs)

        if debug_mode:
            logger.info(
                "=== External Response ===\nStatus Code: %s\nResponse: %s",
                response.status_code,
                response.text,
            )

        return response

    return wrapper


requests.get = log_request(requests.get)
requests.post = log_request(requests.post)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRODOBOTS_API_URL = os.getenv(
    "FRODOBOTS_API_URL", "https://frodobots-web-api.onrender.com/api/v1"
)


class AuthResponse(BaseModel):
    CHANNEL_NAME: str
    RTC_TOKEN: str
    RTM_TOKEN: str
    USERID: int
    APP_ID: str


# In-memory storage for the response
auth_response_data = {}
checkpoints_list_data = {}

app.mount("/static", StaticFiles(directory="./static"), name="static")

browser_service = BrowserService()


async def auth_common():
    global auth_response_data
    channel_name = os.getenv("CHANNEL_NAME")
    rtc_token = os.getenv("RTC_TOKEN")
    rtm_token = os.getenv("RTM_TOKEN")
    userid = os.getenv("USERID")
    app_id = os.getenv("APP_ID")

    if all([channel_name, rtc_token, rtm_token, userid, app_id]):
        auth_response_data = {
            "CHANNEL_NAME": channel_name,
            "RTC_TOKEN": rtc_token,
            "RTM_TOKEN": rtm_token,
            "USERID": userid,
            "APP_ID": app_id,
        }
        return auth_response_data
    else:
        auth_header = os.getenv("SDK_API_TOKEN")
        bot_slug = os.getenv("BOT_SLUG")

        if not auth_header:
            raise HTTPException(
                status_code=500, detail="Authorization header not configured"
            )
        if not bot_slug:
            raise HTTPException(status_code=500, detail="Bot name not configured")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth_header}",
        }

        data = {"bot_slug": bot_slug}

        response = requests.post(
            FRODOBOTS_API_URL + "/sdk/token", headers=headers, json=data, timeout=15
        )

        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code, detail="Failed to retrieve tokens"
            )

        response_data = response.json()
        auth_response_data = {
            "CHANNEL_NAME": response_data.get("CHANNEL_NAME"),
            "RTC_TOKEN": response_data.get("RTC_TOKEN"),
            "RTM_TOKEN": response_data.get("RTM_TOKEN"),
            "USERID": response_data.get("USERID"),
            "APP_ID": response_data.get("APP_ID"),
        }
        return auth_response_data


@app.get("/checkpoints")
async def checkpoints():
    await get_checkpoints_list()
    return JSONResponse(content=checkpoints_list_data)


async def get_checkpoints_list():
    global checkpoints_list_data
    auth_header = os.getenv("SDK_API_TOKEN")
    bot_slug = os.getenv("BOT_SLUG")
    mission_slug = os.getenv("MISSION_SLUG")

    if not mission_slug:
        return

    if not auth_header:
        raise HTTPException(
            status_code=500, detail="Authorization header not configured"
        )
    if not bot_slug:
        raise HTTPException(status_code=500, detail="Bot name not configured")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_header}",
    }

    data = {"bot_slug": bot_slug, "mission_slug": mission_slug}

    response = requests.post(
        FRODOBOTS_API_URL + "/sdk/checkpoints_list",
        headers=headers,
        json=data,
        timeout=15,
    )

    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail="Failed to retrieve checkpoints list",
        )

    checkpoints_list_data = response.json()
    return checkpoints_list_data


@app.post("/auth")
async def auth():
    await auth_common()
    if not checkpoints_list_data:
        await get_checkpoints_list()
    return JSONResponse(
        content={
            "auth_response_data": auth_response_data,
            "checkpoints_list_data": checkpoints_list_data,
        }
    )


@app.get("/")
async def get_index(request: Request):
    if not auth_response_data:
        await auth()

    app_id = auth_response_data.get("APP_ID", "")
    rtc_token = auth_response_data.get("RTC_TOKEN", "")
    rtm_token = auth_response_data.get("RTM_TOKEN", "")
    channel = auth_response_data.get("CHANNEL_NAME", "")
    uid = auth_response_data.get("USERID", "")
    checkpoints_list = json.dumps(checkpoints_list_data.get("checkpoints_list", []))
    with open("index.html", "r", encoding="utf-8") as file:
        html_content = file.read()

    html_content = html_content.replace("{{ appid }}", app_id)
    html_content = html_content.replace("{{ rtc_token }}", rtc_token)
    html_content = html_content.replace("{{ rtm_token }}", rtm_token)
    html_content = html_content.replace("{{ channel }}", channel)
    html_content = html_content.replace("{{ uid }}", str(uid))
    html_content = html_content.replace("{{ checkpoints_list }}", checkpoints_list)

    return HTMLResponse(content=html_content, status_code=200)


@app.post("/control")
async def control(request: Request):
    if not auth_response_data:
        await auth()

    body = await request.json()
    command = body.get("command")
    if not command:
        raise HTTPException(status_code=400, detail="Command not provided")

    RtmClient(auth_response_data).send_message(command)

    return {"message": "Command sent successfully"}


@app.get("/screenshot")
async def get_screenshot():
    print("Received request for screenshot")
    front_video_output_path, rear_video_output_path, map_output_path = (
        await browser_service.take_screenshot("screenshot", "map.png")
    )
    print(
        f"Screenshot saved to {front_video_output_path}, {rear_video_output_path}, and {map_output_path}"
    )

    # Read the image files and encode them in base64
    with open(front_video_output_path, "rb") as front_video_file:
        encoded_front_video = base64.b64encode(front_video_file.read()).decode("utf-8")

    with open(rear_video_output_path, "rb") as rear_video_file:
        encoded_rear_video = base64.b64encode(rear_video_file.read()).decode("utf-8")

    with open(map_output_path, "rb") as map_file:
        encoded_map = base64.b64encode(map_file.read()).decode("utf-8")

    # Get the current Unix UTC timestamp (epoch time)
    current_timestamp = int(datetime.utcnow().timestamp())

    # Return JSON response with base64 images and timestamp
    return JSONResponse(
        content={
            "front_video_frame": encoded_front_video,
            "rear_video_frame": encoded_rear_video,
            "map_frame": encoded_map,
            "timestamp": current_timestamp,
        }
    )


@app.get("/data")
async def get_data():
    data = await browser_service.data()
    return JSONResponse(content=data)


if __name__ == "__main__":
    from hypercorn.config import Config

    config = Config()
    config.bind = ["0.0.0.0:8000"]
