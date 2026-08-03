import base64
import contextlib
import functools
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
import asyncio

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Literal

from browser_service import BrowserService
from rtm_client import RtmClient
from telemetry_hub import TelemetryHub
from tts_service import generate_speech
from video_feed import FrameBroadcaster

load_dotenv()

# Configurar el logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("http_logger")


async def warmup_browser_when_ready():
    # Hold off while mission gating applies: launching the headless browser
    # renders /sdk, which requires auth, and auth must not run before the
    # user calls /start-mission.
    while os.getenv("MISSION_SLUG") and not auth_response_data:
        await asyncio.sleep(2)
    await browser_service.warmup()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Background task: the page loads /sdk from this same server, which only
    # accepts connections after startup yields — never await warm-up here.
    warmup_task = asyncio.create_task(warmup_browser_when_ready())
    yield
    warmup_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await warmup_task
    await browser_service.close()


app = FastAPI(lifespan=lifespan)


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
telemetry_hub = TelemetryHub()

feed_broadcasters = {
    "front": FrameBroadcaster(browser_service.front),
    "rear": FrameBroadcaster(browser_service.rear),
}


@app.get("/feed")
async def feed(view: str = "front", fps: int = 15):
    await need_start_mission()
    if not auth_response_data:
        await auth()

    if view not in feed_broadcasters:
        raise HTTPException(
            status_code=400, detail=f"Invalid view: {view}. Use front or rear"
        )
    fps = max(1, min(fps, 30))

    broadcaster = feed_broadcasters[view]
    queue = await broadcaster.subscribe(fps)

    async def stream():
        min_interval = 1.0 / fps
        last_sent = 0.0
        try:
            while True:
                jpeg = await queue.get()
                now = time.monotonic()
                if now - last_sent < min_interval * 0.9:
                    continue  # this client asked for fewer fps than we capture
                last_sent = now
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
                    + jpeg
                    + b"\r\n"
                )
        finally:
            await broadcaster.unsubscribe(queue)

    return StreamingResponse(
        stream(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.websocket("/ws/ingest")
async def ws_ingest(websocket: WebSocket):
    # Private channel for the headless /sdk page; local connections only.
    client_host = websocket.client.host if websocket.client else None
    if client_host not in ("127.0.0.1", "::1"):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    telemetry_hub.ingest_connected = True
    await telemetry_hub.broadcast({"type": "status", **telemetry_hub.status()})
    try:
        while True:
            data = await websocket.receive_json()
            await telemetry_hub.publish(data)
    except WebSocketDisconnect:
        pass
    finally:
        telemetry_hub.ingest_connected = False
        await telemetry_hub.broadcast({"type": "status", **telemetry_hub.status()})


@app.websocket("/ws/data")
async def ws_data(websocket: WebSocket):
    await websocket.accept()
    telemetry_hub.add_client(websocket)
    try:
        await websocket.send_json(telemetry_hub.snapshot())
        while True:
            # Incoming messages are ignored; the read doubles as a disconnect
            # detector and its timeout paces the status heartbeat.
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=5)
            except asyncio.TimeoutError:
                await websocket.send_json(
                    {"type": "status", **telemetry_hub.status()}
                )
    except WebSocketDisconnect:
        pass
    finally:
        telemetry_hub.remove_client(websocket)


@app.get("/status")
async def get_status():
    return JSONResponse(
        content={
            "browser_ready": browser_service.is_ready,
            "mission_started": bool(auth_response_data)
            or not os.getenv("MISSION_SLUG"),
            **telemetry_hub.status(),
        }
    )


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
            "SPECTATOR_USERID": os.getenv("SPECTATOR_USERID"),
            "SPECTATOR_RTC_TOKEN": os.getenv("SPECTATOR_RTC_TOKEN"),
            "BOT_TYPE": os.getenv("BOT_TYPE", "mini"),
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


def render_template(filename: str, template_vars: dict) -> HTMLResponse:
    with open(filename, "r", encoding="utf-8") as file:
        html_content = file.read()

    for key, value in template_vars.items():
        html_content = html_content.replace(f"{{{{ {key} }}}}", str(value))

    return HTMLResponse(content=html_content, status_code=200)


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

    return render_template("index.html", template_vars)


@app.get("/")
async def get_index(request: Request):
    # The dashboard renders even when the mission hasn't started or auth
    # fails — it degrades to "waiting" states instead of a raw JSON error.
    boot_notice = ""
    if not auth_response_data:
        try:
            await need_start_mission()
            await auth()
        except HTTPException as e:
            boot_notice = e.detail if isinstance(e.detail, str) else "SDK not ready"
        except Exception:
            boot_notice = "SDK auth failed - check the credentials in .env"

    tokens = auth_response_data or {}
    template_vars = {
        "appid": tokens.get("APP_ID") or "",
        "rtc_token": tokens.get("SPECTATOR_RTC_TOKEN") or "",
        "channel": tokens.get("CHANNEL_NAME") or "",
        "uid": tokens.get("SPECTATOR_USERID") or "",
        "bot_uid": tokens.get("BOT_UID") or "",
        "checkpoints_list": json.dumps(
            checkpoints_list_data.get("checkpoints_list", [])
        ),
        "map_zoom_level": os.getenv("MAP_ZOOM_LEVEL", "18"),
        "bot_slug": os.getenv("BOT_SLUG", ""),
        "mission_slug": os.getenv("MISSION_SLUG", ""),
        # Injected inside a double-quoted JS string literal
        "boot_notice": str(boot_notice).replace('"', "'").replace("\n", " "),
    }
    return render_template("dashboard.html", template_vars)


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
    # Fast path: fresh telemetry pushed by the /sdk page, no page.evaluate.
    age = telemetry_hub.age_seconds
    if telemetry_hub.latest is not None and age is not None and age < 2:
        return JSONResponse(content=telemetry_hub.latest)
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
        if not frame:
            return {}
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
