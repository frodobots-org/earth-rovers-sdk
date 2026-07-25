import base64
import functools
import json
import logging
import os
from datetime import datetime
import asyncio

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Literal

from browser_service import BrowserService
from rtm_client import RtmClient
from tts_service import generate_speech

load_dotenv()

# Configurar el logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("http_logger")

app = FastAPI()


# Middleware
def log_request(method):
    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        debug_mode = os.getenv("DEBUG") == "true"
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
    BOT_UID: str


# In-memory storage for the response
auth_response_data = {}
checkpoints_list_data = {}

app.mount("/static", StaticFiles(directory="./static"), name="static")

browser_service = BrowserService()


async def auth_common():
    global auth_response_data
    auth_response_data = get_env_tokens()

    if auth_response_data:
        return auth_response_data

    auth_header = os.getenv("SDK_API_TOKEN")
    bot_slug = os.getenv("BOT_SLUG")
    mission_slug = os.getenv("MISSION_SLUG")

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

    if mission_slug:
        response_data = await start_ride(headers, bot_slug, mission_slug)
    else:
        response_data = await retrieve_tokens(headers, bot_slug)

    auth_response_data = {
        "CHANNEL_NAME": response_data.get("CHANNEL_NAME"),
        "RTC_TOKEN": response_data.get("RTC_TOKEN"),
        "RTM_TOKEN": response_data.get("RTM_TOKEN"),
        "USERID": response_data.get("USERID"),
        "APP_ID": response_data.get("APP_ID"),
        "BOT_UID": response_data.get("BOT_UID"),
        "SPECTATOR_USERID": response_data.get("SPECTATOR_USERID"),
        "SPECTATOR_RTC_TOKEN": response_data.get("SPECTATOR_RTC_TOKEN"),
        "BOT_TYPE": response_data.get("BOT_TYPE", "mini"),
    }

    return auth_response_data


def get_env_tokens():
    channel_name = os.getenv("CHANNEL_NAME")
    rtc_token = os.getenv("RTC_TOKEN")
    rtm_token = os.getenv("RTM_TOKEN")
    userid = os.getenv("USERID")
    app_id = os.getenv("APP_ID")
    bot_uid = os.getenv("BOT_UID")

    if all([channel_name, rtc_token, rtm_token, userid, app_id, bot_uid]):
        return {
            "CHANNEL_NAME": channel_name,
            "RTC_TOKEN": rtc_token,
            "RTM_TOKEN": rtm_token,
            "USERID": userid,
            "APP_ID": app_id,
            "BOT_UID": bot_uid,
        }
    return None


async def start_ride(headers, bot_slug, mission_slug):
    start_ride_data = {"bot_slug": bot_slug, "mission_slug": mission_slug}
    start_ride_response = requests.post(
        FRODOBOTS_API_URL + "/sdk/start_ride",
        headers=headers,
        json=start_ride_data,
        timeout=15,
    )

    if start_ride_response.status_code != 200:
        raise HTTPException(
            status_code=start_ride_response.status_code,
            detail="Bot unavailable for SDK",
        )

    return start_ride_response.json()


async def end_ride(headers, bot_slug, mission_slug):
    end_ride_data = {"bot_slug": bot_slug, "mission_slug": mission_slug}
    end_ride_response = requests.post(
        FRODOBOTS_API_URL + "/sdk/end_ride",
        headers=headers,
        json=end_ride_data,
        timeout=15,
    )

    if end_ride_response.status_code != 200:
        raise HTTPException(
            status_code=end_ride_response.status_code, detail="Failed to end mission"
        )

    return end_ride_response.json()


async def retrieve_tokens(headers, bot_slug):
    data = {"bot_slug": bot_slug}
    response = requests.post(
        FRODOBOTS_API_URL + "/sdk/token", headers=headers, json=data, timeout=15
    )

    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code, detail="Failed to retrieve tokens"
        )

    return response.json()


async def need_start_mission():
    if not os.getenv("MISSION_SLUG"):
        return
    if auth_response_data:
        return
    raise HTTPException(
        status_code=400, detail="Call /start-mission endpoint to start a mission"
    )


@app.post("/checkpoints-list")
@app.get("/checkpoints-list")
async def checkpoints():
    await need_start_mission()
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


@app.post("/start-mission")
async def start_mission():
    required_env_vars = ["SDK_API_TOKEN", "BOT_SLUG", "MISSION_SLUG"]
    missing_vars = [var for var in required_env_vars if not os.getenv(var)]

    if missing_vars:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required environment variables: {', '.join(missing_vars)}",
        )

    if not auth_response_data:
        await auth()
    if not checkpoints_list_data:
        await get_checkpoints_list()
    return JSONResponse(
        status_code=200,
        content={
            "message": "Mission started successfully",
            "checkpoints_list": checkpoints_list_data,
        },
    )


@app.post("/end-mission")
async def end_mission():
    required_env_vars = ["SDK_API_TOKEN", "BOT_SLUG", "MISSION_SLUG"]
    missing_vars = [var for var in required_env_vars if not os.getenv(var)]

    if missing_vars:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required environment variables: {', '.join(missing_vars)}",
        )

    auth_header = os.getenv("SDK_API_TOKEN")
    bot_slug = os.getenv("BOT_SLUG")
    mission_slug = os.getenv("MISSION_SLUG")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_header}",
    }

    try:
        end_ride_response = await end_ride(headers, bot_slug, mission_slug)
        # Clear the stored auth and checkpoints data
        global auth_response_data, checkpoints_list_data
        auth_response_data = {}
        checkpoints_list_data = {}
        return JSONResponse(content={"message": "Mission ended successfully"})
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to end mission: {str(e)}")


async def render_index_html(is_spectator: bool):
    await need_start_mission()
    if not auth_response_data:
        await auth()

    token_type: Literal["SPECTATOR_", ""] = "SPECTATOR_" if is_spectator else ""

    template_vars = {
        "appid": auth_response_data.get("APP_ID", ""),
        "rtc_token": auth_response_data.get(f"{token_type}RTC_TOKEN", ""),
        "rtm_token": "" if is_spectator else auth_response_data.get("RTM_TOKEN", ""),
        "channel": auth_response_data.get("CHANNEL_NAME", ""),
        "uid": auth_response_data.get(f"{token_type}USERID", ""),
        "bot_uid": auth_response_data.get("BOT_UID", ""),
        "checkpoints_list": json.dumps(
            checkpoints_list_data.get("checkpoints_list", [])
        ),
        "map_zoom_level": os.getenv("MAP_ZOOM_LEVEL", "18"),
    }

    with open("index.html", "r", encoding="utf-8") as file:
        html_content = file.read()

    for key, value in template_vars.items():
        html_content = html_content.replace(f"{{{{ {key} }}}}", str(value))

    return HTMLResponse(content=html_content, status_code=200)


@app.get("/")
async def get_index(request: Request):
    return await render_index_html(is_spectator=True)


@app.get("/sdk")
async def sdk(request: Request):
    return await render_index_html(is_spectator=False)


@app.post("/control-legacy")
async def control_legacy(request: Request):
    await need_start_mission()
    if not auth_response_data:
        await auth()

    body = await request.json()
    command = body.get("command")
    if not command:
        raise HTTPException(status_code=400, detail="Command not provided")

    RtmClient(auth_response_data).send_message(command)

    return {"message": "Command sent successfully"}


@app.post("/control")
async def control(request: Request):
    await need_start_mission()
    if not auth_response_data:
        await auth()

    body = await request.json()
    command = body.get("command")
    if not command:
        raise HTTPException(status_code=400, detail="Command not provided")

    try:
        await browser_service.send_message(command)
        return {"message": "Command sent successfully"}
    except Exception as e:
        logger.error("Error sending control command: %s", str(e))
        raise HTTPException(
            status_code=500, detail="Failed to send control command"
        ) from e


