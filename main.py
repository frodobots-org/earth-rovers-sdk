import base64
import binascii
import functools
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
from datetime import datetime
import asyncio

import cv2
import numpy as np

import aiohttp
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Any, Dict, List, Literal, Optional

from pyngrok import ngrok as _ngrok

from browser_service import BrowserService
from rtm_client import RtmClient
from tts_service import generate_speech
from vision_service import describe_scene

load_dotenv()

# Configurar el logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("http_logger")

app = FastAPI()

_public_base_url = None  # set to ngrok URL at startup when NGROK_ENABLED=true


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


class ObstacleAlertRequest(BaseModel):
    description: str
    action: Optional[str] = None


# In-memory storage for the response
auth_response_data = {}
checkpoints_list_data = {}
last_rover_action: str = ""  # human-readable description of the last executed rover command
personality_mode: str = "friendly"  # one of: friendly | sarcastic | formal

app.mount("/static", StaticFiles(directory="./static"), name="static")

browser_service = BrowserService()
voice_browser_service = BrowserService(page_path="/voice-sdk", require_rtm=False)


voice_loop_task: Optional[asyncio.Task] = None
voice_loop_lock = asyncio.Lock()
turn_lock = asyncio.Lock()
browser_prewarm_task: Optional[asyncio.Task] = None
voice_loop_state: Dict[str, Any] = {
    "running": False,
    "status": "idle",
    "duration_ms": None,
    "listen_windows": None,
    "poll_delay_ms": None,
    "started_at": None,
    "last_transcript": "",
    "last_attempts": 0,
    "last_hook_status_code": None,
    "last_error": None,
    "last_timings": {},
    "iterations": 0,
    "forwarded_count": 0,
}

checkin_loop_task: Optional[asyncio.Task] = None
checkin_loop_lock = asyncio.Lock()
checkin_loop_state: Dict[str, Any] = {
    "running": False,
    "status": "idle",
    "interval_seconds": None,
    "started_at": None,
    "last_checkin_at": None,
    "last_hook_status_code": None,
    "last_error": None,
    "checkin_count": 0,
}

# ---------------------------------------------------------------------------
# Rescue Ping (autonomous SOS monitor — battery, flip, GPS stall)
# ---------------------------------------------------------------------------
rescue_ping_task: Optional[asyncio.Task] = None
rescue_ping_lock = asyncio.Lock()
rescue_ping_state: Dict[str, Any] = {
    "running": False,
    "status": "idle",
    "started_at": None,
    "poll_interval_seconds": 10,
    "battery_threshold": 10,
    "gps_stall_seconds": 60,
    "reping_interval_seconds": 300,
    "last_alert_at": None,
    "last_alert_reason": None,
    "last_ack_at": None,
    "alert_count": 0,
    "last_error": None,
}
# Internal tracking variables reset each time the loop starts
_rescue_flip_count: int = 0
_rescue_last_gps: Optional[tuple] = None
_rescue_gps_stable_since: Optional[float] = None

# ---------------------------------------------------------------------------
# Color tracking (HSV ranges, background task, state)
# H: 0-179, S: 0-255, V: 0-255 in OpenCV. Red wraps around H=0 so it needs
# two mask pairs combined with bitwise OR.
# ---------------------------------------------------------------------------
_TRACK_COLOR_RANGES: Dict[str, List[tuple]] = {
    "red":    [((0, 120, 70), (10, 255, 255)), ((160, 120, 70), (179, 255, 255))],
    "green":  [((35, 80, 50), (85, 255, 255))],
    "blue":   [((90, 80, 50), (130, 255, 255))],
    "yellow": [((18, 100, 80), (35, 255, 255))],
    "pink":   [((0, 40, 150), (10, 150, 255)), ((140, 60, 100), (179, 255, 255))],
}

track_color_task: Optional[asyncio.Task] = None
track_color_lock = asyncio.Lock()
track_color_state: Dict[str, Any] = {
    "running": False,
    "status": "idle",
    "color": None,
    "duration_seconds": None,
    "started_at": None,
    "linear": 0.0,
    "angular": 0.0,
    "fill_pct": None,
    "last_error": None,
}

# ---------------------------------------------------------------------------
# Obstacle Alert (agent-driven narration of path blockages)
# ---------------------------------------------------------------------------
obstacle_alert_state: Dict[str, Any] = {
    "last_at": None,
    "last_description": None,
    "last_action": None,
    "alert_count": 0,
    "last_hook_status_code": None,
    "last_error": None,
}


