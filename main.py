import base64
import contextlib
import html
import hmac
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
import asyncio

import aiohttp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from typing import Literal, Optional

from browser_service import FEED_QUALITY, FORMAT, QUALITY, BrowserService
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
    app.state.http_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=15)
    )
    warmup_task = asyncio.create_task(warmup_browser_when_ready())
    yield
    warmup_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await warmup_task
    await asyncio.gather(
        *(broadcaster.close() for broadcaster in feed_broadcasters.values())
    )
    await browser_service.close()
    await app.state.http_session.close()


app = FastAPI(lifespan=lifespan)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRODOBOTS_API_URL = os.getenv(
    "FRODOBOTS_API_URL", "https://frodobots-web-api.onrender.com/api/v1"
)


# In-memory storage for the response
auth_response_data = {}
checkpoints_list_data = {}
auth_lock = None
auth_lock_loop = None
INGEST_TOKEN = secrets.token_urlsafe(32)

app.mount("/static", StaticFiles(directory="./static"), name="static")

browser_service = BrowserService()
telemetry_hub = TelemetryHub()

feed_broadcasters = {
    "front": FrameBroadcaster(browser_service.front_feed),
    "rear": FrameBroadcaster(browser_service.rear_feed),
}


async def external_request(method: str, url: str, **kwargs) -> tuple[int, dict]:
    """Use pooled async HTTP so rover hot paths never block the event loop."""

    async def perform(session: aiohttp.ClientSession):
        if os.getenv("DEBUG") == "true":
            logger.info("External %s %s", method.upper(), url)
        async with session.request(method, url, **kwargs) as response:
            try:
                body = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                body = {"error": await response.text()}
            return response.status, body

    try:
        session = getattr(app.state, "http_session", None)
        if session and not session.closed:
            return await perform(session)
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as temporary_session:
            return await perform(temporary_session)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="External API timed out") from exc
    except aiohttp.ClientError as exc:
        raise HTTPException(status_code=502, detail="External API unavailable") from exc


async def get_camera_frame(
    view: str,
) -> tuple[Optional[str], Optional[float]]:
    """Return a shared fresh frame and its capture timestamp."""
    if FORMAT == "jpeg" and QUALITY == FEED_QUALITY:
        frame = await feed_broadcasters[view].get_frame(
            max_age=1 / 30, timeout=5, fps=30
        )
        if frame:
            return frame.base64_data, frame.captured_at
        return None, None

    # Preserve explicit png/webp v2 configurations. The default and fastest
    # path is JPEG and shares the feed broadcaster above.
    packet = await browser_service.configured_frame(view)
    if not packet or not packet.get("data_url"):
        return None, None
    return packet["data_url"].split(",", 1)[1], float(packet["timestamp"])