@app.post("/speak")
async def speak(request: Request):
    await need_start_mission()
    if not auth_response_data:
        await auth()

    body = await request.json()
    text = body.get("text")
    if not text:
        raise HTTPException(status_code=400, detail="Text not provided")

    try:
        audio_path = await generate_speech(text, "static/tts_output")
        audio_filename = os.path.basename(audio_path)
        audio_url = f"http://127.0.0.1:8000/static/{audio_filename}"
        await browser_service.speak(audio_url)
        return {"message": "Speech sent to rover"}
    except Exception as e:
        logger.error("Error in /speak: %s", str(e))
        raise HTTPException(status_code=500, detail=f"TTS failed: {str(e)}") from e


@app.get("/screenshot")
async def get_screenshot(view_types: str = "rear,map,front"):
    await need_start_mission()
    if not auth_response_data:
        await auth()

    print("Received request for screenshot with view_types:", view_types)
    valid_views = {"rear", "map", "front"}
    views_list = view_types.split(",")

    for view in views_list:
        if view not in valid_views:
            raise HTTPException(status_code=400, detail=f"Invalid view type: {view}")

    await browser_service.take_screenshot("screenshots", views_list)

    response_content = {}
    for view in views_list:
        file_path = f"screenshots/{view}.png"
        try:
            with open(file_path, "rb") as image_file:
                encoded_image = base64.b64encode(image_file.read()).decode("utf-8")
                response_content[f"{view}_frame"] = encoded_image
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to read {view} image"
            ) from exc

    current_timestamp = datetime.utcnow().timestamp()
    response_content["timestamp"] = current_timestamp

    return JSONResponse(content=response_content)


@app.get("/data")
async def get_data():
    await need_start_mission()
    data = await browser_service.data()
    return JSONResponse(content=data)


@app.post("/checkpoint-reached")
async def checkpoint_reached(request: Request):
    await need_start_mission()

    bot_slug = os.getenv("BOT_SLUG")
    mission_slug = os.getenv("MISSION_SLUG")
    auth_header = os.getenv("SDK_API_TOKEN")

    if not all([bot_slug, mission_slug, auth_header]):
        raise HTTPException(
            status_code=500, detail="Required environment variables not configured"
        )

    data = await browser_service.data()
    latitude = data.get("latitude")
    longitude = data.get("longitude")

    if not all([latitude, longitude]):
        raise HTTPException(status_code=400, detail="Missing latitude or longitude")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_header}",
    }

    payload = {
        "bot_slug": bot_slug,
        "mission_slug": mission_slug,
        "latitude": latitude,
        "longitude": longitude,
    }

    response = requests.post(
        FRODOBOTS_API_URL + "/sdk/checkpoint_reached",
        headers=headers,
        json=payload,
        timeout=15,
    )

    response_data = response.json()

    if response.status_code != 200:
        raise HTTPException(
            status_code=response.status_code,
            detail={
                "error": response_data.get("error", "Failed to send checkpoint data"),
                "proximate_distance_to_checkpoint": response_data.get(
                    "distance_to_checkpoint", "Unknown"
                ),
            },
        )
    return JSONResponse(
        status_code=200,
        content={
            "message": "Checkpoint reached successfully",
            "next_checkpoint_sequence": response_data.get(
                "next_checkpoint_sequence", ""
            ),
        },
    )


@app.get("/missions-history")
async def missions_history():
    await auth_common()

    auth_header = os.getenv("SDK_API_TOKEN")
    bot_slug = os.getenv("BOT_SLUG")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_header}",
    }

    data = {"bot_slug": bot_slug}

    try:
        response = requests.post(
            FRODOBOTS_API_URL + "/sdk/rides_history",
            headers=headers,
            json=data,
            timeout=15,
        )

        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail="Failed to retrieve missions history",
            )

        return JSONResponse(content=response.json())
    except requests.RequestException as e:
        raise HTTPException(
            status_code=500, detail=f"Error fetching missions history: {str(e)}"
        )


@app.get("/missions")
async def missions():
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

    payload = {"bot_slug": bot_slug}

    try:
        response = requests.get(
            FRODOBOTS_API_URL + "/sdk/missions",
            headers=headers,
            params=payload,
            timeout=15,
        )

        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail="Failed to retrieve missions",
            )

        missions_list = [
            {
                "slug": mission.get("slug"),
                "distance_in_m": mission.get("distance_in_m"),
                "checkpoints_count": mission.get("checkpoints_count"),
            }
            for mission in response.json().get("missions", [])
        ]

        return JSONResponse(content={"missions": missions_list})
    except requests.RequestException as e:
        raise HTTPException(
            status_code=500, detail=f"Error fetching missions: {str(e)}"
        )


@app.get("/v2/screenshot")
async def get_screenshot_v2():
    await need_start_mission()
    if not auth_response_data:
        await auth()

    async def get_frame(frame_type):
        frame = await getattr(browser_service, frame_type)()
        _, frame = frame.split(",", 1)
        return {f"{frame_type}_frame": frame}

    front_task = asyncio.create_task(get_frame("front"))
    tasks = [front_task]

    if auth_response_data.get("BOT_TYPE") == "zero":
        rear_task = asyncio.create_task(get_frame("rear"))
        tasks.append(rear_task)

    results = await asyncio.gather(*tasks)

    response_data = {}
    for result in results:
        response_data.update(result)

    if not response_data:
        raise HTTPException(status_code=404, detail="Frames not available")

    response_data["timestamp"] = datetime.utcnow().timestamp()

    return JSONResponse(content=response_data)


if __name__ == "__main__":
    from hypercorn.config import Config

    config = Config()
    config.bind = ["0.0.0.0:8000"]


@app.get("/v2/front")
async def get_front_frame():
    await need_start_mission()
    front_frame = await browser_service.front()
    response_data = {}
    if front_frame:
        _, base64_data = front_frame.split(",", 1)
        response_data["front_frame"] = base64_data
        response_data["timestamp"] = datetime.utcnow().timestamp()
        return JSONResponse(content=response_data)
    else:
        raise HTTPException(status_code=404, detail="Front frame not available")


@app.get("/v2/rear")
async def get_rear_frame():
    await need_start_mission()
    if not auth_response_data:
        await auth()

    rear_frame = await browser_service.rear()
    response_data = {}
    if rear_frame:
        _, base64_data = rear_frame.split(",", 1)
        response_data["rear_frame"] = base64_data
        response_data["timestamp"] = datetime.utcnow().timestamp()
        return JSONResponse(content=response_data)
    else:
        raise HTTPException(status_code=404, detail="Rear frame not available")


@app.post("/interventions/start")
async def start_intervention(request: Request):
    await need_start_mission()

    auth_header = os.getenv("SDK_API_TOKEN")
    bot_slug = os.getenv("BOT_SLUG")

    if not auth_header:
        raise HTTPException(
            status_code=500, detail="Authorization header not configured"
        )
    if not bot_slug:
        raise HTTPException(status_code=500, detail="Bot name not configured")

    data = await browser_service.data()
    latitude = data.get("latitude")
    longitude = data.get("longitude")

    if not all([latitude, longitude]):
        raise HTTPException(status_code=400, detail="Missing latitude or longitude")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_header}",
    }

    payload = {
        "bot_slug": bot_slug,
        "latitude": latitude,
        "longitude": longitude,
    }

    try:
        response = requests.post(
            FRODOBOTS_API_URL + "/sdk/interventions/start",
            headers=headers,
            json=payload,
            timeout=15,
        )

        response_data = response.json()

        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=response_data.get("error", "Failed to start intervention"),
            )

        return JSONResponse(
            status_code=200,
            content={
                "message": "Intervention started successfully",
                "intervention_id": response_data.get("intervention_id"),
            },
        )
    except requests.RequestException as e:
        raise HTTPException(
            status_code=500, detail=f"Error starting intervention: {str(e)}"
        )