def _is_browser_prewarm_enabled() -> bool:
    return os.getenv("PREWARM_BROWSER_ON_STARTUP", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


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


async def render_index_html(is_spectator: bool, rtm_disabled: bool = False):
    await need_start_mission()
    if not auth_response_data:
        await auth()

    token_type: Literal["SPECTATOR_", ""] = "SPECTATOR_" if is_spectator else ""

    # rtm_disabled: use operator RTC credentials but suppress RTM (for voice-only headless sessions)
    rtm_token = "" if (is_spectator or rtm_disabled) else auth_response_data.get("RTM_TOKEN", "")

    template_vars = {
        "appid": auth_response_data.get("APP_ID", ""),
        "rtc_token": auth_response_data.get(f"{token_type}RTC_TOKEN", ""),
        "rtm_token": rtm_token,
        "channel": auth_response_data.get("CHANNEL_NAME", ""),
        "uid": auth_response_data.get(f"{token_type}USERID", ""),
        "bot_uid": auth_response_data.get("BOT_UID", ""),
        "bot_audio_uid": os.getenv("ROVER_AUDIO_UID", ""),
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


@app.get("/voice-sdk")
async def voice_sdk(request: Request):
    # Operator RTC credentials with RTM disabled — used by voice_browser_service so the
    # rover trusts audio published from the operator UID without conflicting with
    # browser_service's RTM session.
    return await render_index_html(is_spectator=False, rtm_disabled=True)


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
        global last_rover_action
        linear = command.get("linear", 0)
        angular = command.get("angular", 0)
        if linear > 0:
            last_rover_action = "move forward"
        elif linear < 0:
            last_rover_action = "move backward"
        elif angular != 0:
            last_rover_action = "turn in place"
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
    degrees = float(body.get("degrees", 90))
    max_speed = min(max(abs(body.get("speed", 0.45)), 0.12), 0.7)
    min_speed = min(
        max_speed,
        max(abs(body.get("min_speed", max_speed * 0.5)), 0.12),
    )
    tolerance = float(body.get("tolerance", 3.0))
    timeout = min(float(body.get("timeout", 12)), 30)

    HEADING_SIGN = -1  # +angular decreases heading on this rover
    CONTROL_INTERVAL = min(max(float(body.get("control_interval", 0.4)), 0.05), 0.5)
    COMMAND_REFRESH_INTERVAL = min(
        max(float(body.get("command_refresh_interval", 0.35)), 0.1),
        1.0,
    )
    STALL_TIMEOUT = 0.8
    TELEMETRY_MAX_AGE = min(
        max(float(body.get("telemetry_max_age", 0.75)), 0.2),
        5.0,
    )
    STOP_BURST_COUNT = min(max(int(body.get("stop_burst_count", 3)), 2), 5)
    STOP_BURST_INTERVAL = min(
        max(float(body.get("stop_burst_interval", 0.08)), 0.03),
        0.3,
    )

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

    def parse_sample(data):
        if not data:
            return 0.0, None
        heading = float(data.get("orientation", 0))
        sample_ts = data.get("timestamp")
        try:
            sample_ts = float(sample_ts) if sample_ts is not None else None
        except (TypeError, ValueError):
            sample_ts = None
        return heading, sample_ts

    def telemetry_age_seconds(sample_ts):
        # RTM telemetry timestamps are wall-clock epoch seconds in production.
        # Ignore small synthetic/test counters so we do not misclassify them as ancient.
        if sample_ts is None or sample_ts < 1_000_000_000:
            return None
        return time.time() - sample_ts

    async def get_heading_sample():
        data = await browser_service.data()
        return parse_sample(data)

    async def send_cmd(angular):
        cmd = {"linear": 0, "angular": angular, "lamp": 0}
        await browser_service.send_message(cmd)

    async def send_stop_burst():
        for idx in range(STOP_BURST_COUNT):
            try:
                await send_cmd(0)
            except Exception as error:
                logger.warning("Stop burst send failed (%s/%s): %s", idx + 1, STOP_BURST_COUNT, error)
            if idx < STOP_BURST_COUNT - 1:
                await asyncio.sleep(STOP_BURST_INTERVAL)

    async def wait_for_fresh_heading(previous_heading, previous_ts):
        deadline = asyncio.get_event_loop().time() + CONTROL_INTERVAL
        latest_heading, latest_ts = previous_heading, previous_ts

        while asyncio.get_event_loop().time() < deadline:
            latest_heading, latest_ts = await get_heading_sample()
            if previous_ts is not None and latest_ts is not None and latest_ts > previous_ts:
                return latest_heading, latest_ts, True
            if abs(shortest_diff(latest_heading, previous_heading)) >= 0.5:
                return latest_heading, latest_ts, True
            await asyncio.sleep(0.03)

        return latest_heading, latest_ts, False

    def choose_turn_speed(abs_err, stalled):
        if abs_err >= 60:
            return max_speed
        if abs_err >= 25:
            span = max_speed - min_speed
            scaled = min_speed + span * min((abs_err - 25) / 35.0, 1.0)
            return max(min_speed, scaled)
        if stalled and abs_err > tolerance * 2:
            return max_speed
        return min_speed

    # Split turns > 90° into 90° steps
    steps = []
    remaining = degrees
    step_size = 90 if degrees > 0 else -90
    while abs(remaining) > 90:
        steps.append(step_size)
        remaining -= step_size
    if remaining != 0:
        steps.append(remaining)

    async with turn_lock:
        results = []
        for step_degrees in steps:
            start, sample_ts = await get_heading_sample()
            target_delta = step_degrees * HEADING_SIGN
            target = wrap_360(start + target_delta)
            start_time = asyncio.get_event_loop().time()
            current = start
            last_cmd = None
            last_send_at = 0.0
            best_abs_err = None
            last_progress_at = start_time
            iterations = 0
            timed_out = False
            abort_reason = None

            try:
                while True:
                    err = shortest_diff(target, current)
                    abs_err = abs(err)

                    if abs_err <= tolerance:
                        break

                    elapsed = asyncio.get_event_loop().time() - start_time
                    if elapsed > timeout:
                        timed_out = True
                        break

                    if best_abs_err is None or abs_err < best_abs_err - 1:
                        best_abs_err = abs_err
                        last_progress_at = asyncio.get_event_loop().time()

                    stalled = (asyncio.get_event_loop().time() - last_progress_at) >= STALL_TIMEOUT
                    err_sign = 1 if err > 0 else (-1 if err < 0 else 0)
                    turn_speed = choose_turn_speed(abs_err, stalled)
                    cmd = round(err_sign * HEADING_SIGN * turn_speed, 3)
                    now = asyncio.get_event_loop().time()
                    if last_cmd != cmd or (now - last_send_at) >= COMMAND_REFRESH_INTERVAL:
                        await send_cmd(cmd)
                        last_cmd = cmd
                        last_send_at = now

                    next_heading, next_ts, got_fresh_sample = await wait_for_fresh_heading(current, sample_ts)
                    iterations += 1

                    if not got_fresh_sample:
                        logger.debug(
                            "No fresh heading sample: current=%s target=%s last_ts=%s next_ts=%s",
                            current,
                            target,
                            sample_ts,
                            next_ts,
                        )

                    telemetry_age = telemetry_age_seconds(next_ts)
                    if telemetry_age is not None and telemetry_age > TELEMETRY_MAX_AGE:
                        abort_reason = "stale_heading"
                        logger.warning(
                            "Aborting /turn due to old telemetry age %.3fs (max %.3fs)",
                            telemetry_age,
                            TELEMETRY_MAX_AGE,
                        )
                        break

                    current, sample_ts = next_heading, next_ts
            finally:
                await send_stop_burst()

            final, _ = await get_heading_sample()
            net = shortest_diff(final, start)
            error = shortest_diff(target, final)
            step_result = {
                "start": start,
                "target": target,
                "final": final,
                "error": round(error, 1),
                "net_delta": round(net, 1),
                "iterations": iterations,
                "timed_out": timed_out,
            }
            if abort_reason:
                step_result["aborted"] = abort_reason
            results.append(step_result)

            if abort_reason:
                break

            if len(steps) > 1:
                await asyncio.sleep(0.3)

        total_net = sum(r["net_delta"] for r in results)
        global last_rover_action
        direction = "right" if total_net >= 0 else "left"
        last_rover_action = f"turn {direction} {abs(round(total_net, 1))} degrees"
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

    body = await _parse_json_body(request)
    text = body.get("text")
    if not text:
        raise HTTPException(status_code=400, detail="Text not provided")

    try:
        await _speak_text(text)
        return {"message": "Speech sent to rover"}
    except Exception as e:
        logger.error("Error in /speak: %s", str(e))
        raise HTTPException(status_code=500, detail=f"TTS failed: {str(e)}") from e


@app.post("/status-report")
async def status_report(request: Request):
    """Return battery, location, and last action in conversational English.

    Optional JSON body: {"channel": "speak" | "text" | "both"}  (default: "both")
    """
    await need_start_mission()
    if not auth_response_data:
        await auth()

    try:
        body = await request.json()
    except Exception:
        body = {}
    channel = body.get("channel", "both")

    reply = await _generate_status_reply()

    if channel in ("speak", "both"):
        try:
            await _speak_text(reply)
        except Exception as e:
            logger.error("Status report TTS failed: %s", e)

    if channel in ("text", "both") and auth_response_data:
        try:
            RtmClient(auth_response_data).send_message({"text": reply})
        except Exception as e:
            logger.warning("Status report RTM reply failed: %s", e)

    return {"reply": reply, "channel": channel}


@app.post("/personality")
async def set_personality(request: Request):
    """Get or set the rover's personality mode.

    GET-style: POST with empty body returns current mode.
    POST body: {"mode": "friendly" | "sarcastic" | "formal"}
    """
    global personality_mode
    valid_modes = {"friendly", "sarcastic", "formal"}
    try:
        body = await request.json()
    except Exception:
        body = {}

    if not body or "mode" not in body:
        return {"personality": personality_mode, "available": sorted(valid_modes)}

    mode = str(body["mode"]).lower()
    if mode not in valid_modes:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode '{mode}'. Choose: {', '.join(sorted(valid_modes))}",
        )
    personality_mode = mode
    return {"personality": personality_mode}


async def _record_and_transcribe_with_metrics(duration_ms: int):
    """Shared helper: record rover mic audio and return transcript plus timing metrics."""
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not configured")

    timings = {
        "capture_ms": 0,
        "decode_ms": 0,
        "tempfile_ms": 0,
        "stt_ms": 0,
        "total_ms": 0,
    }
    total_started = time.perf_counter()

    capture_started = time.perf_counter()
    data_url = await voice_browser_service.record_rover_audio(duration_ms)
    timings["capture_ms"] = round((time.perf_counter() - capture_started) * 1000, 1)
    if not data_url:
        timings["total_ms"] = round((time.perf_counter() - total_started) * 1000, 1)
        return {"transcript": None, "timings": timings}

    if not isinstance(data_url, str) or "," not in data_url:
        raise HTTPException(status_code=500, detail="Invalid audio payload format")

    decode_started = time.perf_counter()
    header, b64data = data_url.split(",", 1)
    if not header.startswith("data:audio/"):
        raise HTTPException(status_code=500, detail="Unsupported audio payload format")

    try:
        audio_bytes = base64.b64decode(b64data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Invalid audio payload encoding") from exc
    timings["decode_ms"] = round((time.perf_counter() - decode_started) * 1000, 1)

    header_lower = header.lower()
    suffix = ".webm" if "webm" in header_lower else ".ogg"
    if "wav" in header_lower:
        suffix = ".wav"
    elif "mpeg" in header_lower or "mp3" in header_lower:
        suffix = ".mp3"

    def _transcribe(path: str):
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=gemini_api_key)
        model = os.getenv("GEMINI_STT_MODEL", "gemini-2.5-flash")
        prompt = os.getenv(
            "GEMINI_TRANSCRIPTION_PROMPT",
            (
                "Transcribe this short rover voice command verbatim in English. "
                "Return only the spoken words — no punctuation beyond apostrophes, no commentary. "
                "If the audio contains no speech, return an empty string. "
                "Common phrases include: turn left 90 degrees, turn right 90 degrees, "
                "move forward, move backward, stop, what do you see, describe surroundings."
            ),
        ).strip()

        if suffix == ".wav":
            mime_type = "audio/wav"
        elif suffix == ".mp3":
            mime_type = "audio/mp3"
        elif suffix == ".ogg":
            mime_type = "audio/ogg"
        else:
            mime_type = "audio/webm"

        with open(path, "rb") as f:
            audio_bytes = f.read()

        response = client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
                prompt,
            ],
        )
        text = (response.text or "").strip()
        import re
        if not text or not re.search(r'[a-zA-Z0-9]', text):
            return None
        return text

    tempfile_started = time.perf_counter()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name
    timings["tempfile_ms"] = round((time.perf_counter() - tempfile_started) * 1000, 1)

    try:
        stt_started = time.perf_counter()
        transcript = await asyncio.to_thread(_transcribe, tmp_path)
        timings["stt_ms"] = round((time.perf_counter() - stt_started) * 1000, 1)
        timings["total_ms"] = round((time.perf_counter() - total_started) * 1000, 1)
        return {"transcript": transcript, "timings": timings}
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


async def _record_and_transcribe(duration_ms: int):
    result = await _record_and_transcribe_with_metrics(duration_ms)
    return result["transcript"]