async def latest_rover_data() -> dict:
    age = telemetry_hub.age_seconds
    if telemetry_hub.latest is not None and age is not None and age < 5:
        return telemetry_hub.latest
    return await browser_service.data() or {}


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
    if view == "rear" and auth_response_data.get("BOT_TYPE") != "zero":
        raise HTTPException(status_code=404, detail="Rear camera is not available")

    broadcaster = feed_broadcasters[view]
    queue = await broadcaster.subscribe(
        fps, cached_max_age=min(0.5, max(0.1, 2.0 / fps))
    )
    try:
        first_frame = await asyncio.wait_for(queue.get(), timeout=5)
    except asyncio.TimeoutError as exc:
        await broadcaster.unsubscribe(queue)
        raise HTTPException(
            status_code=503, detail=f"{view} camera is not ready"
        ) from exc
    except asyncio.CancelledError:
        await broadcaster.unsubscribe(queue)
        raise

    async def stream():
        min_interval = 1.0 / fps
        last_sent = 0.0
        try:
            frame = first_frame
            while True:
                now = time.monotonic()
                if now - last_sent < min_interval * 0.9:
                    frame = await queue.get()
                    continue  # this client asked for fewer fps than we capture
                last_sent = now
                jpeg = frame.jpeg
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(jpeg)}\r\n\r\n".encode()
                    + jpeg
                    + b"\r\n"
                )
                frame = await queue.get()
        finally:
            await broadcaster.unsubscribe(queue)

    return StreamingResponse(
        stream(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.websocket("/ws/ingest")
async def ws_ingest(websocket: WebSocket):
    # Private channel for the headless /sdk page; local connections only.
    client_host = websocket.client.host if websocket.client else None
    supplied_token = websocket.query_params.get("token", "")
    if client_host not in ("127.0.0.1", "::1") or not hmac.compare_digest(
        supplied_token, INGEST_TOKEN
    ):
        await websocket.close(code=4403)
        return
    await websocket.accept()
    connection = telemetry_hub.connect_ingest()
    try:
        while True:
            data = await websocket.receive_json()
            telemetry_hub.publish(data)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        telemetry_hub.disconnect_ingest(connection)


@app.websocket("/ws/data")
async def ws_data(websocket: WebSocket):
    await websocket.accept()
    queue = telemetry_hub.subscribe()
    try:
        await websocket.send_json(telemetry_hub.snapshot())
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=5)
            except asyncio.TimeoutError:
                message = {"type": "status", **telemetry_hub.status()}
            await websocket.send_json(message)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        telemetry_hub.unsubscribe(queue)


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
    global auth_lock, auth_lock_loop, auth_response_data
    if auth_response_data:
        return auth_response_data

    # Hypercorn's reloader can recreate the application event loop without
    # recreating every imported module object. asyncio locks are loop-bound on
    # Python 3.9, so never carry this coordinator across loop generations.
    running_loop = asyncio.get_running_loop()
    if auth_lock is None or auth_lock_loop is not running_loop:
        auth_lock = asyncio.Lock()
        auth_lock_loop = running_loop

    async with auth_lock:
        if auth_response_data:
            return auth_response_data
        env_tokens = get_env_tokens()
        if env_tokens:
            auth_response_data = env_tokens
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
    status, response_data = await external_request(
        "POST",
        FRODOBOTS_API_URL + "/sdk/start_ride",
        headers=headers,
        json=start_ride_data,
    )
    if status != 200:
        raise HTTPException(
            status_code=status,
            detail="Bot unavailable for SDK",
        )
    return response_data


async def end_ride(headers, bot_slug, mission_slug):
    end_ride_data = {"bot_slug": bot_slug, "mission_slug": mission_slug}
    status, response_data = await external_request(
        "POST",
        FRODOBOTS_API_URL + "/sdk/end_ride",
        headers=headers,
        json=end_ride_data,
    )
    if status != 200:
        raise HTTPException(status_code=status, detail="Failed to end mission")
    return response_data


async def retrieve_tokens(headers, bot_slug):
    data = {"bot_slug": bot_slug}
    status, response_data = await external_request(
        "POST", FRODOBOTS_API_URL + "/sdk/token", headers=headers, json=data
    )
    if status != 200:
        raise HTTPException(status_code=status, detail="Failed to retrieve tokens")
    return response_data


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

    status, response_data = await external_request(
        "POST",
        FRODOBOTS_API_URL + "/sdk/checkpoints_list",
        headers=headers,
        json=data,
    )
    if status != 200:
        raise HTTPException(
            status_code=status,
            detail="Failed to retrieve checkpoints list",
        )
    checkpoints_list_data = response_data
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
        await end_ride(headers, bot_slug, mission_slug)
        # Clear the stored auth and checkpoints data
        global auth_response_data, checkpoints_list_data
        auth_response_data = {}
        checkpoints_list_data = {}
        await asyncio.gather(
            *(broadcaster.close() for broadcaster in feed_broadcasters.values())
        )
        await browser_service.close()
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
        "appid": html.escape(str(auth_response_data.get("APP_ID", "")), quote=True),
        "rtc_token": html.escape(
            str(auth_response_data.get(f"{token_type}RTC_TOKEN", "")), quote=True
        ),
        "rtm_token": html.escape(
            str("" if is_spectator else auth_response_data.get("RTM_TOKEN", "")),
            quote=True,
        ),
        "channel": html.escape(
            str(auth_response_data.get("CHANNEL_NAME", "")), quote=True
        ),
        "uid": html.escape(
            str(auth_response_data.get(f"{token_type}USERID", "")), quote=True
        ),
        "bot_uid": html.escape(str(auth_response_data.get("BOT_UID", "")), quote=True),
        "ingest_token": html.escape(INGEST_TOKEN, quote=True),
        "checkpoints_list": json.dumps(
            checkpoints_list_data.get("checkpoints_list", [])
        ).replace("</", "<\\/"),
        "map_zoom_level": int(os.getenv("MAP_ZOOM_LEVEL", "18")),
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
    dashboard_config = {
        "appid": tokens.get("APP_ID") or "",
        "rtcToken": tokens.get("SPECTATOR_RTC_TOKEN") or "",
        "channel": tokens.get("CHANNEL_NAME") or "",
        "uid": tokens.get("SPECTATOR_USERID") or "",
        "botUid": tokens.get("BOT_UID") or "",
        "checkpointsList": checkpoints_list_data.get("checkpoints_list", []),
        "mapZoomLevel": int(os.getenv("MAP_ZOOM_LEVEL", "18")),
        "botSlug": os.getenv("BOT_SLUG", ""),
        "missionSlug": os.getenv("MISSION_SLUG", ""),
        "missionStarted": bool(auth_response_data) or not os.getenv("MISSION_SLUG"),
        "bootNotice": str(boot_notice).replace("\n", " "),
    }
    template_vars = {
        "dashboard_config": json.dumps(dashboard_config).replace("</", "<\\/")
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

    await asyncio.to_thread(RtmClient(auth_response_data).send_message, command)

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

    valid_views = {"rear", "map", "front"}
    views_list = view_types.split(",")

    for view in views_list:
        if view not in valid_views:
            raise HTTPException(status_code=400, detail=f"Invalid view type: {view}")

    screenshots = await browser_service.capture_screenshots(views_list)
    missing = [view for view in views_list if view not in screenshots]
    if missing:
        raise HTTPException(
            status_code=404, detail=f"Views not available: {', '.join(missing)}"
        )

    response_content = {
        f"{view}_frame": base64.b64encode(image).decode("ascii")
        for view, image in screenshots.items()
    }

    response_content["timestamp"] = time.time()

    return JSONResponse(content=response_content)


@app.get("/data")
async def get_data():
    await need_start_mission()
    # Fast path: fresh telemetry pushed by the /sdk page, no page.evaluate.
    age = telemetry_hub.age_seconds
    if telemetry_hub.latest is not None and age is not None and age < 2:
        return JSONResponse(content=telemetry_hub.latest)
    data = await latest_rover_data()
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

    data = await latest_rover_data()
    latitude = data.get("latitude")
    longitude = data.get("longitude")

    if latitude is None or longitude is None:
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

    status, response_data = await external_request(
        "POST",
        FRODOBOTS_API_URL + "/sdk/checkpoint_reached",
        headers=headers,
        json=payload,
    )
    if status != 200:
        raise HTTPException(
            status_code=status,
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
    auth_header = os.getenv("SDK_API_TOKEN")
    bot_slug = os.getenv("BOT_SLUG")

    if not auth_header:
        raise HTTPException(status_code=500, detail="Authorization not configured")
    if not bot_slug:
        raise HTTPException(status_code=500, detail="Bot name not configured")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {auth_header}",
    }

    data = {"bot_slug": bot_slug}

    status, response_data = await external_request(
        "POST", FRODOBOTS_API_URL + "/sdk/rides_history", headers=headers, json=data
    )
    if status != 200:
        raise HTTPException(
            status_code=status,
            detail="Failed to retrieve missions history",
        )
    return JSONResponse(content=response_data)


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

    status, response_data = await external_request(
        "GET", FRODOBOTS_API_URL + "/sdk/missions", headers=headers, params=payload
    )
    if status != 200:
        raise HTTPException(
            status_code=status,
            detail="Failed to retrieve missions",
        )
    missions_list = [
        {
            "slug": mission.get("slug"),
            "distance_in_m": mission.get("distance_in_m"),
            "checkpoints_count": mission.get("checkpoints_count"),
        }
        for mission in response_data.get("missions", [])
    ]
    return JSONResponse(content={"missions": missions_list})


@app.get("/v2/screenshot")
async def get_screenshot_v2():
    await need_start_mission()
    if not auth_response_data:
        await auth()

    async def get_frame(frame_type):
        frame, captured_at = await get_camera_frame(frame_type)
        if frame is None:
            return {}
        return {
            f"{frame_type}_frame": frame,
            f"{frame_type}_timestamp": captured_at,
        }

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

    timestamps = [
        value for key, value in response_data.items() if key.endswith("_timestamp")
    ]
    response_data["timestamp"] = max(timestamps)

    return JSONResponse(content=response_data)


@app.get("/v2/front")
async def get_front_frame():
    await need_start_mission()
    if not auth_response_data:
        await auth()
    front_frame, captured_at = await get_camera_frame("front")
    response_data = {}
    if front_frame:
        response_data["front_frame"] = front_frame
        response_data["timestamp"] = captured_at
        return JSONResponse(content=response_data)
    else:
        raise HTTPException(status_code=404, detail="Front frame not available")


@app.get("/v2/rear")
async def get_rear_frame():
    await need_start_mission()
    if not auth_response_data:
        await auth()

    if auth_response_data.get("BOT_TYPE") != "zero":
        raise HTTPException(status_code=404, detail="Rear camera is not available")
    rear_frame, captured_at = await get_camera_frame("rear")
    response_data = {}
    if rear_frame:
        response_data["rear_frame"] = rear_frame
        response_data["timestamp"] = captured_at
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

    data = await latest_rover_data()
    latitude = data.get("latitude")
    longitude = data.get("longitude")

    if latitude is None or longitude is None:
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

    status, response_data = await external_request(
        "POST",
        FRODOBOTS_API_URL + "/sdk/interventions/start",
        headers=headers,
        json=payload,
    )
    if status != 200:
        raise HTTPException(
            status_code=status,
            detail=response_data.get("error", "Failed to start intervention"),
        )
    return JSONResponse(
        status_code=200,
        content={
            "message": "Intervention started successfully",
            "intervention_id": response_data.get("intervention_id"),
        },
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

    data = await latest_rover_data()
    latitude = data.get("latitude")
    longitude = data.get("longitude")

    if latitude is None or longitude is None:
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

    status, response_data = await external_request(
        "POST",
        FRODOBOTS_API_URL + "/sdk/interventions/end",
        headers=headers,
        json=payload,
    )
    if status != 200:
        raise HTTPException(
            status_code=status,
            detail=response_data.get("error", "Failed to end intervention"),
        )
    return JSONResponse(
        status_code=200,
        content={"message": "Intervention ended successfully"},
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

    status, response_data = await external_request(
        "GET",
        FRODOBOTS_API_URL + "/sdk/interventions/history",
        headers=headers,
        params=payload,
    )
    if status != 200:
        raise HTTPException(
            status_code=status,
            detail="Failed to retrieve interventions history",
        )
    return JSONResponse(content=response_data)