@app.post("/interventions/end")
async def end_intervention(request: Request):
    await need_start_mission()

    auth_header = os.getenv("SDK_API_TOKEN")
    bot_slug = os.getenv("BOT_SLUG")

    if not auth_header:
        raise HTTPException(
            status_code=500, detail="Authorization header not configured"
        )
    if not bot_slug:
        raise HTTPException(status_code=500, detail="Bot name not configured")

    data = await browser_service.data()
    latitude = data.get("latitude")
    longitude = data.get("longitude")

    if not all([latitude, longitude]):
        raise HTTPException(status_code=400, detail="Missing latitude or longitude")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_header}",
    }

    payload = {
        "bot_slug": bot_slug,
        "latitude": latitude,
        "longitude": longitude,
    }

    try:
        response = requests.post(
            FRODOBOTS_API_URL + "/sdk/interventions/end",
            headers=headers,
            json=payload,
            timeout=15,
        )

        response_data = response.json()

        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail=response_data.get("error", "Failed to end intervention"),
            )

        return JSONResponse(
            status_code=200,
            content={"message": "Intervention ended successfully"},
        )
    except requests.RequestException as e:
        raise HTTPException(
            status_code=500, detail=f"Error ending intervention: {str(e)}"
        )


@app.get("/interventions/history")
async def interventions_history():
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

    payload = {"bot_slug": bot_slug}

    try:
        response = requests.get(
            FRODOBOTS_API_URL + "/sdk/interventions/history",
            headers=headers,
            params=payload,
            timeout=15,
        )

        if response.status_code != 200:
            raise HTTPException(
                status_code=response.status_code,
                detail="Failed to retrieve interventions history",
            )

        return JSONResponse(content=response.json())
    except requests.RequestException as e:
        raise HTTPException(
            status_code=500, detail=f"Error fetching interventions history: {str(e)}"
        )


# =============================================================================
# autonav-urban endpoints (GENIE-SAMTP). See PLAN_AUTONAV_URBAN.md.
# Everything below this line can be safely removed to revert to stock SDK.
# =============================================================================

import sys as _sys
from pathlib import Path as _Path

_AUTONAV_URBAN_ROOT = _Path(__file__).resolve().parent / "autonav-urban"
if _AUTONAV_URBAN_ROOT.is_dir() and str(_AUTONAV_URBAN_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_AUTONAV_URBAN_ROOT))


_urban_runtime = None
_urban_start_lock = asyncio.Lock()

# ------------------------------------------------------------------ warm boot
#
# Goal: by the time the user clicks "Start" on the dashboard, the browser is
# already open, RTM is joined, the front camera is streaming, and SAM-TP's
# MPS kernels are already compiled. Start then only has to spin up the
# perception / planning / control loops — rover moves within ~1 second.
#
# The warmup is started as a fire-and-forget task from FastAPI's @on_event
# "startup" handler so it never blocks server boot (uvicorn still binds the
# port immediately). Status is exposed via /autonav-urban/status so the
# dashboard can show a "warming up..." banner.
#
# When /autonav-urban/start eventually runs, it hands the pre-loaded SAM-TP
# model into build_runtime() so the runtime skips the ~5-8 s model load.

_urban_warmup: dict = {
    "task": None,               # asyncio.Task | None (the warmup task itself)
    "idle_task": None,          # asyncio.Task | None (the idle telemetry poller)
    "browser_ready": False,
    "samtp_ready": False,
    "clipseg_ready": False,
    "clipseg": None,               # CLIPSegModel | None
    "samtp": None,              # SAMTPModel | None
    "started_at": None,         # float | None
    "finished_at": None,        # float | None
    "last_error": None,         # str | None
    "last_telemetry": None,     # dict | None (raw /data payload cached at 1 Hz)
    "last_telemetry_ts": 0.0,
    "last_telemetry_rover_ts": 0.0,
}


async def _urban_idle_telemetry_loop():
    """Poll /data at 1 Hz while runtime is idle so dashboard has live values.

    Silently stops once /autonav-urban/start has taken over (the runtime's
    own telemetry loop runs at 5 Hz and owns the state).
    """
    while True:
        try:
            # If the runtime is driving, its own telemetry_loop owns polling.
            # Yield and sleep quietly.
            if _urban_runtime is not None and _urban_runtime.state.running:
                await asyncio.sleep(1.0)
                continue
            if not _urban_warmup["browser_ready"]:
                await asyncio.sleep(0.5)
                continue
            raw = await browser_service.data()
            if isinstance(raw, dict) and raw:
                _urban_warmup["last_telemetry"] = raw
                _urban_warmup["last_telemetry_ts"] = asyncio.get_event_loop().time()
                try:
                    _urban_warmup["last_telemetry_rover_ts"] = float(raw.get("timestamp") or 0.0)
                except (TypeError, ValueError):
                    _urban_warmup["last_telemetry_rover_ts"] = 0.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _urban_warmup["last_error"] = f"idle_telemetry: {exc}"
        await asyncio.sleep(1.0)


async def _urban_can_auth_without_mission() -> bool:
    """True iff we can populate auth_response_data without consuming a mission.

    Safe paths (no /start-mission call):
    - auth_response_data already populated (someone hit /start-mission earlier)
    - all six token env vars present (get_env_tokens returns them)
    - MISSION_SLUG unset (retrieve_tokens is safe — no mission binding)
    """
    global auth_response_data
    if auth_response_data:
        return True
    env_tokens = get_env_tokens()
    if env_tokens:
        auth_response_data = env_tokens
        return True
    if os.getenv("MISSION_SLUG"):
        return False
    try:
        await auth_common()
        return bool(auth_response_data)
    except Exception as exc:
        logger.warning("autonav-urban warmup: retrieve_tokens failed: %s", exc)
        return False


async def _urban_warmup_samtp():
    """Stage 1 of warmup — SAM-TP load + dummy inference. Always safe."""
    import time as _time
    try:
        import autonav_urban  # noqa: F401 — sets sys.path for vendored sam2
        from autonav_urban import CONFIGS_ROOT, THIRD_PARTY_ROOT
        from autonav_urban.samtp import SAMTPModel, pick_device
        import yaml
        planner_yaml_path = CONFIGS_ROOT / "mini_urban.yaml"
        with planner_yaml_path.open("r") as f:
            planner_yaml = yaml.safe_load(f) or {}
        samtp_cfg = planner_yaml.get("samtp", {}) or {}
        cfg_path = str(THIRD_PARTY_ROOT / samtp_cfg.get(
            "config_path", "sam2/configs/sam2.1_inference_tiny/sam2.1_custom2.yaml",
        ))
        ckpt_path = str(THIRD_PARTY_ROOT / samtp_cfg.get(
            "checkpoint_path", "sam2_ckpt/checkpoint_2.pt",
        ))
        logger.info("autonav-urban warmup: loading SAM-TP on %s ...", pick_device())
        t0 = _time.time()

        def _load_model():
            return SAMTPModel(
                cfg_path=cfg_path,
                ckpt_path=ckpt_path,
                device=None,
                score_thresh=float(samtp_cfg.get("score_thresh", 0.0)),
                multimask=bool(samtp_cfg.get("multimask", False)),
            )

        model = await asyncio.to_thread(_load_model)
        logger.info("autonav-urban warmup: SAM-TP loaded (%.2fs) on %s",
                    _time.time() - t0, model.device)

        import numpy as np
        dummy = np.zeros((256, 384, 3), dtype=np.uint8)
        t1 = _time.time()
        await asyncio.to_thread(model.run_sam2_inference, dummy)
        logger.info("autonav-urban warmup: SAM-TP warmup inference done (%.2fs)",
                    _time.time() - t1)
        _urban_warmup["samtp"] = model
        _urban_warmup["samtp_ready"] = True
    except Exception as exc:
        _urban_warmup["last_error"] = f"samtp warmup: {exc}"
        logger.exception("autonav-urban warmup: SAM-TP warmup failed")