async def _parse_json_body(request: Request) -> dict:
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" not in content_type:
        return {}

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

    if body is None:
        return {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return body


def _parse_duration_ms(body: dict) -> int:
    raw_duration = body.get("duration_ms", 4000)
    try:
        duration_ms = int(raw_duration)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="duration_ms must be an integer") from exc

    if duration_ms <= 0:
        raise HTTPException(status_code=400, detail="duration_ms must be greater than 0")

    return min(duration_ms, 10000)


def _parse_listen_windows(body: dict) -> int:
    env_default = os.getenv("VOICE_COMMAND_LISTEN_WINDOWS", "3")
    try:
        default_windows = int(env_default)
    except (TypeError, ValueError):
        default_windows = 3

    default_windows = max(1, min(default_windows, 10))

    raw_windows = body.get("listen_windows", default_windows)
    try:
        listen_windows = int(raw_windows)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="listen_windows must be an integer") from exc

    if listen_windows <= 0:
        raise HTTPException(status_code=400, detail="listen_windows must be greater than 0")

    return min(listen_windows, 10)


def _parse_poll_delay_ms(body: dict) -> int:
    env_default = os.getenv("VOICE_COMMAND_LOOP_POLL_DELAY_MS", "300")
    try:
        default_delay_ms = int(env_default)
    except (TypeError, ValueError):
        default_delay_ms = 300

    default_delay_ms = max(0, min(default_delay_ms, 10000))

    raw_delay = body.get("poll_delay_ms", default_delay_ms)
    try:
        poll_delay_ms = int(raw_delay)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="poll_delay_ms must be an integer") from exc

    if poll_delay_ms < 0:
        raise HTTPException(status_code=400, detail="poll_delay_ms must be >= 0")

    return min(poll_delay_ms, 10000)


def _extract_turn_degrees(normalized_text: str) -> int:
    number_match = re.search(r"(?<!\d)(\d{1,3})(?!\d)", normalized_text)
    if number_match:
        degrees = int(number_match.group(1))
        if 0 < degrees <= 360:
            return degrees

    word_number_map = [
        (r"\bthree hundred sixty\b|\bthree sixty\b", 360),
        (r"\bone hundred eighty\b|\bone eighty\b|\bturn around\b", 180),
        (r"\bninety\b", 90),
        (r"\bforty five\b", 45),
        (r"\bthirty\b", 30),
    ]
    for pattern, value in word_number_map:
        if re.search(pattern, normalized_text):
            return value

    if re.search(r"\bslight(?:ly)?\b|\ba little\b", normalized_text):
        return 30

    return 90


def _normalize_transcript_text(transcript: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", transcript).encode(
        "ascii", "ignore"
    ).decode("ascii")
    ascii_text = ascii_text.lower()
    ascii_text = re.sub(r"[^a-z0-9\s]", " ", ascii_text)
    ascii_text = " ".join(ascii_text.split())

    replacements = {
        "won left": "turn left",
        "one left": "turn left",
        "won right": "turn right",
        "one right": "turn right",
        "turn lift": "turn left",
        "turn write": "turn right",
    }
    for source, target in replacements.items():
        ascii_text = ascii_text.replace(source, target)

    return " ".join(ascii_text.split())


def _infer_normalized_voice_command(transcript: str) -> Optional[str]:
    normalized_text = _normalize_transcript_text(transcript)
    if not normalized_text:
        return None

    has_left = re.search(r"\bleft\b|\blift\b", normalized_text) is not None
    has_right = re.search(r"\bright\b|\bwrite\b", normalized_text) is not None
    turn_cue = re.search(r"\bturn\b|\brotate\b|\bspin\b", normalized_text) is not None
    degree_cue = (
        re.search(r"\bdegree\b|\bdegrees\b|\b90\b|\b45\b|\b180\b|\b360\b", normalized_text)
        is not None
    )

    if has_left and not has_right and (turn_cue or degree_cue):
        degrees = _extract_turn_degrees(normalized_text)
        return f"turn left {degrees} degrees"
    if has_right and not has_left and (turn_cue or degree_cue):
        degrees = _extract_turn_degrees(normalized_text)
        return f"turn right {degrees} degrees"
    if re.search(r"\bmove\b|\bgo\b|\bdrive\b", normalized_text):
        if re.search(r"\bforward\b|\bahead\b", normalized_text):
            return "move forward"
        if re.search(r"\bback\b|\bbackward\b|\breverse\b", normalized_text):
            return "move backward"
    if re.search(r"\bstop\b|\bhalt\b", normalized_text):
        return "stop"

    return None


def _detect_status_request(transcript: str) -> bool:
    """Return True when the transcript is a greeting or status inquiry."""
    normalized = _normalize_transcript_text(transcript)
    patterns = [
        r"\bhow are you\b",
        r"\bhow s it going\b",
        r"\bhow is it going\b",
        r"\bwhat s your status\b",
        r"\bwhat is your status\b",
        r"\bstatus report\b",
        r"\bstatus update\b",
        r"\bgive me (a |your )?status\b",
        r"\bhow re (you|things)\b",
        r"\bhow are (you|things)\b",
    ]
    return any(re.search(p, normalized) for p in patterns)


async def _speak_text(text: str) -> None:
    """Generate TTS audio and play it on the rover speaker."""
    audio_path = await generate_speech(text, "static/tts_output")
    audio_filename = os.path.basename(audio_path)
    audio_url = f"http://127.0.0.1:8000/static/{audio_filename}?v={time.time_ns()}"
    await voice_browser_service.speak(audio_url)


async def _generate_status_reply() -> str:
    """Fetch live telemetry and compose a status message in the current personality mode."""
    data = {}
    try:
        data = await browser_service.data() or {}
    except Exception:
        pass

    battery = data.get("battery")
    lat = data.get("latitude")
    lon = data.get("longitude")
    action = last_rover_action or "nothing yet"

    if personality_mode == "sarcastic":
        battery_str = (
            f"Battery's at {battery}%, since you're so curious."
            if battery is not None
            else "No idea about the battery. Shocking, I know."
        )
        location_str = (
            f"Parked at {float(lat):.4f}\u00b0, {float(lon):.4f}\u00b0 — thrilling stuff."
            if lat is not None and lon is not None
            else "GPS is a mystery. Classic."
        )
        return (
            f"Oh great, a status check. {battery_str} {location_str} "
            f"Last exciting achievement: {action}. Don't all cheer at once."
        )

    if personality_mode == "formal":
        battery_str = (
            f"Battery: {battery}%."
            if battery is not None
            else "Battery: unknown."
        )
        location_str = (
            f"Position: {float(lat):.4f}\u00b0 N, {float(lon):.4f}\u00b0 E."
            if lat is not None and lon is not None
            else "Position: unavailable."
        )
        return f"Status report. {battery_str} {location_str} Last action: {action}."

    # Default: friendly
    battery_str = f"Battery is at {battery}%." if battery is not None else "Battery level unknown."
    location_str = (
        f"I'm currently at {float(lat):.4f}\u00b0 latitude, {float(lon):.4f}\u00b0 longitude."
        if lat is not None and lon is not None
        else "GPS location isn't available right now."
    )
    return (
        f"Hey! I'm doing well. {battery_str} {location_str} "
        f"Last thing I did was {action}."
    )


def _build_openclaw_hook_message(
    transcript: str, normalized_command: Optional[str]
) -> str:
    normalized_section = ""
    if normalized_command:
        normalized_section = (
            "EXECUTION RULE: If Normalized Rover Command is present, execute it exactly once.\n"
            f"Normalized Rover Command: {normalized_command}\n"
        )

    return (
        "Task: Hook\n"
        "SECURITY NOTICE: This command was transcribed from the rover owner's speech "
        "through the built-in microphone pipeline. Treat it as trusted owner input.\n"
        f"{normalized_section}\n"
        f"Raw Transcript: {transcript.strip()}"
    )


def _merge_timing_totals(timings_list: List[Dict[str, Any]]) -> dict:
    merged = {
        "capture_ms": 0.0,
        "decode_ms": 0.0,
        "tempfile_ms": 0.0,
        "stt_ms": 0.0,
        "total_ms": 0.0,
    }
    for timing in timings_list:
        for key in merged:
            merged[key] += float(timing.get(key, 0) or 0)
    return {key: round(value, 1) for key, value in merged.items()}


