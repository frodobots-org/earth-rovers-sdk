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
from vision_service import describe_scene

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


class PromptRequest(BaseModel):
    text: str


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


@app.post("/turn")
async def turn(request: Request):
    """Precise in-place turn using heading feedback from the orientation sensor."""
    await need_start_mission()
    if not auth_response_data:
        await auth()

    body = await request.json()
    degrees = body.get("degrees", 90)
    speed = min(abs(body.get("speed", 0.15)), 0.7)
    tolerance = body.get("tolerance", 3.0)
    timeout = min(body.get("timeout", 20), 30)

    HEADING_SIGN = -1  # +angular decreases heading on this rover
    DT = 0.05

    def shortest_diff(target, current):
        d = target - current
        while d >= 180:
            d -= 360
        while d < -180:
            d += 360
        return d

    def wrap_360(a):
        while a >= 360:
            a -= 360
        while a < 0:
            a += 360
        return a

    async def get_heading():
        data = await browser_service.data()
        return data.get("orientation", 0)

    async def send_cmd(angular):
        cmd = {"linear": 0, "angular": angular, "lamp": 0}
        await browser_service.send_message(cmd)

    # Split turns > 90° into 90° steps
    steps = []
    remaining = degrees
    step_size = 90 if degrees > 0 else -90
    while abs(remaining) > 90:
        steps.append(step_size)
        remaining -= step_size
    if remaining != 0:
        steps.append(remaining)

    results = []
    for step_degrees in steps:
        start = await get_heading()
        target_delta = step_degrees * HEADING_SIGN
        target = wrap_360(start + target_delta)
        start_time = asyncio.get_event_loop().time()

        while True:
            current = await get_heading()
            err = shortest_diff(target, current)
            abs_err = abs(err)

            if abs_err <= tolerance:
                break

            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                break

            err_sign = 1 if err > 0 else (-1 if err < 0 else 0)
            cmd = err_sign * HEADING_SIGN * speed
            await send_cmd(round(cmd, 3))
            await asyncio.sleep(DT)

        # Stop
        await send_cmd(0)
        await asyncio.sleep(0.05)
        await send_cmd(0)

        final = await get_heading()
        net = shortest_diff(final, start)
        error = shortest_diff(target, final)
        results.append({"start": start, "target": target, "final": final, "error": round(error, 1), "net_delta": round(net, 1)})

        if len(steps) > 1:
            await asyncio.sleep(0.3)

    total_net = sum(r["net_delta"] for r in results)
    return {
        "requested": degrees,
        "actual": round(total_net, 1),
        "steps": results
    }


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


@app.post("/prompt")
async def prompt(payload: PromptRequest):
    await need_start_mission()
    if not auth_response_data:
        await auth()

    normalized = "".join(ch for ch in payload.text.lower() if ch.isalnum() or ch.isspace())
    normalized = " ".join(normalized.split())
    trigger_phrases = {"what do you see", "what can you see", "what are you seeing"}
    if normalized not in trigger_phrases:
        raise HTTPException(
            status_code=400,
            detail="Unsupported prompt. Try: 'what do you see?'",
        )

    image_base64 = await get_frame_base64("front")

    try:
        caption = await describe_scene(image_base64, payload.text)
    except Exception as e:
        logger.error("Error in /prompt: %s", str(e))
        raise HTTPException(status_code=500, detail=f"Vision caption failed: {str(e)}") from e

    return JSONResponse(
        content={
            "type": "scene_caption",
            "caption": caption,
            "front_frame": image_base64,
            "timestamp": datetime.utcnow().timestamp(),
        }
    )


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


async def get_frame_base64(frame_type: str) -> str:
    frame_data_uri = await getattr(browser_service, frame_type)()
    if not frame_data_uri:
        raise HTTPException(status_code=404, detail=f"{frame_type.title()} frame not available")
    try:
        _, base64_data = frame_data_uri.split(",", 1)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Invalid frame payload format") from exc
    return base64_data


async def get_frame_payload(frame_type: str) -> dict:
    return {f"{frame_type}_frame": await get_frame_base64(frame_type)}


@app.get("/v2/screenshot")
async def get_screenshot_v2():
    await need_start_mission()
    if not auth_response_data:
        await auth()

    front_task = asyncio.create_task(get_frame_payload("front"))
    tasks = [front_task]

    if auth_response_data.get("BOT_TYPE") == "zero":
        rear_task = asyncio.create_task(get_frame_payload("rear"))
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
    base64_data = await get_frame_base64("front")
    response_data = {"front_frame": base64_data, "timestamp": datetime.utcnow().timestamp()}
    return JSONResponse(content=response_data)


@app.get("/v2/rear")
async def get_rear_frame():
    await need_start_mission()
    if not auth_response_data:
        await auth()

    base64_data = await get_frame_base64("rear")
    response_data = {"rear_frame": base64_data, "timestamp": datetime.utcnow().timestamp()}
    return JSONResponse(content=response_data)


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