async def _urban_warmup_clipseg():
    """Load CLIPSeg + one dummy inference to compile MPS kernels.

    Non-fatal: if the HuggingFace download fails or transformers isn't
    installed, we log and continue — the runtime falls back to SAM-TP only.
    """
    import time as _time
    try:
        from autonav_urban.clipseg import CLIPSegModel
        from autonav_urban.config import UrbanRuntimeConfig
        # Use default prompt list from config — the runtime will do the same
        # when the user hits Start (or the /start body can override).
        default_prompts = list(UrbanRuntimeConfig().clipseg_prompts)
        logger.info("autonav-urban warmup: loading CLIPSeg ...")
        t0 = _time.time()
        model = await asyncio.to_thread(
            CLIPSegModel,
            default_prompts,
            None,
            float(UrbanRuntimeConfig().clipseg_confidence_thresh),
        )
        logger.info(
            "autonav-urban warmup: CLIPSeg loaded (%.2fs) on %s",
            _time.time() - t0, model.device,
        )
        import numpy as np
        dummy = np.zeros((256, 384, 3), dtype=np.uint8)
        t1 = _time.time()
        await asyncio.to_thread(model.predict, dummy)
        logger.info(
            "autonav-urban warmup: CLIPSeg warmup inference done (%.2fs)",
            _time.time() - t1,
        )
        _urban_warmup["clipseg"] = model
        _urban_warmup["clipseg_ready"] = True
    except Exception as exc:
        logger.warning("autonav-urban warmup: CLIPSeg load failed (%s) — running SAM-TP only", exc)


async def _urban_warmup_browser():
    """Stage 2 of warmup — Chrome + RTM. Requires auth_response_data first.

    Called from _urban_warmup_task at startup (skipped if MISSION_SLUG is set
    and no cached tokens), and again from /autonav-urban/start after
    /start-mission has populated auth (that path is handled by the existing
    _urban_wait_for_browser_ready call).
    """
    try:
        logger.info("autonav-urban warmup: initializing browser + RTM ...")
        ready = await _urban_wait_for_browser_ready(timeout_s=45.0)
        _urban_warmup["browser_ready"] = bool(ready)
        if not ready:
            _urban_warmup["last_error"] = "browser/RTM did not become ready within 45s"
            logger.warning("autonav-urban warmup: %s", _urban_warmup["last_error"])
            return
        logger.info("autonav-urban warmup: browser + RTM ready")
        if _urban_warmup["idle_task"] is None:
            _urban_warmup["idle_task"] = asyncio.create_task(
                _urban_idle_telemetry_loop(), name="urban_idle_telemetry",
            )
    except Exception as exc:
        _urban_warmup["last_error"] = f"browser init: {exc}"
        logger.exception("autonav-urban warmup: browser init failed")


async def _urban_warmup_task():
    """One-shot warmup orchestrator. SAM-TP first (always safe), then browser
    (only if we can auth without consuming a mission)."""
    import time as _time
    _urban_warmup["started_at"] = _time.time()
    _urban_warmup["last_error"] = None
    try:
        # Stage 1 — SAM-TP + CLIPSeg in parallel. Kill the 5-8 s SAM-TP cold
        # start and the 5-8 s CLIPSeg checkpoint download+load. Running them
        # concurrently keeps boot time near max(sam, clipseg) instead of sum.
        # Both are safe to run any time — no bot needed.
        await asyncio.gather(_urban_warmup_samtp(), _urban_warmup_clipseg())

        # Stage 2 — Browser + RTM. Only safe if we can populate auth without
        # calling /start-mission (which would consume a mission attempt).
        can_auth = await _urban_can_auth_without_mission()
        if not can_auth:
            logger.info(
                "autonav-urban warmup: deferring browser init "
                "(MISSION_SLUG is set — click Start to open a mission first)"
            )
            _urban_warmup["last_error"] = "browser warmup deferred until /start-mission"
        else:
            await _urban_warmup_browser()
    finally:
        _urban_warmup["finished_at"] = _time.time()

    if _urban_warmup["browser_ready"] and _urban_warmup["samtp_ready"]:
        logger.info("autonav-urban warmup: complete — /start will use pre-warmed stack")
    elif _urban_warmup["samtp_ready"]:
        logger.info("autonav-urban warmup: partial — SAM-TP warm, browser deferred")


def _urban_warmup_status() -> dict:
    """Snapshot of the warmup / idle-mode state (safe to call any time)."""
    snap = _urban_warmup.get("last_telemetry") or {}
    rover_ts = float(_urban_warmup.get("last_telemetry_rover_ts") or 0.0)
    return {
        "browser_ready": bool(_urban_warmup["browser_ready"]),
        "samtp_ready": bool(_urban_warmup["samtp_ready"]),
        "clipseg_ready": bool(_urban_warmup["clipseg_ready"]),
        "started_at": _urban_warmup["started_at"],
        "finished_at": _urban_warmup["finished_at"],
        "last_error": _urban_warmup["last_error"],
        "telemetry": {
            "battery": snap.get("battery"),
            "yaw_deg": snap.get("orientation"),
            "speed_ms": snap.get("speed"),
            "lat": snap.get("latitude"),
            "lon": snap.get("longitude"),
            "gps_signal": snap.get("gps_signal"),
            "rover_ts": rover_ts,
        } if snap else None,
    }


@app.on_event("startup")
async def _urban_startup_warmup():
    """Kick off warmup on server boot. Never blocks — fire and forget."""
    if os.getenv("AUTONAV_URBAN_WARMUP", "true").lower() == "false":
        logger.info("autonav-urban warmup disabled via AUTONAV_URBAN_WARMUP=false")
        return
    if _urban_warmup["task"] is None or _urban_warmup["task"].done():
        _urban_warmup["task"] = asyncio.create_task(
            _urban_warmup_task(), name="urban_warmup",
        )
        logger.info("autonav-urban warmup task scheduled")


@app.post("/autonav-urban/warmup")
async def autonav_urban_warmup_endpoint():
    """Retrigger warmup (useful if the server started before the bot was online)."""
    if _urban_warmup["task"] is not None and not _urban_warmup["task"].done():
        return JSONResponse(content={"message": "warmup already in progress",
                                     "status": _urban_warmup_status()})
    _urban_warmup["browser_ready"] = False
    _urban_warmup["samtp_ready"] = False
    _urban_warmup["samtp"] = None
    _urban_warmup["clipseg_ready"] = False
    _urban_warmup["clipseg"] = None
    _urban_warmup["last_error"] = None
    _urban_warmup["task"] = asyncio.create_task(_urban_warmup_task(), name="urban_warmup")
    return JSONResponse(content={"message": "warmup started",
                                 "status": _urban_warmup_status()})


async def _urban_get_frame_base64(view: str = "front") -> str:
    """Adapter: return the base64 body (no data-URL prefix) of the latest frame."""
    if view != "front":
        raise ValueError(f"unsupported view for autonav-urban: {view}")
    frame = await browser_service.front()
    if not frame:
        raise RuntimeError("front frame not available yet")
    if "," in frame:
        _, b64 = frame.split(",", 1)
        return b64
    return frame