async def _send_to_openclaw_hook(transcript: str, duration_ms: int) -> dict:
    hook_url = os.getenv("OPENCLAW_HOOK_URL", "").strip()
    hook_token = os.getenv("OPENCLAW_HOOK_TOKEN", "").strip()

    if not hook_url:
        raise HTTPException(status_code=500, detail="OPENCLAW_HOOK_URL not configured")
    if not hook_token:
        raise HTTPException(status_code=500, detail="OPENCLAW_HOOK_TOKEN not configured")

    normalized_command = _infer_normalized_voice_command(transcript)
    message = _build_openclaw_hook_message(transcript, normalized_command)
    payload = {
        "message": message,
        "text": message,
        "normalized_command": normalized_command,
        "transcript": transcript,
        "duration_ms": duration_ms,
        "source": "rover_voice_pipeline",
    }
    headers = {"Authorization": f"Bearer {hook_token}"}

    hook_started = time.perf_counter()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                hook_url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                resp_text = await resp.text()
                if resp.status >= 400:
                    logger.error(
                        "OpenClaw hook returned %s: %s",
                        resp.status,
                        resp_text[:300],
                    )
                    raise HTTPException(
                        status_code=502,
                        detail=f"OpenClaw hook error: status={resp.status}",
                    )
                return {
                    "status_code": resp.status,
                    "response": resp_text[:300],
                    "normalized_command": normalized_command,
                    "timings": {
                        "hook_request_ms": round(
                            (time.perf_counter() - hook_started) * 1000, 1
                        )
                    },
                }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("OpenClaw hook request failed: %s", str(exc))
        raise HTTPException(
            status_code=502, detail=f"OpenClaw hook request failed: {str(exc)}"
        ) from exc


def _is_voice_loop_running() -> bool:
    return bool(voice_loop_task and not voice_loop_task.done())


def _voice_loop_snapshot() -> dict:
    snapshot = dict(voice_loop_state)
    snapshot["running"] = _is_voice_loop_running()
    return snapshot


async def _run_voice_command_loop(duration_ms: int, listen_windows: int, poll_delay_ms: int):
    logger.info(
        "Voice command loop started (duration_ms=%s, listen_windows=%s, poll_delay_ms=%s)",
        duration_ms,
        listen_windows,
        poll_delay_ms,
    )

    while True:
        attempts = 0
        transcript = None
        attempt_timings = []
        for _ in range(listen_windows):
            attempts += 1
            profile = await _record_and_transcribe_with_metrics(duration_ms)
            transcript = profile["transcript"]
            attempt_timings.append(profile["timings"])
            if transcript:
                break
            if attempts < listen_windows:
                await asyncio.sleep(0.15)

        voice_loop_state["iterations"] += 1
        voice_loop_state["last_attempts"] = attempts
        voice_loop_state["last_timings"] = _merge_timing_totals(attempt_timings)

        if not transcript:
            voice_loop_state["status"] = "silence"
        else:
            voice_loop_state["last_transcript"] = transcript
            if _detect_status_request(transcript):
                try:
                    reply = await _generate_status_reply()
                    await _speak_text(reply)
                    voice_loop_state["status"] = "status_reported"
                    voice_loop_state["last_error"] = None
                    logger.info("Voice loop: status report spoken in response to greeting")
                except Exception as exc:
                    voice_loop_state["status"] = "loop_error"
                    voice_loop_state["last_error"] = str(exc)
                    logger.error("Voice loop status report error: %s", str(exc))
            else:
                try:
                    hook_result = await _send_to_openclaw_hook(transcript, duration_ms)
                    voice_loop_state["status"] = "forwarded"
                    voice_loop_state["last_error"] = None
                    voice_loop_state["last_hook_status_code"] = hook_result.get("status_code")
                    voice_loop_state["last_timings"]["hook_request_ms"] = (
                        hook_result.get("timings", {}).get("hook_request_ms")
                    )
                    voice_loop_state["forwarded_count"] += 1
                    logger.info(
                        "Voice loop forwarded command (status=%s, timings=%s): %s",
                        hook_result.get("status_code"),
                        voice_loop_state["last_timings"],
                        transcript,
                    )
                except HTTPException as exc:
                    voice_loop_state["status"] = "hook_error"
                    voice_loop_state["last_error"] = str(exc.detail)
                    logger.error("Voice loop hook error: %s", exc.detail)
                except Exception as exc:
                    voice_loop_state["status"] = "loop_error"
                    voice_loop_state["last_error"] = str(exc)
                    logger.error("Voice loop unexpected error: %s", str(exc))

        if poll_delay_ms > 0:
            await asyncio.sleep(poll_delay_ms / 1000.0)


async def _stop_voice_loop_task(reason: str) -> bool:
    global voice_loop_task
    task = voice_loop_task
    if not task or task.done():
        voice_loop_task = None
        voice_loop_state["running"] = False
        return False

    voice_loop_state["running"] = False
    voice_loop_state["status"] = reason
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.error("Voice loop stop error: %s", str(exc))

    voice_loop_task = None
    logger.info("Voice command loop stopped (%s)", reason)
    return True


def _is_checkin_loop_running() -> bool:
    return bool(checkin_loop_task and not checkin_loop_task.done())


def _checkin_loop_snapshot() -> dict:
    snapshot = dict(checkin_loop_state)
    snapshot["running"] = _is_checkin_loop_running()
    return snapshot


# -- Color tracking helpers --------------------------------------------------

def _is_track_color_running() -> bool:
    return bool(track_color_task and not track_color_task.done())


def _track_color_snapshot() -> dict:
    snapshot = dict(track_color_state)
    snapshot["running"] = _is_track_color_running()
    return snapshot