async def _urban_get_data() -> dict:
    return await browser_service.data()


async def _urban_post_control(linear: float, angular: float, lamp: int = 0):
    """Send a /control command via the browser service.

    IMPORTANT: rover firmware expects the FLAT payload
    ``{linear, angular, lamp}`` — NOT wrapped in a "command" key. The
    /control HTTP endpoint receives ``{"command": {...}}`` and extracts
    the inner object before sending; we must send the flat object directly.
    Sending the wrapped form makes Agora RTM deliver a message the rover
    can't parse — no motion.
    """
    payload = {
        "linear": float(max(-1.0, min(1.0, linear))),
        "angular": float(max(-1.0, min(1.0, angular))),
        "lamp": int(lamp),
    }
    await browser_service.send_message(payload)


async def _urban_wait_for_browser_ready(timeout_s: float = 20.0) -> bool:
    """Poll until window.sendMessage exists AND window.rtm_data is populated.

    Prevents autonav from starting its 10Hz control loop while the RTM
    JavaScript hasn't loaded (which caused every /control send to fail with
    'window.sendMessage is not a function' silently for the first 5-10s).
    """
    await browser_service.initialize_browser()
    if browser_service.page is None:
        return False
    deadline = asyncio.get_event_loop().time() + float(timeout_s)
    while asyncio.get_event_loop().time() < deadline:
        try:
            ok = await browser_service.page.evaluate(
                "() => typeof window.sendMessage === 'function' && typeof window.rtm_data !== 'undefined'"
            )
            if ok:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False


async def _urban_get_checkpoints_list() -> dict:
    """Fetch fresh checkpoint list from FrodoBots backend."""
    await get_checkpoints_list()
    return dict(checkpoints_list_data or {})


async def _urban_checkpoint_reached() -> dict:
    """Report arrival at current checkpoint. Raises HTTPException on backend reject."""
    bot_slug = os.getenv("BOT_SLUG")
    mission_slug = os.getenv("MISSION_SLUG")
    auth_header = os.getenv("SDK_API_TOKEN")
    if not all([bot_slug, mission_slug, auth_header]):
        raise HTTPException(status_code=500, detail="Missing BOT_SLUG/MISSION_SLUG/SDK_API_TOKEN")
    data = await browser_service.data()
    lat = data.get("latitude")
    lon = data.get("longitude")
    if lat is None or lon is None:
        raise HTTPException(status_code=400, detail="Missing latitude/longitude in /data")
    resp = requests.post(
        FRODOBOTS_API_URL + "/sdk/checkpoint_reached",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {auth_header}"},
        json={"bot_slug": bot_slug, "mission_slug": mission_slug,
              "latitude": lat, "longitude": lon},
        timeout=15,
    )
    body = resp.json()
    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail={
                "error": body.get("error", "Failed to send checkpoint data"),
                "proximate_distance_to_checkpoint": body.get("distance_to_checkpoint"),
            },
        )
    return {
        "message": body.get("message", "Checkpoint reached"),
        "next_checkpoint_sequence": body.get("next_checkpoint_sequence"),
    }


@app.post("/autonav-urban/start")
async def autonav_urban_start(request: Request):
    """Start the GENIE-SAMTP perception loop. Phase 5 = perception + BEV only."""
    global _urban_runtime

    try:
        import autonav_urban  # noqa: F401
        from autonav_urban.config import UrbanRuntimeConfig
        from autonav_urban.runtime import build_runtime
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"autonav-urban import error: {exc}")

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        body = {}

    await need_start_mission()

    async with _urban_start_lock:
        if _urban_runtime is not None and _urban_runtime.state.running:
            return JSONResponse(
                status_code=409,
                content={"error": "autonav-urban already running", "status": _urban_runtime.status_dict()},
            )

        # Prefer the warm-boot path: if the FastAPI @startup task already
        # brought the browser + RTM online, skip the 25 s poll.
        if _urban_warmup["browser_ready"]:
            logger.info("autonav-urban: reusing warm browser + RTM (no re-init)")
        else:
            logger.info("autonav-urban: no warm boot — waiting for browser+RTM ...")
            ready = await _urban_wait_for_browser_ready(timeout_s=25.0)
            if not ready:
                raise HTTPException(
                    status_code=503,
                    detail="Browser/RTM did not become ready within 25s. "
                           "Confirm /start-mission succeeded and the bot is online.",
                )
            _urban_warmup["browser_ready"] = True
        logger.info("autonav-urban: browser+RTM ready, launching loops")

        cfg_kwargs = {}
        for key in ("perception_target_hz", "telemetry_hz", "control_hz", "mission_hz",
                    "max_linear", "min_linear", "max_angular", "k_ang", "lookahead_m",
                    "turn_in_place_thresh_deg",
                    "dry_run", "battery_floor", "max_error_streak",
                    "planner_replan_distance_m", "goal_virtual_range_m",
                    "collision_forward_m", "collision_half_width_m",
                    "collision_trav_thresh", "collision_hazard_fraction"):
            if key in body and body[key] is not None:
                cfg_kwargs[key] = body[key]
        cfg = UrbanRuntimeConfig(**cfg_kwargs)

        # Only wire mission callbacks if MISSION_SLUG is set — otherwise we run in
        # free-drive (no checkpoints, no arrival scoring).
        mission_active = bool(os.getenv("MISSION_SLUG"))
        rt = build_runtime(
            cfg,
            get_frame_base64=_urban_get_frame_base64,
            get_data=_urban_get_data,
            post_control=_urban_post_control,
            get_checkpoints_list=_urban_get_checkpoints_list if mission_active else None,
            checkpoint_reached=_urban_checkpoint_reached if mission_active else None,
            planner_yaml_path=body.get("config_path"),
            preloaded_samtp=_urban_warmup["samtp"] if _urban_warmup["samtp_ready"] else None,
            preloaded_clipseg=_urban_warmup["clipseg"] if _urban_warmup["clipseg_ready"] else None,
        )
        try:
            await rt.start()
        except Exception as exc:
            logger.exception("autonav-urban start failed")
            raise HTTPException(status_code=500, detail=f"start failed: {exc}")

        _urban_runtime = rt
        return JSONResponse(content={"message": "autonav-urban started", "status": rt.status_dict()})


@app.post("/autonav-urban/stop")
async def autonav_urban_stop(request: Request):
    global _urban_runtime
    if _urban_runtime is None:
        return JSONResponse(content={"message": "not running"})

    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    reason = str(body.get("reason", "user_requested"))

    rt = _urban_runtime
    _urban_runtime = None
    await rt.stop(reason)
    return JSONResponse(content={"message": "autonav-urban stopped", "status": rt.status_dict()})


@app.get("/autonav-urban/status")
async def autonav_urban_status():
    warmup = _urban_warmup_status()
    if _urban_runtime is None:
        # Idle mode — surface warmup + last polled telemetry so the dashboard
        # can show live cam/battery/GPS/RTM before the user clicks Start.
        tel = warmup.get("telemetry") or {}
        return JSONResponse(content={
            "running": False,
            "mode": "warming" if not warmup["browser_ready"] else "idle",
            "warmup": warmup,
            "last_yaw_deg": tel.get("yaw_deg"),
            "last_speed_ms": tel.get("speed_ms"),
            "battery": tel.get("battery"),
            "last_gps": (
                {"lat": tel["lat"], "lon": tel["lon"]}
                if tel and tel.get("lat") is not None else None
            ),
            "bot_gps": (
                {"lat": tel["lat"], "lon": tel["lon"]}
                if tel and tel.get("lat") is not None else None
            ),
            "rover_ts": tel.get("rover_ts") if tel else None,
            "checkpoints_status": [],
        })
    payload = _urban_runtime.status_dict()
    payload["warmup"] = warmup
    return JSONResponse(content=payload)


@app.get("/autonav-urban/bev")
async def autonav_urban_bev():
    """Latest BEV visualization PNG. Returns 202 if no BEV yet."""
    from fastapi.responses import Response
    if _urban_runtime is None:
        raise HTTPException(status_code=404, detail="autonav-urban not started")
    png = _urban_runtime.latest_bev_png()
    if png is None:
        return Response(status_code=202, content=b"", media_type="image/png")
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/autonav-urban/plan")
async def autonav_urban_plan(request: Request):
    """Latest plan. Query ?fmt=png returns the plan overlay PNG; default returns JSON."""
    from fastapi.responses import Response
    if _urban_runtime is None:
        raise HTTPException(status_code=404, detail="autonav-urban not started")
    fmt = request.query_params.get("fmt", "json").lower()
    if fmt == "png":
        png = _urban_runtime.latest_plan_png()
        if png is None:
            return Response(status_code=202, content=b"", media_type="image/png")
        return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})
    payload = _urban_runtime.latest_plan_json()
    if payload is None:
        return JSONResponse(status_code=202, content={"status": "no_plan_yet"})
    return JSONResponse(content=payload)


@app.post("/autonav-urban/debug-send")
async def autonav_urban_debug_send(request: Request):
    """Fire ONE command directly through the same path autonav uses.

    Use this to isolate: does autonav's send path work when we bypass
    perception/planning entirely? If this rotates the rover but autonav
    doesn't, autonav's control loop is broken. If this ALSO doesn't
    rotate the rover, browser_service.send_message is broken.

    Body: {"linear": 0.0, "angular": 1.0, "hold_s": 4.0}
        hold_s: how long to hold this command before sending stop
    """
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    linear = float(body.get("linear", 0.0))
    angular = float(body.get("angular", 1.0))
    hold_s = float(body.get("hold_s", 4.0))

    # BEFORE snapshot
    before = await browser_service.data() or {}

    # Send via the EXACT same path autonav uses (main.py::_urban_post_control)
    await _urban_post_control(linear, angular, 0)
    logger.info("debug-send: sent linear=%.2f angular=%.2f hold=%.1fs", linear, angular, hold_s)
    await asyncio.sleep(hold_s)
    await _urban_post_control(0.0, 0.0, 0)
    logger.info("debug-send: sent stop")
    await asyncio.sleep(1.0)

    after = await browser_service.data() or {}
    delta_yaw = None
    try:
        delta_yaw = int(after.get("orientation") or 0) - int(before.get("orientation") or 0)
    except Exception:
        pass
    return JSONResponse(content={
        "before": {"yaw": before.get("orientation"), "lat": before.get("latitude"), "lon": before.get("longitude")},
        "after":  {"yaw": after.get("orientation"),  "lat": after.get("latitude"),  "lon": after.get("longitude")},
        "delta_yaw": delta_yaw,
        "sent": {"linear": linear, "angular": angular, "hold_s": hold_s},
    })