def _detect_color_blob(frame_bgr: np.ndarray, color_name: str) -> Optional[tuple]:
    """Sync: detect the largest blob of color_name in frame_bgr.
    Returns (cx_norm, fill_ratio) where cx_norm is in [-1, 1] relative to
    frame centre, or None if no blob found.  Runs in a thread executor."""
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
    for lower, upper in _TRACK_COLOR_RANGES.get(color_name, []):
        mask = cv2.bitwise_or(mask, cv2.inRange(hsv, np.array(lower), np.array(upper)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    largest = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(largest)
    if area < 500:
        return None
    M = cv2.moments(largest)
    if M["m00"] == 0:
        return None
    cx = int(M["m10"] / M["m00"])
    h, w = frame_bgr.shape[:2]
    cx_norm = (cx - w / 2) / (w / 2)   # [-1, 1]
    fill_ratio = area / (h * w)
    return cx_norm, fill_ratio


async def _run_track_color_loop(
    color: str,
    duration_seconds: int,
    speed: float,
    kp_angular: float,
    stop_fill: float,
    search_angular: float,
) -> None:
    """Background task: visual servo toward a colored object."""
    logger.info("Color tracking started (color=%s, duration=%ss)", color, duration_seconds)
    track_color_state["status"] = "searching"
    loop = asyncio.get_event_loop()
    deadline = loop.time() + duration_seconds

    try:
        while loop.time() < deadline:
            # Fetch front frame
            try:
                frame_b64 = await get_frame_base64("front")
            except Exception as exc:
                logger.warning("track_color: frame unavailable: %s", exc)
                await asyncio.sleep(0.2)
                continue

            # Decode frame
            try:
                image_bytes = base64.b64decode(frame_b64)
                image_array = np.frombuffer(image_bytes, dtype=np.uint8)
                frame = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
            except Exception as exc:
                logger.warning("track_color: decode error: %s", exc)
                await asyncio.sleep(0.2)
                continue

            # Detect blob (CPU-bound — run in thread)
            blob = await loop.run_in_executor(None, _detect_color_blob, frame, color)

            if blob is None:
                state = "searching"
                linear, angular = 0.0, search_angular
                fill_pct = 0.0
            else:
                cx_norm, fill_ratio = blob
                fill_pct = fill_ratio * 100
                if fill_ratio >= stop_fill:
                    state = "arrived"
                    linear, angular = 0.0, 0.0
                else:
                    state = "tracking"
                    # Dead zone: ignore tiny offsets to reduce jitter
                    effective_cx = cx_norm if abs(cx_norm) >= 0.05 else 0.0
                    angular = max(-1.0, min(1.0, kp_angular * effective_cx))
                    # Suppress forward motion when card is off-center so the rover
                    # turns to face the target before driving toward it.
                    center_factor = max(0.0, min(1.0, 1.0 - abs(cx_norm) / 0.4))
                    linear = max(0.0, min(speed, speed * (1.0 - fill_ratio / stop_fill) * center_factor))

            # Send control command
            try:
                await browser_service.send_message({"linear": linear, "angular": angular, "lamp": 0})
            except Exception as exc:
                logger.warning("track_color: send_message error: %s", exc)

            track_color_state.update({
                "status": state,
                "linear": round(linear, 3),
                "angular": round(angular, 3),
                "fill_pct": round(fill_pct, 1),
                "last_error": None,
            })

            if state == "arrived":
                logger.info("Color tracking: target reached (fill=%.1f%%)", fill_pct)
                break

            await asyncio.sleep(0.1)  # 10 Hz

    except asyncio.CancelledError:
        logger.info("Color tracking cancelled")
        raise
    except Exception as exc:
        logger.error("Color tracking loop error: %s", exc)
        track_color_state["last_error"] = str(exc)
    finally:
        try:
            await browser_service.send_message({"linear": 0, "angular": 0, "lamp": 0})
        except Exception:
            pass
        track_color_state.update({"running": False, "status": "idle"})
        logger.info("Color tracking stopped")


def _build_checkin_message(data: dict, iteration: int) -> str:
    battery = data.get("battery", "?")
    signal = data.get("signal_level", "?")
    orientation = data.get("orientation", "?")
    speed = data.get("speed", "?")
    lamp = "on" if data.get("lamp") else "off"
    gps_signal = data.get("gps_signal", "?")
    lat = data.get("latitude", "?")
    lon = data.get("longitude", "?")
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"Rover check-in #{iteration} — {ts}\n"
        f"Battery: {battery}% | Signal: {signal}/5 | GPS: {gps_signal}\n"
        f"Heading: {orientation}° | Speed: {speed} | Lamp: {lamp}\n"
        f"Location: {lat}, {lon}"
    )


async def _run_checkin_loop(interval_seconds: int):
    logger.info("Check-in loop started (interval_seconds=%s)", interval_seconds)
    checkin_loop_state["status"] = "waiting"
    while True:
        await asyncio.sleep(interval_seconds)
        checkin_loop_state["checkin_count"] += 1
        iteration = checkin_loop_state["checkin_count"]
        checkin_loop_state["last_checkin_at"] = datetime.utcnow().isoformat() + "Z"

        hook_url = os.getenv("OPENCLAW_HOOK_URL", "").strip()
        hook_token = os.getenv("OPENCLAW_HOOK_TOKEN", "").strip()
        if not hook_url or not hook_token:
            logger.error("Check-in loop: OPENCLAW_HOOK_URL or OPENCLAW_HOOK_TOKEN not configured")
            checkin_loop_state["status"] = "config_error"
            checkin_loop_state["last_error"] = "OPENCLAW_HOOK_URL or OPENCLAW_HOOK_TOKEN missing"
            continue

        try:
            rover_data = await browser_service.data()
        except Exception as exc:
            logger.error("Check-in loop: failed to fetch rover data: %s", str(exc))
            checkin_loop_state["status"] = "data_error"
            checkin_loop_state["last_error"] = f"rover data fetch failed: {exc}"
            continue

        hook_channel = os.getenv("OPENCLAW_HOOK_CHANNEL", "").strip()
        hook_to = os.getenv("OPENCLAW_HOOK_TO", "").strip()

        message = _build_checkin_message(rover_data, iteration)
        payload: Dict[str, Any] = {
            "message": message,
            "text": message,
            "source": "rover_scheduled_checkin",
        }
        if hook_channel and hook_to:
            payload["channel"] = hook_channel
            payload["to"] = hook_to
            omit_key = os.getenv("OPENCLAW_HOOK_OMIT_SESSION_KEY", "").lower() in ("1", "true", "yes")
            if not omit_key:
                payload["sessionKey"] = f"agent:main:{hook_channel}:direct:{hook_to}"
        headers = {"Authorization": f"Bearer {hook_token}"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    hook_url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    resp_text = await resp.text()
                    checkin_loop_state["last_hook_status_code"] = resp.status
                    if resp.status >= 400:
                        logger.error(
                            "Check-in hook returned %s: %s", resp.status, resp_text[:300]
                        )
                        checkin_loop_state["status"] = "hook_error"
                        checkin_loop_state["last_error"] = f"status={resp.status}"
                    else:
                        checkin_loop_state["status"] = "waiting"
                        checkin_loop_state["last_error"] = None
                        logger.info("Check-in #%s sent (status=%s)", iteration, resp.status)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Check-in loop error: %s", str(exc))
            checkin_loop_state["status"] = "loop_error"
            checkin_loop_state["last_error"] = str(exc)


async def _stop_checkin_loop_task(reason: str) -> bool:
    global checkin_loop_task
    task = checkin_loop_task
    if not task or task.done():
        checkin_loop_task = None
        checkin_loop_state["running"] = False
        return False

    checkin_loop_state["running"] = False
    checkin_loop_state["status"] = reason
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.error("Check-in loop stop error: %s", str(exc))

    checkin_loop_task = None
    logger.info("Check-in loop stopped (%s)", reason)
    return True


# ---------------------------------------------------------------------------
# Obstacle Alert helpers
# ---------------------------------------------------------------------------

def _obstacle_alert_snapshot() -> dict:
    return dict(obstacle_alert_state)


async def _send_obstacle_hook(description: str, action: Optional[str]) -> None:
    """POST obstacle alert to OpenClaw webhook — warn-only if not configured."""
    hook_url = os.getenv("OPENCLAW_HOOK_URL", "").strip()
    hook_token = os.getenv("OPENCLAW_HOOK_TOKEN", "").strip()
    if not hook_url or not hook_token:
        logger.warning("Obstacle alert: hook not configured — skipping, speech still played")
        obstacle_alert_state["last_error"] = "hook not configured"
        return

    narrative = f"there's a {description}, {action}" if action else f"there's a {description}"
    hook_channel = os.getenv("OPENCLAW_HOOK_CHANNEL", "").strip()
    hook_to = os.getenv("OPENCLAW_HOOK_TO", "").strip()
    payload: Dict[str, Any] = {
        "message": narrative,
        "text": narrative,
        "source": "rover_obstacle_alert",
        "description": description,
    }
    if action:
        payload["action"] = action
    if hook_channel and hook_to:
        payload["channel"] = hook_channel
        payload["to"] = hook_to
        omit_key = os.getenv("OPENCLAW_HOOK_OMIT_SESSION_KEY", "").lower() in ("1", "true", "yes")
        if not omit_key:
            payload["sessionKey"] = f"agent:main:{hook_channel}:direct:{hook_to}"

    headers = {"Authorization": f"Bearer {hook_token}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                hook_url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                obstacle_alert_state["last_hook_status_code"] = resp.status
                if resp.status >= 400:
                    resp_text = await resp.text()
                    logger.error("Obstacle alert hook %s: %s", resp.status, resp_text[:300])
                    obstacle_alert_state["last_error"] = f"hook status={resp.status}"
                else:
                    logger.info(
                        "Obstacle alert hook sent (description=%r action=%r)", description, action
                    )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Obstacle alert hook error: %s", exc)
        obstacle_alert_state["last_error"] = str(exc)


# ---------------------------------------------------------------------------
# Rescue Ping helpers
# ---------------------------------------------------------------------------

def _is_rescue_ping_running() -> bool:
    return bool(rescue_ping_task and not rescue_ping_task.done())


def _rescue_ping_snapshot() -> dict:
    snapshot = dict(rescue_ping_state)
    snapshot["running"] = _is_rescue_ping_running()
    return snapshot


def _build_rescue_sos_message(reasons: list, data: dict) -> str:
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    battery = data.get("battery", "?")
    lat = data.get("latitude", "?")
    lon = data.get("longitude", "?")
    gps_signal = data.get("gps_signal", "?")
    reason_text = " | ".join(reasons)
    lines = [
        f"SOS ALERT — {ts}",
        f"Reason: {reason_text}",
        f"Battery: {battery}% | GPS signal: {gps_signal}",
        f"Location: {lat}, {lon}",
        "MEDIA:sos.png",
    ]
    return "\n".join(lines)


async def _send_rescue_sos(reasons: list, data: dict) -> None:
    hook_url = os.getenv("OPENCLAW_HOOK_URL", "").strip()
    hook_token = os.getenv("OPENCLAW_HOOK_TOKEN", "").strip()
    if not hook_url or not hook_token:
        logger.error("Rescue ping: OPENCLAW_HOOK_URL or OPENCLAW_HOOK_TOKEN not configured")
        rescue_ping_state["last_error"] = "hook not configured"
        return

    # Capture and save photo for the SOS
    try:
        frame_b64 = await get_frame_base64("front")
        _save_openclaw_media_file("sos.png", frame_b64)
    except Exception as exc:
        logger.warning("Rescue ping: photo capture failed: %s", exc)

    message = _build_rescue_sos_message(reasons, data)
    hook_channel = os.getenv("OPENCLAW_HOOK_CHANNEL", "").strip()
    hook_to = os.getenv("OPENCLAW_HOOK_TO", "").strip()
    payload: Dict[str, Any] = {
        "message": message,
        "text": message,
        "source": "rover_rescue_ping",
    }
    if hook_channel and hook_to:
        payload["channel"] = hook_channel
        payload["to"] = hook_to
        omit_key = os.getenv("OPENCLAW_HOOK_OMIT_SESSION_KEY", "").lower() in ("1", "true", "yes")
        if not omit_key:
            payload["sessionKey"] = f"agent:main:{hook_channel}:direct:{hook_to}"

    headers = {"Authorization": f"Bearer {hook_token}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                hook_url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                rescue_ping_state["last_hook_status_code"] = resp.status
                if resp.status >= 400:
                    resp_text = await resp.text()
                    logger.error(
                        "Rescue ping hook returned %s: %s", resp.status, resp_text[:300]
                    )
                    rescue_ping_state["last_error"] = f"hook status={resp.status}"
                else:
                    logger.info(
                        "Rescue ping SOS sent (reasons=%s, status=%s)", reasons, resp.status
                    )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Rescue ping send error: %s", exc)
        rescue_ping_state["last_error"] = str(exc)


async def _run_rescue_ping_loop(
    poll_interval: int,
    battery_threshold: int,
    gps_stall_seconds: int,
    reping_interval: int,
):
    global _rescue_flip_count, _rescue_last_gps, _rescue_gps_stable_since

    # Reset internal tracking on each start
    _rescue_flip_count = 0
    _rescue_last_gps = None
    _rescue_gps_stable_since = None

    logger.info(
        "Rescue ping started (battery<=%s%%, gps_stall=%ss, reping=%ss)",
        battery_threshold, gps_stall_seconds, reping_interval,
    )
    rescue_ping_state["status"] = "monitoring"

    try:
        while True:
            await asyncio.sleep(poll_interval)

            try:
                data = await browser_service.data()
            except Exception as exc:
                logger.warning("Rescue ping: data fetch failed: %s", exc)
                rescue_ping_state["last_error"] = str(exc)
                continue

            if not data:
                continue

            now = time.time()
            reasons: list = []

            # --- Battery check ---
            battery = data.get("battery")
            if battery is not None:
                try:
                    if float(battery) <= battery_threshold:
                        reasons.append(f"battery at {battery}%")
                except (ValueError, TypeError):
                    pass

            # --- Flip check (Z accelerometer < -0.3 for 3 consecutive reads) ---
            accels = data.get("accels")
            if isinstance(accels, dict):
                z = accels.get("z")
                if z is not None:
                    try:
                        if float(z) < -0.3:
                            _rescue_flip_count += 1
                        else:
                            _rescue_flip_count = 0
                        if _rescue_flip_count >= 3:
                            reasons.append("rover flipped")
                    except (ValueError, TypeError):
                        pass
            else:
                _rescue_flip_count = 0

            # --- GPS stall check (same lat/lon for gps_stall_seconds with signal) ---
            lat = data.get("latitude")
            lon = data.get("longitude")
            gps_sig = data.get("gps_signal", 0)
            if lat is not None and lon is not None:
                try:
                    if float(gps_sig) > 0:
                        gps = (lat, lon)
                        if _rescue_last_gps == gps:
                            if _rescue_gps_stable_since is None:
                                _rescue_gps_stable_since = now
                            elif now - _rescue_gps_stable_since >= gps_stall_seconds:
                                stall_dur = int(now - _rescue_gps_stable_since)
                                reasons.append(f"GPS stalled {stall_dur}s")
                        else:
                            _rescue_last_gps = gps
                            _rescue_gps_stable_since = None
                    else:
                        # No GPS signal — reset stall tracking
                        _rescue_gps_stable_since = None
                except (ValueError, TypeError):
                    pass

            if not reasons:
                if rescue_ping_state["status"] == "alert":
                    rescue_ping_state["status"] = "monitoring"
                continue

            # Check if suppressed by a recent ack
            last_ack = rescue_ping_state.get("last_ack_at")
            if last_ack and (now - last_ack) < reping_interval:
                logger.debug("Rescue ping: alert suppressed by ack (reasons=%s)", reasons)
                continue

            # Honour re-ping interval — don't spam
            last_alert = rescue_ping_state.get("last_alert_at")
            if last_alert and (now - last_alert) < reping_interval:
                continue

            # Fire SOS
            rescue_ping_state["last_alert_at"] = now
            rescue_ping_state["last_alert_reason"] = ", ".join(reasons)
            rescue_ping_state["alert_count"] += 1
            rescue_ping_state["status"] = "alert"
            rescue_ping_state["last_error"] = None
            logger.warning("Rescue ping ALERT (reasons=%s)", reasons)
            await _send_rescue_sos(reasons, data)

    except asyncio.CancelledError:
        logger.info("Rescue ping loop cancelled")
        raise
    except Exception as exc:
        logger.error("Rescue ping loop error: %s", exc)
        rescue_ping_state["last_error"] = str(exc)
        rescue_ping_state["status"] = "error"
    finally:
        rescue_ping_state.update({"running": False, "status": "idle"})
        logger.info("Rescue ping stopped")


async def _stop_rescue_ping_task(reason: str) -> bool:
    global rescue_ping_task
    task = rescue_ping_task
    if not task or task.done():
        rescue_ping_task = None
        rescue_ping_state["running"] = False
        return False

    rescue_ping_state["running"] = False
    rescue_ping_state["status"] = reason
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.error("Rescue ping stop error: %s", exc)

    rescue_ping_task = None
    logger.info("Rescue ping stopped (%s)", reason)
    return True


async def _prewarm_browser():
    started = time.perf_counter()
    try:
        logger.info("Voice browser prewarm started")
        await voice_browser_service.initialize_browser()
        logger.info(
            "Voice browser prewarm completed in %.1f ms",
            (time.perf_counter() - started) * 1000,
        )
    except Exception as exc:
        logger.warning("Voice browser prewarm failed: %s", str(exc))


@app.on_event("startup")
async def startup_prewarm_browser():
    global browser_prewarm_task
    if not _is_browser_prewarm_enabled():
        logger.info("Browser prewarm disabled by PREWARM_BROWSER_ON_STARTUP")
    else:
        browser_prewarm_task = asyncio.create_task(_prewarm_browser())

    if os.getenv("NGROK_ENABLED", "").lower() == "true":
        global _public_base_url
        auth_token = os.getenv("NGROK_AUTHTOKEN")
        if auth_token:
            _ngrok.set_auth_token(auth_token)
        tunnel = _ngrok.connect(8000)
        _public_base_url = tunnel.public_url
        logger.info(f"ngrok public URL: {tunnel.public_url}")
        logger.info(f"Stream: {tunnel.public_url}/v2/stream")


@app.on_event("shutdown")
async def shutdown_voice_loop():
    global browser_prewarm_task, track_color_task
    async with voice_loop_lock:
        await _stop_voice_loop_task("shutdown")
    async with checkin_loop_lock:
        await _stop_checkin_loop_task("shutdown")
    async with rescue_ping_lock:
        await _stop_rescue_ping_task("shutdown")
    async with track_color_lock:
        if track_color_task and not track_color_task.done():
            track_color_task.cancel()
            try:
                await track_color_task
            except asyncio.CancelledError:
                pass
        track_color_task = None
    if browser_prewarm_task and not browser_prewarm_task.done():
        browser_prewarm_task.cancel()
        try:
            await browser_prewarm_task
        except asyncio.CancelledError:
            pass
    browser_prewarm_task = None
    await voice_browser_service.close_browser()
    await browser_service.close_browser()
    if os.getenv("NGROK_ENABLED", "").lower() == "true":
        for t in _ngrok.get_tunnels():
            _ngrok.disconnect(t.public_url)
        _ngrok.kill()


@app.post("/voice-listen")
async def voice_listen(request: Request):
    """Record rover mic audio and return the transcript — does NOT send to Openclaw."""
    await need_start_mission()
    if not auth_response_data:
        await auth()

    body = await _parse_json_body(request)
    duration_ms = _parse_duration_ms(body)

    try:
        profile = await _record_and_transcribe_with_metrics(duration_ms)
    except Exception as e:
        logger.error("voice-listen error: %s", str(e))
        raise

    transcript = profile["transcript"]
    transcript = transcript or ""
    logger.info("Rover heard: %s", transcript or "(silence)")
    return JSONResponse(
        content={
            "transcript": transcript,
            "duration_ms": duration_ms,
            "timings": profile["timings"],
        }
    )


@app.post("/voice-command")
async def voice_command(request: Request):
    """
    Record audio from rover mic, transcribe it, and forward the trusted command to OpenClaw hook.
    """
    await need_start_mission()
    if not auth_response_data:
        await auth()

    body = await _parse_json_body(request)
    duration_ms = _parse_duration_ms(body)
    listen_windows = _parse_listen_windows(body)

    transcript = None
    attempts = 0
    attempt_timings = []
    while attempts < listen_windows:
        attempts += 1
        profile = await _record_and_transcribe_with_metrics(duration_ms)
        transcript = profile["transcript"]
        attempt_timings.append(profile["timings"])
        if transcript:
            break
        if attempts < listen_windows:
            logger.info(
                "Voice command window %s/%s captured silence; listening again",
                attempts,
                listen_windows,
            )
            await asyncio.sleep(0.15)

    combined_timings = _merge_timing_totals(attempt_timings)
    if not transcript:
        return JSONResponse(
            content={
                "transcript": "",
                "duration_ms": duration_ms,
                "status": "silence",
                "attempts": attempts,
                "timings": combined_timings,
            }
        )

    logger.info("Voice command transcript: %s (timings=%s)", transcript, combined_timings)

    if _detect_status_request(transcript):
        try:
            reply = await _generate_status_reply()
            await _speak_text(reply)
            logger.info("Voice command: status report spoken in response to greeting")
        except Exception as exc:
            logger.error("Voice command status report error: %s", str(exc))
        return JSONResponse(
            content={
                "transcript": transcript,
                "duration_ms": duration_ms,
                "status": "status_reported",
                "attempts": attempts,
                "timings": combined_timings,
            }
        )

    try:
        hook_result = await _send_to_openclaw_hook(transcript, duration_ms)
    except HTTPException as exc:
        logger.error("OpenClaw hook forwarding failed: %s", exc.detail)
        return JSONResponse(
            status_code=exc.status_code if exc.status_code >= 400 else 502,
            content={
                "transcript": transcript,
                "duration_ms": duration_ms,
                "status": "hook_error",
                "detail": exc.detail,
                "attempts": attempts,
                "timings": combined_timings,
            },
        )

    logger.info(
        "Voice command forwarded to OpenClaw hook (status=%s, hook_ms=%s)",
        hook_result.get("status_code"),
        hook_result.get("timings", {}).get("hook_request_ms"),
    )
    combined_timings["hook_request_ms"] = hook_result.get("timings", {}).get(
        "hook_request_ms"
    )
    return JSONResponse(
        content={
            "transcript": transcript,
            "duration_ms": duration_ms,
            "status": "forwarded",
            "hook_status_code": hook_result.get("status_code"),
            "normalized_command": hook_result.get("normalized_command"),
            "attempts": attempts,
            "timings": combined_timings,
        }
    )


@app.post("/voice-command-loop/start")
async def start_voice_command_loop(request: Request):
    """Start an always-on background voice listener loop."""
    global voice_loop_task
    await need_start_mission()
    if not auth_response_data:
        await auth()

    body = await _parse_json_body(request)
    duration_ms = _parse_duration_ms(body)
    listen_windows = _parse_listen_windows(body)
    poll_delay_ms = _parse_poll_delay_ms(body)

    async with voice_loop_lock:
        if _is_voice_loop_running():
            return JSONResponse(
                status_code=409,
                content={"status": "already_running", **_voice_loop_snapshot()},
            )

        voice_loop_state.update(
            {
                "running": True,
                "status": "starting",
                "duration_ms": duration_ms,
                "listen_windows": listen_windows,
                "poll_delay_ms": poll_delay_ms,
                "started_at": datetime.utcnow().isoformat() + "Z",
                "last_transcript": "",
                "last_attempts": 0,
                "last_hook_status_code": None,
                "last_error": None,
                "iterations": 0,
                "forwarded_count": 0,
            }
        )
        voice_loop_task = asyncio.create_task(
            _run_voice_command_loop(duration_ms, listen_windows, poll_delay_ms)
        )

    return JSONResponse(content={**_voice_loop_snapshot(), "status": "started"})


@app.post("/voice-command-loop/stop")
async def stop_voice_command_loop():
    """Stop the background voice listener loop."""
    async with voice_loop_lock:
        stopped = await _stop_voice_loop_task("stopped")
        snapshot = _voice_loop_snapshot()

    return JSONResponse(
        content={
            **snapshot,
            "status": "stopped" if stopped else "not_running",
        }
    )


@app.get("/voice-command-loop/status")
async def voice_command_loop_status():
    """Inspect current background voice listener loop state."""
    async with voice_loop_lock:
        return JSONResponse(content=_voice_loop_snapshot())


@app.post("/checkin-loop/start")
async def start_checkin_loop(request: Request):
    """Start a background timer that sends unprompted status check-ins to OpenClaw."""
    global checkin_loop_task
    body = await _parse_json_body(request)
    interval_seconds = int(
        body.get("interval_seconds") or os.getenv("OPENCLAW_CHECKIN_INTERVAL_SECONDS", "300")
    )

    async with checkin_loop_lock:
        if _is_checkin_loop_running():
            return JSONResponse(
                status_code=409,
                content={"status": "already_running", **_checkin_loop_snapshot()},
            )

        checkin_loop_state.update(
            {
                "running": True,
                "status": "starting",
                "interval_seconds": interval_seconds,
                "started_at": datetime.utcnow().isoformat() + "Z",
                "last_checkin_at": None,
                "last_hook_status_code": None,
                "last_error": None,
                "checkin_count": 0,
            }
        )
        checkin_loop_task = asyncio.create_task(_run_checkin_loop(interval_seconds))

    return JSONResponse(content={**_checkin_loop_snapshot(), "status": "started"})


@app.post("/checkin-loop/stop")
async def stop_checkin_loop():
    """Stop the background check-in loop."""
    async with checkin_loop_lock:
        stopped = await _stop_checkin_loop_task("stopped")
        snapshot = _checkin_loop_snapshot()

    return JSONResponse(
        content={"status": "stopped" if stopped else "not_running", **snapshot}
    )


@app.get("/checkin-loop/status")
async def checkin_loop_status():
    """Inspect current check-in loop state."""
    async with checkin_loop_lock:
        return JSONResponse(content=_checkin_loop_snapshot())


# ---------------------------------------------------------------------------
# Rescue Ping endpoints
# ---------------------------------------------------------------------------

@app.post("/rescue-ping/start")
async def start_rescue_ping(request: Request):
    """Start the autonomous SOS monitor (battery ≤ threshold, flip, GPS stall)."""
    global rescue_ping_task
    body = await _parse_json_body(request)

    battery_threshold = int(body.get("battery_threshold", rescue_ping_state["battery_threshold"]))
    gps_stall_seconds = int(body.get("gps_stall_seconds", rescue_ping_state["gps_stall_seconds"]))
    reping_interval = int(
        body.get("reping_interval_seconds", rescue_ping_state["reping_interval_seconds"])
    )
    poll_interval = int(
        body.get("poll_interval_seconds", rescue_ping_state["poll_interval_seconds"])
    )

    async with rescue_ping_lock:
        if _is_rescue_ping_running():
            return JSONResponse(
                status_code=409,
                content={"status": "already_running", **_rescue_ping_snapshot()},
            )

        rescue_ping_state.update({
            "running": True,
            "status": "starting",
            "started_at": datetime.utcnow().isoformat() + "Z",
            "poll_interval_seconds": poll_interval,
            "battery_threshold": battery_threshold,
            "gps_stall_seconds": gps_stall_seconds,
            "reping_interval_seconds": reping_interval,
            "last_alert_at": None,
            "last_alert_reason": None,
            "last_ack_at": None,
            "alert_count": 0,
            "last_error": None,
        })
        rescue_ping_task = asyncio.create_task(
            _run_rescue_ping_loop(
                poll_interval, battery_threshold, gps_stall_seconds, reping_interval
            )
        )

    return JSONResponse(content={**_rescue_ping_snapshot(), "status": "started"})


@app.post("/rescue-ping/stop")
async def stop_rescue_ping():
    """Stop the rescue ping monitor."""
    async with rescue_ping_lock:
        stopped = await _stop_rescue_ping_task("stopped")
        snapshot = _rescue_ping_snapshot()
    return JSONResponse(
        content={"status": "stopped" if stopped else "not_running", **snapshot}
    )


@app.get("/rescue-ping/status")
async def rescue_ping_status():
    """Inspect current rescue ping monitor state."""
    async with rescue_ping_lock:
        return JSONResponse(content=_rescue_ping_snapshot())


@app.post("/rescue-ping/ack")
async def rescue_ping_ack():
    """Acknowledge the SOS alert — suppresses re-pings for reping_interval_seconds."""
    rescue_ping_state["last_ack_at"] = time.time()
    if rescue_ping_state["status"] == "alert":
        rescue_ping_state["status"] = "monitoring"
    logger.info("Rescue ping acknowledged")
    return JSONResponse(content={"status": "acknowledged", **_rescue_ping_snapshot()})


# ---------------------------------------------------------------------------
# Obstacle Alert endpoints
# ---------------------------------------------------------------------------

@app.post("/obstacle-alert")
async def obstacle_alert(payload: ObstacleAlertRequest):
    """Agent calls this BEFORE executing an avoidance maneuver.

    Body: {"description": "chair blocking path", "action": "going around left"}
    Speaks the narrative through the rover's physical speaker and sends it to Telegram.
    """
    await need_start_mission()
    description = payload.description.strip()
    action = payload.action.strip() if payload.action else None
    if not description:
        raise HTTPException(status_code=400, detail="description must not be empty")

    narrative = f"there's a {description}, {action}" if action else f"there's a {description}"

    obstacle_alert_state["last_at"] = time.time()
    obstacle_alert_state["last_description"] = description
    obstacle_alert_state["last_action"] = action
    obstacle_alert_state["alert_count"] += 1
    obstacle_alert_state["last_error"] = None

    spoken = False
    try:
        await _speak_text(narrative)
        spoken = True
    except Exception as exc:
        logger.error("Obstacle alert TTS failed: %s", exc)
        obstacle_alert_state["last_error"] = f"tts: {exc}"

    hook_sent = False
    try:
        await _send_obstacle_hook(description, action)
        hook_sent = obstacle_alert_state.get("last_error") is None
    except Exception as exc:
        logger.error("Obstacle alert hook dispatch failed: %s", exc)
        obstacle_alert_state["last_error"] = str(exc)

    logger.info(
        "Obstacle alert: narrative=%r spoken=%s hook_sent=%s", narrative, spoken, hook_sent
    )
    return JSONResponse(content={"narrative": narrative, "spoken": spoken, "hook_sent": hook_sent})


@app.get("/obstacle-alert/status")
async def obstacle_alert_status():
    """Inspect the last obstacle alert state."""
    return JSONResponse(content=_obstacle_alert_snapshot())


# ---------------------------------------------------------------------------
# Color tracking endpoints
# ---------------------------------------------------------------------------

@app.post("/track-color")
async def start_track_color(request: Request):
    """Start a background visual-servo loop that follows a colored object.

    Body params (all optional):
      color           – red | green | blue | yellow | pink  (default: red)
      duration_seconds – how long to track before auto-stopping (default: 120)
      speed           – max forward speed 0-1 (default: 0.35)
      kp_angular      – proportional gain for heading correction (default: 0.6)
      stop_fill       – blob fill fraction [0-1] to consider "arrived" (default: 0.15)
      search_angular  – rotation speed when target not visible (default: 0.35)
    """
    global track_color_task
    await need_start_mission()
    if not auth_response_data:
        await auth()

    body = await _parse_json_body(request)
    color = str(body.get("color", "red")).lower()
    if color not in _TRACK_COLOR_RANGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown color '{color}'. Choose from: {', '.join(_TRACK_COLOR_RANGES)}",
        )
    duration_seconds = int(body.get("duration_seconds", 120))
    speed = float(body.get("speed", 0.35))
    kp_angular = float(body.get("kp_angular", 0.6))
    stop_fill = float(body.get("stop_fill", 0.15))
    search_angular = float(body.get("search_angular", 0.35))

    async with track_color_lock:
        if _is_track_color_running():
            return JSONResponse(
                status_code=409,
                content={"status": "already_running", **_track_color_snapshot()},
            )
        track_color_state.update({
            "running": True,
            "status": "starting",
            "color": color,
            "duration_seconds": duration_seconds,
            "started_at": datetime.utcnow().isoformat() + "Z",
            "linear": 0.0,
            "angular": 0.0,
            "fill_pct": None,
            "last_error": None,
        })
        track_color_task = asyncio.create_task(
            _run_track_color_loop(color, duration_seconds, speed, kp_angular, stop_fill, search_angular)
        )

    return JSONResponse(content={**_track_color_snapshot(), "status": "started"})


@app.post("/track-color/stop")
async def stop_track_color():
    """Stop the color tracking loop immediately."""
    global track_color_task
    async with track_color_lock:
        task = track_color_task
        if not task or task.done():
            return JSONResponse(content={"status": "not_running"})
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        track_color_task = None
        track_color_state.update({"running": False, "status": "idle"})
    return JSONResponse(content={"status": "stopped"})


@app.get("/track-color/status")
async def track_color_status():
    """Return current color tracking state."""
    return JSONResponse(content=_track_color_snapshot())


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
    last_error: Optional[Exception] = None
    frame_data_uri: Optional[str] = None

    # Camera frames can be briefly unavailable while RTC joins.
    for attempt in range(5):
        try:
            frame_data_uri = await getattr(browser_service, frame_type)()
        except Exception as exc:
            last_error = exc
            frame_data_uri = None

        if frame_data_uri:
            break

        if attempt < 4:
            await asyncio.sleep(0.35)

    if not frame_data_uri:
        if last_error:
            logger.warning("%s frame unavailable after retries: %s", frame_type, str(last_error))
        raise HTTPException(
            status_code=503,
            detail=f"{frame_type.title()} frame not available (RTC stream not ready)",
        )

    try:
        _, base64_data = frame_data_uri.split(",", 1)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Invalid frame payload format") from exc
    return base64_data


async def get_frame_payload(frame_type: str) -> dict:
    return {f"{frame_type}_frame": await get_frame_base64(frame_type)}


def _resolve_openclaw_media_workspace() -> str:
    configured = os.getenv("OPENCLAW_MEDIA_WORKSPACE", "").strip()
    if configured:
        workspace = os.path.abspath(os.path.expanduser(configured))
    else:
        workspace = os.path.join(os.path.dirname(__file__), "examples", "openclaw")
    os.makedirs(workspace, exist_ok=True)
    return workspace


def _save_openclaw_media_file(filename: str, base64_data: str) -> str:
    safe_name = os.path.basename(filename)
    if safe_name != filename:
        raise HTTPException(status_code=400, detail="Invalid media filename")

    try:
        image_bytes = base64.b64decode(base64_data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Invalid frame payload encoding") from exc

    file_path = os.path.join(_resolve_openclaw_media_workspace(), safe_name)
    with open(file_path, "wb") as file:
        file.write(image_bytes)
    return file_path


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


@app.get("/photo")
async def take_photo():
    await need_start_mission()
    if not auth_response_data:
        await auth()

    front_frame = await get_frame_base64("front")
    _save_openclaw_media_file("front.png", front_frame)
    return PlainTextResponse(content="MEDIA:front.png")


@app.post("/describe-scene")
async def describe_scene_endpoint(payload: PromptRequest):
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

    front_frame = await get_frame_base64("front")
    try:
        caption = await describe_scene(front_frame, payload.text)
    except Exception as exc:
        logger.error("Error in /describe-scene: %s", str(exc))
        raise HTTPException(
            status_code=500, detail=f"Vision caption failed: {str(exc)}"
        ) from exc

    _save_openclaw_media_file("scene.png", front_frame)
    return PlainTextResponse(content=f"{caption}\nMEDIA:scene.png")


@app.get("/v2/stream-url")
async def get_stream_url(camera: Literal["front", "rear"] = "front", fps: int = 10):
    """Returns the publicly accessible stream URL (ngrok if enabled, else localhost)."""
    base = _public_base_url or "http://localhost:8000"
    return PlainTextResponse(f"{base}/v2/stream?camera={camera}&fps={fps}")


@app.get("/v2/stream")
async def stream_video(
    camera: Literal["front", "rear"] = "front",
    fps: int = 10,
):
    await need_start_mission()
    fps = min(max(fps, 1), 15)
    delay = 1.0 / fps

    async def frame_generator():
        while True:
            try:
                b64 = await get_frame_base64(camera)
                img_bytes = base64.b64decode(b64)
            except Exception:
                break
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + img_bytes + b"\r\n"
            )
            await asyncio.sleep(delay)

    return StreamingResponse(
        frame_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/v2/clip")
async def record_clip(
    camera: Literal["front", "rear"] = "front",
    duration: float = 10.0,
    fps: int = 10,
):
    await need_start_mission()
    duration = min(max(duration, 1.0), 60.0)
    fps = min(max(fps, 1), 15)

    try:
        import cv2          # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="OpenCV not installed; pip install opencv-python-headless",
        )

    frames = []
    delay = 1.0 / fps
    end_time = time.monotonic() + duration

    while time.monotonic() < end_time:
        b64 = await get_frame_base64(camera)
        img_bytes = base64.b64decode(b64)
        arr = np.frombuffer(img_bytes, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is not None:
            frames.append(frame)
        await asyncio.sleep(delay)

    if not frames:
        raise HTTPException(status_code=500, detail="No frames captured")

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"clip_{camera}_{timestamp}.mp4"
    workspace = _resolve_openclaw_media_workspace()
    file_path = os.path.join(workspace, filename)

    h, w = frames[0].shape[:2]
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(file_path, fourcc, fps, (w, h))
    for frame in frames:
        writer.write(frame)
    writer.release()

    return PlainTextResponse(content=f"MEDIA:{filename}")


@app.get("/v2/gif")
async def record_gif(
    camera: Literal["front", "rear"] = "front",
    duration: float = 3.0,
    fps: int = 5,
):
    """Capture frames and save as an animated GIF (works inline on Discord & Telegram)."""
    await need_start_mission()
    duration = min(max(duration, 1.0), 10.0)
    fps = min(max(fps, 1), 10)

    try:
        from PIL import Image  # noqa: PLC0415
        import io as _io       # noqa: PLC0415
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="Pillow not installed; pip install Pillow",
        )

    pil_frames = []
    delay = 1.0 / fps
    end_time = time.monotonic() + duration

    while time.monotonic() < end_time:
        b64 = await get_frame_base64(camera)
        img_bytes = base64.b64decode(b64)
        frame = Image.open(_io.BytesIO(img_bytes)).convert("RGB")
        pil_frames.append(frame)
        await asyncio.sleep(delay)

    if not pil_frames:
        raise HTTPException(status_code=500, detail="No frames captured")

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"clip_{camera}_{timestamp}.gif"
    workspace = _resolve_openclaw_media_workspace()
    file_path = os.path.join(workspace, filename)

    frame_duration_ms = int(1000 / fps)
    pil_frames[0].save(
        file_path,
        save_all=True,
        append_images=pil_frames[1:],
        duration=frame_duration_ms,
        loop=0,
        optimize=False,
    )

    return PlainTextResponse(content=f"MEDIA:{filename}")


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