@app.get("/autonav-urban/clipseg")
async def autonav_urban_clipseg():
    """CLIPSeg obstacle mask in ORIGINAL front-camera image space.

    Blue=not-obstacle (low prob), red=obstacle (high prob). Same color scheme
    as SAM-TP raw but from CLIPSeg's text-prompted view instead. Useful for
    seeing WHAT CLIPSeg is flagging as an obstacle (rovers, grass, cars, etc).
    """
    from fastapi.responses import Response
    if _urban_runtime is None:
        raise HTTPException(status_code=404, detail="autonav-urban not started")
    png = _urban_runtime.latest_clipseg_png()
    if png is None:
        return Response(status_code=202, content=b"", media_type="image/png")
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/autonav-urban/samtp")
async def autonav_urban_samtp():
    """Raw SAM-TP traversability, in ORIGINAL front-camera image space.

    Same color mapping as /bev (green=drivable, red=obstacle, black=unknown)
    but not projected — lets you see what SAM-TP actually predicted before the
    ground-plane transform. Handy for diagnosing over-conservative perception.
    """
    from fastapi.responses import Response
    if _urban_runtime is None:
        raise HTTPException(status_code=404, detail="autonav-urban not started")
    png = _urban_runtime.latest_samtp_png()
    if png is None:
        return Response(status_code=202, content=b"", media_type="image/png")
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/autonav-urban/dashboard")
async def autonav_urban_dashboard():
    """Standalone dashboard: front frame, BEV, plan overlay, live status."""
    return HTMLResponse(content="""<!doctype html>
<html><head>
<meta charset="utf-8"><title>autonav-urban dashboard</title>
<style>
  body{background:#111;color:#eee;font-family:system-ui,sans-serif;margin:0;padding:12px}
  h1{font-size:16px;margin:0 0 12px;font-weight:600}
  .row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start}
  .panel{background:#1e1e1e;border-radius:8px;padding:8px;box-shadow:0 2px 8px #0006}
  .panel h2{font-size:12px;margin:0 0 6px;color:#8cf;font-weight:500}
  .panel img{display:block;max-width:100%;image-rendering:pixelated;background:#000}
  pre{margin:0;font-size:11px;line-height:1.4;color:#ccc;max-height:60vh;overflow:auto}
  .status{min-width:300px}
  button{background:#334;color:#eee;border:1px solid #556;padding:4px 10px;border-radius:4px;font:inherit;margin-right:6px;cursor:pointer}
  button:hover{background:#556}
  .kv{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;font-size:12px;margin-bottom:8px}
  .kv .k{color:#8cf}
  .warn{color:#f80}
  .ok{color:#4d5}
  label{font-size:12px;display:block;margin:6px 0}
  input[type=number]{width:80px;background:#222;color:#eee;border:1px solid #445;padding:2px 4px}
</style></head><body>
<h1>autonav-urban — full stack (Phases 5→8)</h1>
<div id="warmupBanner" style="display:none;background:#332;border:1px solid #664;color:#fc8;padding:6px 10px;border-radius:6px;font-size:12px;margin-bottom:10px"></div>
<div class="row">
  <div class="panel"><h2>front camera</h2>
    <img id="front" width="480" alt="front frame" style="background:#000">
  </div>
  <div class="panel"><h2>SAM-TP raw (image-space, before projection)</h2>
    <img id="samtp" width="480" alt="samtp raw" style="max-height:270px;object-fit:contain">
    <div style="font-size:10px;color:#888">green = SAM-TP thinks drivable · red = obstacle</div>
  </div>
  <div class="panel"><h2>CLIPSeg obstacles (text-prompted)</h2>
    <img id="clipseg" width="480" alt="clipseg" style="max-height:270px;object-fit:contain;background:#000">
    <div style="font-size:10px;color:#888">red = CLIPSeg says obstacle (rover / car / grass / etc)</div>
  </div>
  <div class="panel"><h2>BEV traversability</h2>
    <img id="bev" width="240" alt="BEV">
  </div>
  <div class="panel"><h2>plan overlay (cost + paths)</h2>
    <img id="plan" width="240" alt="plan overlay">
  </div>
  <div class="panel" style="min-width:280px;max-width:360px">
    <h2>checkpoints</h2>
    <div id="cpList" style="font-size:12px;line-height:1.5">no mission active</div>
    <div id="botGps" style="font-size:11px;color:#8cf;margin-top:8px;padding-top:6px;border-top:1px solid #333">bot GPS: —</div>
  </div>
  <div class="panel status">
    <h2>state</h2>
    <div class="kv" id="kv"></div>
    <div style="margin:6px 0 8px 0">
      <button id="startBtn" title="calls /start-mission then /autonav-urban/start">start</button>
      <button id="stopBtn" title="stops autonav only (mission stays active)">stop</button>
      <button id="endBtn" title="stops autonav AND ends mission (loses progress)" style="background:#622;border-color:#844">end mission</button>
      <button id="pauseBtn" title="pause dashboard polling">pause</button>
      <label><input type="checkbox" id="dryrun"> dry_run (log only, no /control)</label>
      <label><input type="checkbox" id="nocoll"> disable collision monitor</label>
      <label>max_linear <input type="number" id="maxlinear" step="0.05" value="1.0" min="0" max="1"></label>
      <label>min_linear <input type="number" id="minlinear" step="0.05" value="0.45" min="0" max="1" title="deadband floor — commands below this yield ~0.04 m/s"></label>
      <div id="notice" style="margin-top:6px;font-size:11px;color:#8cf;min-height:14px"></div>
    </div>
    <details><summary style="cursor:pointer;font-size:11px;color:#89f">raw status JSON</summary>
      <pre id="status">loading...</pre>
    </details>
  </div>
</div>
<script>
// Serialized-refresh design: NO setInterval. Each poller chains its next
// tick with setTimeout only AFTER the current fetch finished (or errored).
// Requests can never overlap. If the server is slow, we back off, not pile up.
function fmt(v, dp){ return v==null ? '—' : (typeof v==='number' ? v.toFixed(dp||2) : String(v)); }

let paused = false;
let last_bev_ts_seen   = 0;
let last_plan_ts_seen  = 0;
let last_samtp_ts_seen = 0;
let last_clipseg_ts_seen = 0;

function renderCheckpoints(s){
  const list = (s && s.checkpoints_status) || [];
  const el = document.getElementById('cpList');
  const gpsEl = document.getElementById('botGps');
  if(!list.length){
    el.textContent = 'no mission active';
  } else {
    el.innerHTML = list.map(cp => {
      const badge = cp.status === 'achieved' ? '✓' : (cp.status === 'current' ? '▶' : '○');
      const color = cp.status === 'achieved' ? '#4d5' : (cp.status === 'current' ? '#fc4' : '#888');
      const distStr = cp.distance_m == null ? '—' : (cp.distance_m.toFixed(1) + ' m');
      const distSuffix = cp.status === 'achieved' && cp.reached_at_dist_m != null
        ? ` <span style="color:#666">(reached at ${cp.reached_at_dist_m.toFixed(1)} m)</span>`
        : '';
      const gpsCoord = `<span style="color:#555;font-size:10px">${cp.lat.toFixed(6)}, ${cp.lon.toFixed(6)}</span>`;
      return `<div style="display:flex;align-items:baseline;gap:8px;padding:3px 0">
        <span style="color:${color};font-size:16px;width:18px;text-align:center">${badge}</span>
        <span style="min-width:34px;color:${color}">CP ${cp.seq}</span>
        <span style="flex:1">${distStr}${distSuffix}<br>${gpsCoord}</span>
      </div>`;
    }).join('');
  }
  if(s && s.bot_gps){
    gpsEl.textContent = `bot GPS: ${s.bot_gps.lat.toFixed(6)}, ${s.bot_gps.lon.toFixed(6)}`;
  } else {
    gpsEl.textContent = 'bot GPS: —';
  }
}

function renderKV(s){
  const modeClass = ({driving:'ok', scoring:'ok', done:'ok', stopped:'warn', error:'warn', battery_low:'warn'})[s.mode] || '';
  document.getElementById('kv').innerHTML = `
    <div class="k">running</div><div>${s.running}</div>
    <div class="k">mode</div><div class="${modeClass}">${s.mode || '—'}</div>
    <div class="k">RTM link</div><div class="${s.rtm_link === 'DEAD' ? 'warn' : (s.rtm_link === 'alive' ? 'ok' : '')}">${s.rtm_link || '—'} (rover ${fmt(s.rover_age_ms, 0)} ms ago)</div>
    <div class="k">device</div><div>${s.device || '—'}</div>
    <div class="k">dry_run</div><div>${s.dry_run}</div>
    <div class="k">iterations</div><div>${s.iterations}</div>
    <div class="k">errors</div><div>${s.error_streak}</div>
    <div class="k">battery</div><div>${fmt(s.battery, 0)}%</div>
    <div class="k">yaw</div><div>${fmt(s.last_yaw_deg, 1)}°</div>
    <div class="k">speed</div><div>${fmt(s.last_speed_ms, 2)} m/s</div>
    <div class="k">CP</div><div>${s.current_seq || '—'} / ${s.total_checkpoints || '—'}</div>
    <div class="k">dist to CP</div><div>${fmt(s.distance_to_next_m, 1)} m</div>
    <div class="k">goal (local)</div><div>x=${fmt(s.goal_local && s.goal_local.x_m, 2)} y=${fmt(s.goal_local && s.goal_local.y_m, 2)}</div>
    <div class="k">linear</div><div>${fmt(s.last_command && s.last_command.linear, 2)}</div>
    <div class="k">angular</div><div>${fmt(s.last_command && s.last_command.angular, 2)}</div>
    <div class="k">why</div><div>${(s.last_command && s.last_command.reason) || '—'}</div>
    <div class="k">SAM-TP age</div><div>${fmt(s.last_samtp_age_ms, 0)} ms</div>
    <div class="k">BEV age</div><div>${fmt(s.last_bev_age_ms, 0)} ms</div>
    <div class="k">plan age</div><div>${fmt(s.last_plan_age_ms, 0)} ms</div>
    <div class="k">last err</div><div class="warn">${s.last_error || ''}</div>
  `;
}

function loadImg(elId, url){
  return new Promise(resolve=>{
    const el = document.getElementById(elId);
    const img = new Image();
    img.onload  = ()=>{ el.src = img.src; resolve(true); };
    img.onerror = ()=>{ resolve(false); };
    img.src = url;
  });
}

let latest_status = null;   // last status snapshot — shared by all pollers

function renderWarmupBanner(s){
  const w = s && s.warmup;
  const banner = document.getElementById('warmupBanner');
  const startBtn = document.getElementById('startBtn');
  if(!w){ banner.style.display='none'; if(startBtn) startBtn.disabled = false; return; }
  const warming = w.started_at && !w.finished_at;
  const deferred = !!w.finished_at && !w.browser_ready;

  if(w.browser_ready && w.samtp_ready){
    banner.style.display='none';
    if(startBtn){ startBtn.disabled = false; startBtn.title = 'calls /start-mission then /autonav-urban/start'; }
    return;
  }
  if(deferred){
    // Warmup ran, SAM-TP is hot, browser wasn't warmed (MISSION_SLUG set).
    // Start is enabled — clicking it opens the mission then warms the browser.
    banner.style.background = '#232';
    banner.style.borderColor = '#464';
    banner.style.color = '#bec';
    banner.textContent = '✓ SAM-TP warm  ·  browser will initialize on Start (~10-15 s to open mission + RTM)';
    banner.style.display='block';
    if(startBtn){ startBtn.disabled = false; startBtn.title = 'opens mission + browser + autonav (browser warmup is fast because SAM-TP is already hot)'; }
    return;
  }
  // Actively warming — disable Start.
  const bits = [];
  bits.push(w.browser_ready ? '✓ browser + RTM' : '… browser + RTM');
  bits.push(w.samtp_ready ? '✓ SAM-TP warm' : '… SAM-TP warming');
  bits.push(w.clipseg_ready ? '✓ CLIPSeg warm' : '… CLIPSeg warming');
  banner.style.background = '#332';
  banner.style.borderColor = '#664';
  banner.style.color = '#fc8';
  banner.textContent = 'Warming up:  ' + bits.join('   ·   ') + (w.last_error ? '   ⚠ '+w.last_error : '');
  banner.style.display='block';
  if(startBtn) startBtn.disabled = warming && !w.samtp_ready;
}

// Poller A: status only — cheap, tick every 800 ms
async function pollStatus(){
  if(paused){ return schedule(pollStatus, 800); }
  try{
    const r = await fetch('/autonav-urban/status', {cache:'no-store'});
    const s = await r.json();
    latest_status = s;
    document.getElementById('status').textContent = JSON.stringify(s, null, 2);
    renderKV(s);
    renderCheckpoints(s);
    renderWarmupBanner(s);
  }catch(e){
    document.getElementById('status').textContent = 'status err: '+e.message;
  }
  schedule(pollStatus, 800);
}

// Poller B: BEV image — only refreshes when the server has a NEW BEV
async function pollBev(){
  if(paused){ return schedule(pollBev, 1500); }
  try{
    const s = latest_status;
    if(s && s.running && s.last_bev_ts && s.last_bev_ts !== last_bev_ts_seen){
      const ok = await loadImg('bev', '/autonav-urban/bev?t=' + s.last_bev_ts);
      if(ok) last_bev_ts_seen = s.last_bev_ts;
    }
  }catch(_){}
  schedule(pollBev, 1500);
}

// Poller C: plan image — only refreshes when the server has a NEW plan
async function pollPlan(){
  if(paused){ return schedule(pollPlan, 1500); }
  try{
    const s = latest_status;
    if(s && s.running && s.last_plan_ts && s.last_plan_ts !== last_plan_ts_seen){
      const ok = await loadImg('plan', '/autonav-urban/plan?fmt=png&t=' + s.last_plan_ts);
      if(ok) last_plan_ts_seen = s.last_plan_ts;
    }
  }catch(_){}
  schedule(pollPlan, 1500);
}

// Poller E: raw SAM-TP heatmap — refreshes with perception
async function pollSamtp(){
  if(paused){ return schedule(pollSamtp, 1500); }
  try{
    const s = latest_status;
    if(s && s.running && s.last_samtp_ts && s.last_samtp_ts !== last_samtp_ts_seen){
      const ok = await loadImg('samtp', '/autonav-urban/samtp?t=' + s.last_samtp_ts);
      if(ok) last_samtp_ts_seen = s.last_samtp_ts;
    }
  }catch(_){}
  schedule(pollSamtp, 1500);
}

// Poller F: CLIPSeg mask — same cadence as SAM-TP
async function pollClipseg(){
  if(paused){ return schedule(pollClipseg, 1500); }
  try{
    const s = latest_status;
    if(s && s.running && s.last_clipseg_ts && s.last_clipseg_ts !== last_clipseg_ts_seen){
      const ok = await loadImg('clipseg', '/autonav-urban/clipseg?t=' + s.last_clipseg_ts);
      if(ok) last_clipseg_ts_seen = s.last_clipseg_ts;
    }
  }catch(_){}
  schedule(pollClipseg, 1500);
}

// Poller D: front camera — fires as soon as the browser is warm, so the
// dashboard shows live cam BEFORE the user clicks Start.
async function pollFront(){
  if(paused){ return schedule(pollFront, 2000); }
  try{
    const s = latest_status;
    const browserReady = s && ((s.warmup && s.warmup.browser_ready) || s.running);
    if(browserReady){
      const r = await fetch('/v2/front', {cache:'no-store'});
      if(r.ok){
        const f = await r.json();
        if(f && f.front_frame){
          document.getElementById('front').src = 'data:image/jpeg;base64,' + f.front_frame;
        }
      }
    }
  }catch(_){}
  schedule(pollFront, 2000);
}

function schedule(fn, ms){ setTimeout(fn, ms); }

function setNotice(msg, cls){
  const el = document.getElementById('notice');
  el.textContent = msg || '';
  el.style.color = cls === 'err' ? '#f80' : (cls === 'ok' ? '#4d5' : '#8cf');
}

async function postJson(url, body){
  const r = await fetch(url, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body || {}),
  });
  let j = null;
  try { j = await r.json(); } catch(_) {}
  return {ok:r.ok, status:r.status, body:j};
}

document.getElementById('startBtn').onclick = async ()=>{
  const btn = document.getElementById('startBtn');
  btn.disabled = true;
  setNotice('starting mission ...');
  try{
    // Step 1: start the FrodoBots mission (idempotent-ish; harmless if MISSION_SLUG unset).
    const m = await postJson('/start-mission');
    if(!m.ok){
      const detail = m.body && (m.body.detail || m.body.error);
      // If mission already active OR MISSION_SLUG missing, keep going.
      const detailStr = typeof detail === 'string' ? detail.toLowerCase() : '';
      const canContinue = detailStr.includes('already') || detailStr.includes('missing') || m.status === 500;
      if(!canContinue){
        setNotice('start-mission failed: ' + JSON.stringify(detail || m.status), 'err');
        btn.disabled = false;
        return;
      }
      setNotice('mission may already be running — continuing with autonav');
    } else {
      setNotice('mission started, launching autonav ...');
    }

    // Step 2: start autonav.
    const s = await postJson('/autonav-urban/start', {
      dry_run: document.getElementById('dryrun').checked,
      max_linear: parseFloat(document.getElementById('maxlinear').value),
      min_linear: parseFloat(document.getElementById('minlinear').value),
      // If the "disable collision monitor" box is checked, set threshold to 0
      // which is our escape hatch (front_strip_hazard returns False).
      collision_trav_thresh: document.getElementById('nocoll').checked ? 0.0 : undefined,
    });
    if(!s.ok){
      const detail = s.body && (s.body.detail || s.body.error);
      setNotice('autonav-urban/start failed: ' + JSON.stringify(detail || s.status), 'err');
    } else {
      setNotice('autonav-urban running ✓', 'ok');
    }
  }catch(e){
    setNotice('start error: ' + e.message, 'err');
  }
  btn.disabled = false;
};

document.getElementById('stopBtn').onclick = async ()=>{
  setNotice('stopping autonav (mission stays active) ...');
  try{
    await postJson('/autonav-urban/stop');
    setNotice('autonav stopped — mission still active', 'ok');
  }catch(e){
    setNotice('stop error: ' + e.message, 'err');
  }
};

document.getElementById('endBtn').onclick = async ()=>{
  if(!confirm('End mission? This LOSES all progress on this attempt.')) return;
  const btn = document.getElementById('endBtn');
  btn.disabled = true;
  setNotice('stopping autonav ...');
  try{
    await postJson('/autonav-urban/stop');
    setNotice('ending mission ...');
    const e2 = await postJson('/end-mission');
    if(!e2.ok){
      const detail = e2.body && (e2.body.detail || e2.body.error);
      setNotice('end-mission returned: ' + JSON.stringify(detail || e2.status), 'err');
    } else {
      setNotice('mission ended — bot released. Wait ~20 s before starting a new one.', 'ok');
    }
  }catch(e){
    setNotice('end error: ' + e.message, 'err');
  }
  btn.disabled = false;
};

document.getElementById('pauseBtn').onclick = ()=>{
  paused = !paused;
  document.getElementById('pauseBtn').textContent = paused ? 'resume' : 'pause';
};

// Kick off the five pollers.
pollStatus();
pollBev();
pollPlan();
pollFront();
pollSamtp();
pollClipseg();
</script>
</body></html>""")
