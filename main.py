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
from autonav_service import decide as autonav_decide

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
    "orange": [((5, 100, 80), (18, 255, 255))],
    "green":  [((35, 80, 50), (85, 255, 255))],
    "cyan":   [((80, 80, 50), (95, 255, 255))],
    "teal":   [((75, 60, 50), (95, 255, 220))],
    "blue":   [((90, 80, 50), (130, 255, 255))],
    "skyblue": [((85, 15, 100), (120, 200, 255))],
    "purple": [((130, 50, 50), (160, 255, 255))],
    "yellow": [((18, 100, 80), (35, 255, 255))],
    "pink":   [((0, 40, 150), (10, 150, 255)), ((140, 60, 100), (179, 255, 255))],
    "black":  [((0, 0, 0), (179, 255, 60))],
    "white":  [((0, 0, 200), (179, 60, 255))],
    "gray":   [((0, 0, 60), (179, 60, 200))],
    "brown":  [((5, 50, 20), (25, 255, 180))],
}

_TRACK_COLOR_ALIASES: Dict[str, str] = {
    "grey": "gray",
    "violet": "purple",
    "magenta": "pink",
    "hotpink": "pink",
    "hot pink": "pink",
    "lightblue": "skyblue",
    "light blue": "skyblue",
    "sky blue": "skyblue",
    "aqua": "cyan",
    "turquoise": "cyan",
}
_TRACK_COLOR_CANONICAL_NAMES = ", ".join(_TRACK_COLOR_RANGES.keys())
_TRACK_COLOR_ALIAS_NAMES = ", ".join(_TRACK_COLOR_ALIASES.keys())

_TRACK_COLOR_MIN_BLOB_AREA = 500
_TRACK_COLOR_MIN_DETECT_FILL = 0.001

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
# Autonomous Navigation (Gemini Flash closed-loop maze driving)
# ---------------------------------------------------------------------------
autonav_loop_task: Optional[asyncio.Task] = None
autonav_loop_lock = asyncio.Lock()
autonav_loop_state: Dict[str, Any] = {
    "running": False,
    "status": "idle",  # idle|starting|waiting|deciding|acting|stopped|error|battery_low
    "started_at": None,
    "iterations": 0,
    "config": {},
    "last_decision": None,
    "last_action_at": None,
    "last_error": None,
    "error_streak": 0,
    "history": [],
    "log_dir": None,
    # Calibrated color profile of the current environment. Populated from
    # early good-looking ticks (frames with a clear horizon) so later ticks
    # can compare current frame colors against a learned floor/wall baseline.
    "floor_rgb": None,       # rolling mean of bottom-half RGB
    "wall_rgb": None,        # rolling mean of top-half RGB
    "color_sample_count": 0,
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


async def _perform_turn(degrees: float, opts: Optional[dict] = None) -> dict:
    """Execute a precise in-place turn. Shared by /turn endpoint and internal callers."""
    await need_start_mission()
    if not auth_response_data:
        await auth()

    opts = opts or {}
    degrees = float(degrees)
    max_speed = min(max(abs(opts.get("speed", 0.45)), 0.12), 0.7)
    min_speed = min(
        max_speed,
        max(abs(opts.get("min_speed", max_speed * 0.5)), 0.12),
    )
    tolerance = float(opts.get("tolerance", 3.0))
    timeout = min(float(opts.get("timeout", 12)), 30)

    HEADING_SIGN = -1  # +angular decreases heading on this rover
    CONTROL_INTERVAL = min(max(float(opts.get("control_interval", 0.4)), 0.05), 0.5)
    COMMAND_REFRESH_INTERVAL = min(
        max(float(opts.get("command_refresh_interval", 0.35)), 0.1),
        1.0,
    )
    STALL_TIMEOUT = 0.8
    TELEMETRY_MAX_AGE = min(
        max(float(opts.get("telemetry_max_age", 0.75)), 0.2),
        5.0,
    )
    STOP_BURST_COUNT = min(max(int(opts.get("stop_burst_count", 3)), 2), 5)
    STOP_BURST_INTERVAL = min(
        max(float(opts.get("stop_burst_interval", 0.08)), 0.03),
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


@app.post("/turn")
async def turn(request: Request):
    """Precise in-place turn using heading feedback from the orientation sensor."""
    body = await request.json()
    degrees = body.get("degrees", 90)
    return await _perform_turn(degrees, body)


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
    if re.search(r"\bstop\b|\bhalt\b", normalized_text):
        return "stop"
    follow_cue = re.search(
        r"\bfollow\b|\bfolow\b|\bfollowing\b|\btrack\b|\btracking\b",
        normalized_text,
    ) is not None
    if follow_cue:
        color = _extract_track_color_from_text(normalized_text)
        if color:
            return f"follow {color} card"
    if re.search(r"\bmove\b|\bgo\b|\bdrive\b", normalized_text):
        if re.search(r"\bforward\b|\bahead\b", normalized_text):
            return "move forward"
        if re.search(r"\bback\b|\bbackward\b|\breverse\b", normalized_text):
            return "move backward"

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
        tracking_section = ""
        if normalized_command.startswith("follow ") and normalized_command.endswith(" card"):
            tracking_section = (
                "Color Tracking Rule: Supported tracking colors are "
                f"{_TRACK_COLOR_CANONICAL_NAMES}. Accepted aliases are "
                f"{_TRACK_COLOR_ALIAS_NAMES}. For a follow-color command, call "
                "POST /track-color with the normalized color.\n"
            )
        normalized_section = (
            "EXECUTION RULE: If Normalized Rover Command is present, execute it exactly once.\n"
            f"Normalized Rover Command: {normalized_command}\n"
            f"{tracking_section}"
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


def _normalize_track_color_name(color_name: str) -> str:
    name = str(color_name or "").lower()
    name = re.sub(r"[^a-z0-9\s]", " ", name)
    name = " ".join(name.split())
    compact_name = name.replace(" ", "")
    return (
        _TRACK_COLOR_ALIASES.get(name)
        or _TRACK_COLOR_ALIASES.get(compact_name)
        or (name if name in _TRACK_COLOR_RANGES else compact_name)
    )


def _track_color_choices() -> str:
    choices = sorted(set(_TRACK_COLOR_RANGES) | set(_TRACK_COLOR_ALIASES))
    return ", ".join(choices)


def _extract_track_color_from_text(normalized_text: str) -> Optional[str]:
    color_words = sorted(
        set(_TRACK_COLOR_RANGES) | set(_TRACK_COLOR_ALIASES),
        key=len,
        reverse=True,
    )
    for color_word in color_words:
        pattern = r"\b" + re.escape(color_word).replace(r"\ ", r"\s+") + r"\b"
        if re.search(pattern, normalized_text):
            color = _normalize_track_color_name(color_word)
            if color in _TRACK_COLOR_RANGES:
                return color
    return None


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
    h, w = frame_bgr.shape[:2]
    frame_area = h * w
    if area < _TRACK_COLOR_MIN_BLOB_AREA:
        return None
    if frame_area > 0 and (area / frame_area) < _TRACK_COLOR_MIN_DETECT_FILL:
        return None
    M = cv2.moments(largest)
    if M["m00"] == 0:
        return None
    cx = int(M["m10"] / M["m00"])
    cx_norm = (cx - w / 2) / (w / 2)   # [-1, 1]
    fill_ratio = area / frame_area
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
    search_direction = 1.0 if search_angular >= 0 else -1.0
    search_turn = search_direction * abs(search_angular)

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
                linear, angular = 0.0, search_turn
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
                    # Front camera is mirrored, so negate the offset to turn toward the card.
                    angular = max(-1.0, min(1.0, -kp_angular * effective_cx))
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
# Autonomous Navigation helpers
# ---------------------------------------------------------------------------

def _is_autonav_loop_running() -> bool:
    return bool(autonav_loop_task and not autonav_loop_task.done())


def _autonav_loop_snapshot() -> dict:
    snapshot = dict(autonav_loop_state)
    snapshot["running"] = _is_autonav_loop_running()
    return snapshot


async def _autonav_stop_burst(count: int = 3, interval: float = 0.08) -> None:
    """Send zero-velocity commands a few times in quick succession to hard-stop the rover."""
    for idx in range(count):
        try:
            await browser_service.send_message({"linear": 0, "angular": 0, "lamp": 0})
        except Exception as exc:
            logger.warning("Autonav stop burst %s/%s failed: %s", idx + 1, count, exc)
        if idx < count - 1:
            await asyncio.sleep(interval)


def _autonav_log_root() -> str:
    configured = os.getenv("AUTONAV_LOG_DIR", "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.join(os.path.dirname(__file__), "autonav_logs")


def _autonav_tick_logging_enabled() -> bool:
    return os.getenv("AUTONAV_SAVE_TICK_LOGS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _autonav_start_run_dir() -> str:
    """Create and return a fresh per-run log directory."""
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(_autonav_log_root(), run_id)
    os.makedirs(path, exist_ok=True)
    return path


def _autonav_write_tick(
    log_dir: Optional[str],
    tick: int,
    payload: Dict[str, Any],
    front_b64: Optional[str],
    rear_b64: Optional[str],
    path_zone_b64: Optional[str] = None,
) -> None:
    """Write one tick's JSON + frames to disk. No-op if log_dir is falsy or IO fails."""
    if not log_dir:
        return
    try:
        stem = os.path.join(log_dir, f"tick_{tick:04d}")
        with open(f"{stem}.json", "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
        if front_b64:
            with open(f"{stem}_front.jpg", "wb") as fh:
                fh.write(base64.b64decode(front_b64))
        if path_zone_b64:
            with open(f"{stem}_path_zone.jpg", "wb") as fh:
                fh.write(base64.b64decode(path_zone_b64))
        if rear_b64:
            with open(f"{stem}_rear.jpg", "wb") as fh:
                fh.write(base64.b64decode(rear_b64))
    except Exception as exc:
        logger.warning("Autonav tick-log write failed (tick=%s): %s", tick, exc)


def _frame_uniformity(frame_b64: str) -> Optional[float]:
    """Grayscale pixel stddev of the front frame. Low values mean the camera
    sees a near-uniform surface. Not sufficient on its own — a textured wall
    filling the frame has high stddev but is still a wall. Pair with
    _frame_top_bottom_delta() to catch both cases.
    Returns None on decode failure (treat as unknown)."""
    try:
        from PIL import Image
        import io
        raw = base64.b64decode(frame_b64)
        img = Image.open(io.BytesIO(raw)).convert("L")
        img.thumbnail((320, 240))
        arr = np.asarray(img, dtype=np.float32)
        return float(arr.std())
    except Exception as exc:
        logger.debug("Frame uniformity decode failed: %s", exc)
        return None


def _frame_top_bottom_delta(frame_b64: str) -> Optional[float]:
    """Mean-brightness difference between the top half and the bottom half of
    the frame (0-255 scale). A real corridor view has a visible horizon so
    the bottom is noticeably darker/lighter than the top. A wall pressed
    against the fisheye has the same color top-to-bottom even if textured.
    Low delta (<8) plus high uniformity = wall-up-close even with texture.
    Returns None on decode failure."""
    try:
        from PIL import Image
        import io
        raw = base64.b64decode(frame_b64)
        img = Image.open(io.BytesIO(raw)).convert("L")
        img.thumbnail((320, 240))
        arr = np.asarray(img, dtype=np.float32)
        h = arr.shape[0]
        if h < 2:
            return None
        top_mean = float(arr[: h // 2].mean())
        bot_mean = float(arr[h // 2 :].mean())
        return abs(top_mean - bot_mean)
    except Exception as exc:
        logger.debug("Frame top/bottom delta failed: %s", exc)
        return None


def _frame_color_samples(frame_b64: str) -> Optional[Dict[str, Any]]:
    """Return mean RGB of the top half and bottom half of the frame plus a
    grayscale horizon delta. Used to build a calibrated per-run color profile
    of floor-vs-wall so we can detect obstacles (bottom no longer matches
    learned floor) and wall-close (both halves match learned wall).
    Returns None on decode failure."""
    try:
        from PIL import Image
        import io
        raw = base64.b64decode(frame_b64)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((320, 240))
        arr = np.asarray(img, dtype=np.float32)
        h = arr.shape[0]
        if h < 2:
            return None
        top = arr[: h // 2]
        bot = arr[h // 2 :]
        return {
            "top_rgb": [round(float(top[..., c].mean()), 1) for c in range(3)],
            "bot_rgb": [round(float(bot[..., c].mean()), 1) for c in range(3)],
        }
    except Exception as exc:
        logger.debug("Frame color sampling failed: %s", exc)
        return None


def _frame_path_crop_base64(frame_b64: str) -> Optional[str]:
    """Return a zoomed JPEG crop of the rover's immediate driving lane."""
    try:
        from PIL import Image
        import io

        raw = base64.b64decode(frame_b64)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = img.size
        crop = img.crop(
            (
                int(w * 0.18),
                int(h * 0.48),
                int(w * 0.82),
                int(h * 0.95),
            )
        )
        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=82)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:
        logger.debug("Frame path crop failed: %s", exc)
        return None


def _frame_path_profile(
    frame_b64: str,
    floor_rgb: Optional[List[float]],
    wall_rgb: Optional[List[float]],
) -> Optional[Dict[str, Any]]:
    """Estimate whether the immediate lane is blocked and which side looks more open."""
    try:
        from PIL import Image
        import io

        raw = base64.b64decode(frame_b64)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((256, 144))
        arr = np.asarray(img, dtype=np.float32)
        h, w = arr.shape[:2]
        if h < 10 or w < 10:
            return None

        def _region_rgb(y0: float, y1: float, x0: float, x1: float) -> List[float]:
            region = arr[int(h * y0) : int(h * y1), int(w * x0) : int(w * x1)]
            return [float(region[..., c].mean()) for c in range(3)]

        def _gray_std(y0: float, y1: float, x0: float, x1: float) -> float:
            region = arr[int(h * y0) : int(h * y1), int(w * x0) : int(w * x1)]
            gray = region.mean(axis=2)
            return float(gray.std())

        left_rgb = _region_rgb(0.58, 0.92, 0.02, 0.30)
        center_rgb = _region_rgb(0.58, 0.92, 0.35, 0.65)
        right_rgb = _region_rgb(0.58, 0.92, 0.70, 0.98)
        left_tall_std = _gray_std(0.20, 0.95, 0.00, 0.28)
        right_tall_std = _gray_std(0.20, 0.95, 0.72, 0.99)

        left_floor = _rgb_distance(left_rgb, floor_rgb)
        left_wall = _rgb_distance(left_rgb, wall_rgb)
        center_floor = _rgb_distance(center_rgb, floor_rgb)
        center_wall = _rgb_distance(center_rgb, wall_rgb)
        right_floor = _rgb_distance(right_rgb, floor_rgb)
        right_wall = _rgb_distance(right_rgb, wall_rgb)

        center_blocked = bool(
            center_floor is not None
            and center_wall is not None
            and center_wall < 22.0
            and center_wall + 10.0 < center_floor
        )

        preferred_turn: Optional[str] = None
        std_gap = right_tall_std - left_tall_std
        open_side_turn: Optional[str] = None
        if std_gap >= 8.0:
            open_side_turn = "turn_right"
        elif std_gap <= -8.0:
            open_side_turn = "turn_left"
        left_floor_like = bool(
            left_floor is not None
            and left_wall is not None
            and left_floor < 24.0
            and left_floor + 8.0 < left_wall
        )
        right_floor_like = bool(
            right_floor is not None
            and right_wall is not None
            and right_floor < 24.0
            and right_floor + 8.0 < right_wall
        )
        if center_blocked:
            if std_gap >= 8.0:
                preferred_turn = "turn_right"
            elif std_gap <= -8.0:
                preferred_turn = "turn_left"
            elif right_floor_like and not left_floor_like:
                preferred_turn = "turn_right"
            elif left_floor_like and not right_floor_like:
                preferred_turn = "turn_left"
            elif std_gap > 0:
                preferred_turn = "turn_right"
            elif std_gap < 0:
                preferred_turn = "turn_left"

        return {
            "center_blocked": center_blocked,
            "preferred_turn": preferred_turn,
            "open_side_turn": open_side_turn,
            "left_tall_std": round(left_tall_std, 1),
            "right_tall_std": round(right_tall_std, 1),
            "side_std_gap": round(std_gap, 1),
            "left_floor_dist": round(left_floor, 1) if left_floor is not None else None,
            "left_wall_dist": round(left_wall, 1) if left_wall is not None else None,
            "center_floor_dist": round(center_floor, 1) if center_floor is not None else None,
            "center_wall_dist": round(center_wall, 1) if center_wall is not None else None,
            "right_floor_dist": round(right_floor, 1) if right_floor is not None else None,
            "right_wall_dist": round(right_wall, 1) if right_wall is not None else None,
        }
    except Exception as exc:
        logger.debug("Frame path profile failed: %s", exc)
        return None


def _format_path_profile_summary(path_profile: Optional[Dict[str, Any]]) -> Optional[str]:
    if not path_profile:
        return None
    return (
        "center_blocked="
        f"{path_profile.get('center_blocked')}; "
        f"preferred_turn={path_profile.get('preferred_turn')}; "
        f"open_side_turn={path_profile.get('open_side_turn')}; "
        f"center(df={path_profile.get('center_floor_dist')}, dw={path_profile.get('center_wall_dist')}); "
        f"left_std={path_profile.get('left_tall_std')}; "
        f"right_std={path_profile.get('right_tall_std')}"
    )


def _apply_center_block_override(
    decision: Dict[str, Any],
    path_profile: Optional[Dict[str, Any]],
    max_turn_deg: float,
) -> Dict[str, Any]:
    """If the immediate lane is blocked, prevent forward motion into the obstacle."""
    if decision.get("action") != "forward" or not path_profile or not path_profile.get("center_blocked"):
        return decision

    forced_turn = path_profile.get("preferred_turn")
    if forced_turn not in ("turn_left", "turn_right"):
        return decision

    direction_text = "left" if forced_turn == "turn_left" else "right"
    return {
        **decision,
        "action": forced_turn,
        "linear_speed": 0.0,
        "turn_degrees": round(min(max_turn_deg, 70.0), 1),
        "duration_ms": decision.get("duration_ms", 700),
        "confidence": max(float(decision.get("confidence") or 0.0), 0.85),
        "reason": (
            "[center-block-override] My immediate lane is blocked by a nearby surface, "
            f"so I turn {direction_text} toward the more open side."
        )[:240],
        "comment_front": (
            f"A nearby surface fills my immediate lane; the {direction_text} side shows more usable space."
        )[:240],
        "plan_of_action": (
            f"I will turn {direction_text} to clear the nearby obstruction, then check the lane again."
        )[:240],
        "reasoning_steps": [
            "The immediate driving lane in front of me is blocked at very close range.",
            "Moving forward would push me into the nearby surface instead of through open floor.",
            f"The {direction_text} side shows more usable depth and structure than the opposite side.",
            f"I will turn {direction_text} first, then re-check before moving forward.",
        ],
    }


def _choose_recovery_turn(
    path_profile: Optional[Dict[str, Any]],
    last_turn_direction: Optional[str] = None,
    preferred_turn: Optional[str] = None,
) -> str:
    for candidate in (
        preferred_turn,
        path_profile.get("preferred_turn") if path_profile else None,
        path_profile.get("open_side_turn") if path_profile else None,
    ):
        if candidate in ("turn_left", "turn_right"):
            return candidate
    if last_turn_direction == "turn_left":
        return "turn_right"
    if last_turn_direction == "turn_right":
        return "turn_left"
    return "turn_right"


def _blocked_turn_step_degrees(step_index: int, max_turn_deg: float) -> float:
    """Blocked-lane search turns sweep 45 -> 90 -> 135 -> 180 degrees."""
    staircase = (45.0, 90.0, 135.0, 180.0)
    bounded_index = max(1, int(step_index))
    target_turn = staircase[min(bounded_index - 1, len(staircase) - 1)]
    return round(min(max_turn_deg, target_turn), 1)


def _apply_repeat_turn_escalation(
    decision: Dict[str, Any],
    max_turn_deg: float,
    last_turn_direction: Optional[str],
    consecutive_turns: int = 0,
) -> Dict[str, Any]:
    """Apply the blocked-turn staircase: 45, then 90, then 135, then 180."""
    action = decision.get("action")
    if action not in ("turn_left", "turn_right"):
        return decision

    prior_same_direction_turns = consecutive_turns if last_turn_direction == action else 0
    turn_step = min(prior_same_direction_turns + 1, 4)
    current_turn = round(float(decision.get("turn_degrees") or 0.0), 1)
    staged_turn = _blocked_turn_step_degrees(turn_step, max_turn_deg)
    if abs(staged_turn - current_turn) <= 1e-6:
        return decision

    direction_text = "left" if action == "turn_left" else "right"
    return {
        **decision,
        "turn_degrees": staged_turn,
        "reason": (
            f"{decision.get('reason', '')} [turn-search-staircase: step {turn_step}/4, "
            f"so I turn {direction_text} {staged_turn:.0f} degrees.]"
        )[:240],
        "comment_front": (
            f"I am searching for a usable lane, so I use turn step {turn_step}: {staged_turn:.0f} degrees to the {direction_text}."
        )[:240],
        "plan_of_action": (
            f"I will turn {direction_text} {staged_turn:.0f} degrees, then check whether the center lane has opened."
        )[:240],
    }


def _apply_turn_commitment_override(
    decision: Dict[str, Any],
    history: List[Dict[str, Any]],
    clear_forward_lane: bool,
) -> Dict[str, Any]:
    """Avoid immediate left/right bouncing while searching for a new lane."""
    action = decision.get("action")
    if action not in ("turn_left", "turn_right") or clear_forward_lane or not history:
        return decision

    last_action = history[-1].get("action")
    if last_action not in ("turn_left", "turn_right") or last_action == action:
        return decision

    direction_text = "left" if last_action == "turn_left" else "right"
    return {
        **decision,
        "action": last_action,
        "reason": (
            f"{decision.get('reason', '')} [turn-commitment: I just turned {direction_text} and will keep "
            "searching that way instead of bouncing to the opposite side.]"
        )[:240],
        "comment_front": (
            f"My lane is still blocked, so I keep turning {direction_text} rather than undoing the previous search turn."
        )[:240],
        "plan_of_action": (
            f"I will continue turning {direction_text} to finish the search sweep, then re-check the lane."
        )[:240],
    }


def _build_recovery_turn_decision(
    path_profile: Optional[Dict[str, Any]],
    max_turn_deg: float,
    reason_tag: str,
    reason_text: str,
    comment_text: str,
    plan_text: str,
    reasoning_steps: List[str],
    preferred_turn: Optional[str] = None,
    last_turn_direction: Optional[str] = None,
    default_turn_deg: float = 70.0,
) -> Dict[str, Any]:
    action = _choose_recovery_turn(path_profile, last_turn_direction, preferred_turn)
    turn_degrees = round(min(max_turn_deg, default_turn_deg), 1)
    return {
        "action": action,
        "linear_speed": 0.0,
        "turn_degrees": turn_degrees,
        "duration_ms": 800,
        "confidence": 1.0,
        "reason": f"{reason_tag} {reason_text}"[:240],
        "comment_front": comment_text[:240],
        "comment_rear": "",
        "plan_of_action": plan_text[:240],
        "reasoning_steps": reasoning_steps[:4],
    }


def _apply_wall_escape_cycle_override(
    decision: Dict[str, Any],
    path_profile: Optional[Dict[str, Any]],
    recent_wall_escape_count: int,
    max_turn_deg: float,
) -> Dict[str, Any]:
    """Break repeated forward/backward wall-skim loops with a committed turn."""
    if (
        decision.get("action") != "forward"
        or not path_profile
        or recent_wall_escape_count < 2
    ):
        return decision

    forced_turn = path_profile.get("open_side_turn")
    if forced_turn not in ("turn_left", "turn_right"):
        return decision

    direction_text = "left" if forced_turn == "turn_left" else "right"
    return {
        **decision,
        "action": forced_turn,
        "linear_speed": 0.0,
        "turn_degrees": round(min(max_turn_deg, 65.0), 1),
        "duration_ms": decision.get("duration_ms", 700),
        "confidence": max(float(decision.get("confidence") or 0.0), 0.85),
        "reason": (
            f"[wall-escape-cycle-override] I have repeated wall escapes, so I turn {direction_text} "
            "to break the forward/backward loop."
        )[:240],
        "comment_front": (
            f"I am hugging a wall at close range; the {direction_text} side shows more open structure."
        )[:240],
        "plan_of_action": (
            f"I will turn {direction_text} to move away from the wall, then reassess before driving forward."
        )[:240],
        "reasoning_steps": [
            "I have just repeated the same forward-then-backward recovery cycle multiple times.",
            "That pattern means going straight is not clearing the nearby wall or obstacle.",
            f"The {direction_text} side shows more open structure than the opposite side.",
            f"I will turn {direction_text} now to break the loop and create a better forward angle.",
        ],
    }


def _apply_no_backward_policy(
    decision: Dict[str, Any],
    path_profile: Optional[Dict[str, Any]],
    max_turn_deg: float,
    last_turn_direction: Optional[str] = None,
) -> Dict[str, Any]:
    """Convert reverse recovery into turn-and-recheck recovery."""
    if decision.get("action") != "backward":
        return decision

    action = _choose_recovery_turn(path_profile, last_turn_direction)
    direction_text = "left" if action == "turn_left" else "right"
    return {
        **decision,
        "action": action,
        "linear_speed": 0.0,
        "turn_degrees": round(min(max_turn_deg, 70.0), 1),
        "duration_ms": decision.get("duration_ms", 800),
        "confidence": max(float(decision.get("confidence") or 0.0), 0.85),
        "reason": (
            f"[no-backward-policy] I turn {direction_text} to reacquire a valid lane instead of reversing."
        )[:240],
        "comment_front": (
            f"My forward lane is not valid right now, so I turn {direction_text} to search for a clearer path."
        )[:240],
        "plan_of_action": (
            f"I will turn {direction_text}, then re-check the immediate lane before moving again."
        )[:240],
        "reasoning_steps": [
            "My immediate lane is not reliable enough for another straight move.",
            "Instead of reversing, I will rotate to search for a usable forward lane.",
            f"The {direction_text} side is the best available recovery direction from recent structure.",
            f"I will turn {direction_text} now and then reassess the view.",
        ],
    }


def _build_local_forward_decision(
    max_linear: float,
    max_forward_ms: int,
    reason_text: str,
) -> Dict[str, Any]:
    return {
        "action": "forward",
        "linear_speed": round(min(max_linear, 0.25), 3),
        "turn_degrees": 0.0,
        "duration_ms": min(max_forward_ms, 700),
        "confidence": 0.95,
        "reason": reason_text[:240],
        "comment_front": "My immediate lane is clearly floor and safe for a short forward move."[:240],
        "comment_rear": "",
        "plan_of_action": "I will move forward briefly, then re-check the lane before choosing again."[:240],
        "reasoning_steps": [
            "The center lane looks like floor at immediate driving distance.",
            "The center lane does not look wall-like or blocked.",
            "Recent history does not indicate that I am repeating the same wall view.",
            "A short forward move is the safest way to make progress and re-evaluate.",
        ],
    }


def _has_clear_forward_lane(
    path_profile: Optional[Dict[str, Any]],
    bot_dist_to_floor: Optional[float],
    bot_dist_to_wall: Optional[float],
    uniformity: Optional[float],
    tb_delta: Optional[float],
) -> bool:
    """True when the current frame shows a genuinely usable short forward lane."""
    if not path_profile or path_profile.get("center_blocked"):
        return False

    center_floor = path_profile.get("center_floor_dist")
    center_wall = path_profile.get("center_wall_dist")

    obvious_clear = (
        center_floor is not None
        and center_wall is not None
        and center_floor <= 10.0
        and center_wall >= 30.0
        and (bot_dist_to_floor is None or bot_dist_to_floor <= 24.0)
        and (bot_dist_to_wall is None or bot_dist_to_wall >= bot_dist_to_floor + 2.0)
        and (uniformity is None or uniformity >= 10.0)
        and (tb_delta is None or tb_delta >= 8.0)
    )
    if obvious_clear:
        return True

    strong_center_profile = (
        center_floor is not None
        and center_floor <= 12.0
        and center_wall is not None
        and center_wall >= 25.0
        and (bot_dist_to_floor is None or bot_dist_to_floor <= 16.0)
        and (uniformity is None or uniformity >= 20.0)
    )
    if strong_center_profile:
        return True

    reopened_lane = (
        center_floor is not None
        and center_wall is not None
        and center_floor <= 26.0
        and center_wall >= 45.0
        and bot_dist_to_floor is not None
        and bot_dist_to_floor <= 16.0
        and bot_dist_to_wall is not None
        and bot_dist_to_wall >= bot_dist_to_floor + 14.0
        and (uniformity is None or uniformity >= 20.0)
        and (tb_delta is None or tb_delta >= 12.0)
    )
    if reopened_lane:
        return True

    visual_corridor = (
        path_profile.get("open_side_turn") is None
        and abs(float(path_profile.get("side_std_gap") or 0.0)) <= 6.0
        and center_wall is not None
        and center_wall >= 45.0
        and bot_dist_to_wall is not None
        and bot_dist_to_wall >= 25.0
        and (uniformity is None or 15.0 <= uniformity <= 40.0)
        and (tb_delta is None or tb_delta >= 10.0)
    )
    if visual_corridor:
        return True

    corridor_reopened = (
        path_profile.get("open_side_turn") is None
        and center_floor is not None
        and center_floor <= 34.0
        and center_wall is not None
        and center_wall >= 55.0
        and bot_dist_to_floor is not None
        and bot_dist_to_floor <= 34.0
        and bot_dist_to_wall is not None
        and bot_dist_to_wall >= bot_dist_to_floor + 20.0
        and (uniformity is None or uniformity >= 25.0)
        and (tb_delta is None or tb_delta >= 18.0)
    )
    return corridor_reopened


def _apply_visual_forward_override(
    decision: Dict[str, Any],
    clear_forward_lane: bool,
    max_linear: float,
    max_forward_ms: int,
) -> Dict[str, Any]:
    """If the current frame is clearly drivable, don't let a turn override that."""
    if decision.get("action") not in ("turn_left", "turn_right") or not clear_forward_lane:
        return decision

    return _build_local_forward_decision(
        max_linear=max_linear,
        max_forward_ms=max_forward_ms,
        reason_text=(
            "The current frame shows a usable forward corridor, so I move forward instead of turning."
        ),
    )


def _is_pressed_against_wall(
    uniformity: Optional[float],
    tb_delta: Optional[float],
    looks_like_wall_at_floor: bool,
    path_profile: Optional[Dict[str, Any]],
    bot_dist_to_floor: Optional[float],
    bot_dist_to_wall: Optional[float],
) -> bool:
    """True only when wall-close signals exist and we do not also have a strong forward lane."""
    raw_pressed = (
        (uniformity is not None and uniformity < 8.0)
        or (
            tb_delta is not None
            and tb_delta < 6.0
            and (uniformity is None or uniformity < 25.0)
        )
        or looks_like_wall_at_floor
    )
    if not raw_pressed:
        return False

    return not _has_clear_forward_lane(
        path_profile=path_profile,
        bot_dist_to_floor=bot_dist_to_floor,
        bot_dist_to_wall=bot_dist_to_wall,
        uniformity=uniformity,
        tb_delta=tb_delta,
    )


def _detect_side_opening_only_turn(
    path_profile: Optional[Dict[str, Any]],
    bot_dist_to_floor: Optional[float],
    bot_dist_to_wall: Optional[float],
    uniformity: Optional[float],
    tb_delta: Optional[float],
    color_sample_count: Optional[int] = None,
    recent_wall_escape_count: int = 0,
) -> Optional[str]:
    """Detect cases where only a side opening is viable and center is not convincingly drivable."""
    if not path_profile or path_profile.get("center_blocked"):
        return None

    open_side_turn = path_profile.get("open_side_turn")
    if open_side_turn not in ("turn_left", "turn_right"):
        return None

    center_floor = path_profile.get("center_floor_dist")
    center_wall = path_profile.get("center_wall_dist")
    side_std_gap = abs(float(path_profile.get("side_std_gap") or 0.0))

    if (
        center_floor is not None
        and center_floor >= 18.0
        and center_wall is not None
        and center_wall >= 55.0
        and bot_dist_to_floor is not None
        and bot_dist_to_floor >= 28.0
        and bot_dist_to_wall is not None
        and bot_dist_to_wall >= bot_dist_to_floor + 20.0
        and side_std_gap >= 20.0
        and (uniformity is None or uniformity >= 25.0)
        and (tb_delta is None or tb_delta >= 10.0)
    ):
        return open_side_turn

    early_uncertain_side_opening = (
        color_sample_count is not None
        and color_sample_count <= 2
        and recent_wall_escape_count >= 1
        and center_floor is not None
        and center_floor >= 30.0
        and center_wall is not None
        and center_wall >= 45.0
        and bot_dist_to_floor is not None
        and bot_dist_to_floor >= 26.0
        and side_std_gap >= 10.0
        and (uniformity is None or uniformity >= 20.0)
        and (tb_delta is None or tb_delta >= 12.0)
    )
    if early_uncertain_side_opening:
        return open_side_turn

    return None


def _decide_from_local_controller(
    path_profile: Optional[Dict[str, Any]],
    persistent_wall_turn: Optional[str],
    recent_wall_escape_count: int,
    color_sample_count: int,
    spin_detected: bool,
    max_linear: float,
    max_turn_deg: float,
    max_forward_ms: int,
    last_turn_direction: Optional[str],
    bot_dist_to_floor: Optional[float],
    bot_dist_to_wall: Optional[float],
    uniformity: Optional[float],
    tb_delta: Optional[float],
) -> Optional[Dict[str, Any]]:
    """Primary local controller. Returns None when the scene is genuinely ambiguous."""
    if path_profile and path_profile.get("center_blocked"):
        forced_turn = _choose_recovery_turn(
            path_profile, last_turn_direction, path_profile.get("preferred_turn")
        )
        direction_text = "left" if forced_turn == "turn_left" else "right"
        return _build_recovery_turn_decision(
            path_profile=path_profile,
            max_turn_deg=max_turn_deg,
            reason_tag="[local-center-block]",
            reason_text=(
                f"the immediate lane is blocked, so I turn {direction_text} toward the clearer side"
            ),
            comment_text=(
                f"A nearby barrier fills my immediate lane, so I turn {direction_text} instead of driving forward."
            ),
            plan_text=f"I will turn {direction_text}, then re-check for a valid center lane.",
            reasoning_steps=[
                "The immediate center lane is blocked at close range.",
                "Driving forward would push me into a barrier instead of through open floor.",
                f"The {direction_text} side is the best available recovery direction.",
                f"I will turn {direction_text} first and reassess the lane after the turn.",
            ],
            preferred_turn=forced_turn,
            last_turn_direction=last_turn_direction,
            default_turn_deg=70.0,
        )

    if persistent_wall_turn:
        direction_text = "left" if persistent_wall_turn == "turn_left" else "right"
        return _build_recovery_turn_decision(
            path_profile=path_profile,
            max_turn_deg=max_turn_deg,
            reason_tag="[local-persistent-wall]",
            reason_text=(
                f"recent forward frames stayed almost unchanged, so I turn {direction_text} off the wall"
            ),
            comment_text=(
                f"My recent forward views keep showing the same nearby wall, so I turn {direction_text}."
            ),
            plan_text=f"I will turn {direction_text} to change my angle on the wall, then reassess.",
            reasoning_steps=[
                "Several recent forward ticks produced nearly the same close wall view.",
                "That means I am not progressing into open space.",
                f"The repeated open side is to the {direction_text}.",
                f"I will turn {direction_text} now to find a usable forward lane.",
            ],
            preferred_turn=persistent_wall_turn,
            last_turn_direction=last_turn_direction,
            default_turn_deg=55.0,
        )

    if _has_clear_forward_lane(
        path_profile=path_profile,
        bot_dist_to_floor=bot_dist_to_floor,
        bot_dist_to_wall=bot_dist_to_wall,
        uniformity=uniformity,
        tb_delta=tb_delta,
    ):
        return _build_local_forward_decision(
            max_linear=max_linear,
            max_forward_ms=max_forward_ms,
            reason_text=(
                "The immediate lane has reopened with strong floor evidence, so a short forward move is safe."
            ),
        )

    side_opening_turn = _detect_side_opening_only_turn(
        path_profile=path_profile,
        bot_dist_to_floor=bot_dist_to_floor,
        bot_dist_to_wall=bot_dist_to_wall,
        uniformity=uniformity,
        tb_delta=tb_delta,
        color_sample_count=color_sample_count,
        recent_wall_escape_count=recent_wall_escape_count,
    )
    if side_opening_turn:
        direction_text = "left" if side_opening_turn == "turn_left" else "right"
        return _build_recovery_turn_decision(
            path_profile=path_profile,
            max_turn_deg=max_turn_deg,
            reason_tag="[local-side-opening-only]",
            reason_text=(
                f"the viable opening is to the {direction_text}; the center lane is not clear enough for forward"
            ),
            comment_text=(
                f"I see usable space to the {direction_text}, but the center lane is not convincingly open."
            ),
            plan_text=f"I will turn {direction_text} toward the side opening, then re-check the lane.",
            reasoning_steps=[
                "The center lane does not show strong enough floor evidence for a safe straight move.",
                "Most usable structure and opening appear on one side rather than in the center.",
                f"The better recovery direction is {direction_text}.",
                f"I will turn {direction_text} first and reassess before moving forward.",
            ],
            preferred_turn=side_opening_turn,
            last_turn_direction=last_turn_direction,
            default_turn_deg=60.0,
        )

    if recent_wall_escape_count >= 2 and path_profile and path_profile.get("open_side_turn"):
        forced_turn = path_profile.get("open_side_turn")
        direction_text = "left" if forced_turn == "turn_left" else "right"
        return _build_recovery_turn_decision(
            path_profile=path_profile,
            max_turn_deg=max_turn_deg,
            reason_tag="[local-wall-cycle]",
            reason_text=(
                f"recent wall recoveries repeat the same pattern, so I turn {direction_text} to break the loop"
            ),
            comment_text=(
                f"I am repeating the same wall recovery cycle, so I turn {direction_text} instead of probing forward."
            ),
            plan_text=f"I will turn {direction_text} decisively, then check the center lane again.",
            reasoning_steps=[
                "Recent history shows repeated recovery near the same wall.",
                "Another straight move would likely repeat the same non-progressing scene.",
                f"The best recovery direction is {direction_text}.",
                f"I will turn {direction_text} to create a new forward angle.",
            ],
            preferred_turn=forced_turn,
            last_turn_direction=last_turn_direction,
            default_turn_deg=65.0,
        )

    if spin_detected:
        forced_turn = _choose_recovery_turn(path_profile, last_turn_direction)
        direction_text = "left" if forced_turn == "turn_left" else "right"
        return _build_recovery_turn_decision(
            path_profile=path_profile,
            max_turn_deg=max_turn_deg,
            reason_tag="[local-spin-break]",
            reason_text=f"I turn {direction_text} decisively to break the turn loop",
            comment_text=(
                f"I have been turning without finding a lane, so I commit to a larger {direction_text} turn."
            ),
            plan_text=f"I will turn {direction_text} more decisively, then re-check the lane.",
            reasoning_steps=[
                "Recent actions show a turn loop without meaningful forward progress.",
                "A larger committed turn is better than another small adjustment.",
                f"The best recovery direction is {direction_text}.",
                f"I will turn {direction_text} and reassess the lane after the turn.",
            ],
            preferred_turn=forced_turn,
            last_turn_direction=last_turn_direction,
            default_turn_deg=80.0,
        )

    if not path_profile:
        return None

    if _has_clear_forward_lane(
        path_profile=path_profile,
        bot_dist_to_floor=bot_dist_to_floor,
        bot_dist_to_wall=bot_dist_to_wall,
        uniformity=uniformity,
        tb_delta=tb_delta,
    ):
        return _build_local_forward_decision(
            max_linear=max_linear,
            max_forward_ms=max_forward_ms,
            reason_text="The immediate center lane is clearly floor, so a short forward move is safe.",
        )

    return None


def _build_nav_signature(
    orientation: Any,
    uniformity: Optional[float],
    tb_delta: Optional[float],
    bot_dist_to_floor: Optional[float],
    bot_dist_to_wall: Optional[float],
    path_profile: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compact per-tick signature used to spot repeated wall-hugging frames."""
    return {
        "orientation": round(float(orientation), 1)
        if isinstance(orientation, (int, float))
        else None,
        "front_uniformity": round(float(uniformity), 1) if uniformity is not None else None,
        "front_tb_delta": round(float(tb_delta), 1) if tb_delta is not None else None,
        "bot_dist_to_floor": round(float(bot_dist_to_floor), 1)
        if bot_dist_to_floor is not None
        else None,
        "bot_dist_to_wall": round(float(bot_dist_to_wall), 1)
        if bot_dist_to_wall is not None
        else None,
        "open_side_turn": path_profile.get("open_side_turn") if path_profile else None,
        "center_floor_dist": path_profile.get("center_floor_dist") if path_profile else None,
        "center_wall_dist": path_profile.get("center_wall_dist") if path_profile else None,
        "left_wall_dist": path_profile.get("left_wall_dist") if path_profile else None,
        "right_wall_dist": path_profile.get("right_wall_dist") if path_profile else None,
    }


def _heading_delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    delta = abs(float(a) - float(b))
    if delta > 180:
        delta = 360 - delta
    return delta


def _signatures_match_for_wall_loop(
    current_sig: Dict[str, Any],
    previous_sig: Dict[str, Any],
) -> bool:
    if not current_sig or not previous_sig:
        return False
    if not current_sig.get("open_side_turn"):
        return False
    if current_sig.get("open_side_turn") != previous_sig.get("open_side_turn"):
        return False

    heading_delta = _heading_delta(
        current_sig.get("orientation"), previous_sig.get("orientation")
    )
    if heading_delta is not None and heading_delta > 12.0:
        return False

    checks = (
        ("front_uniformity", 2.5),
        ("front_tb_delta", 3.5),
        ("bot_dist_to_floor", 3.0),
        ("bot_dist_to_wall", 3.0),
        ("center_floor_dist", 3.0),
        ("center_wall_dist", 6.0),
    )
    compared = 0
    matched = 0
    for key, threshold in checks:
        cur = current_sig.get(key)
        prev = previous_sig.get(key)
        if cur is None or prev is None:
            continue
        compared += 1
        if abs(float(cur) - float(prev)) <= threshold:
            matched += 1
    return compared >= 4 and matched >= 4


def _detect_persistent_wall_ahead_turn(
    history: List[Dict[str, Any]],
    current_sig: Dict[str, Any],
) -> Optional[str]:
    """If repeated forward ticks look visually unchanged, stop treating them as progress."""
    if not current_sig.get("open_side_turn"):
        return None

    forward_streak: List[Dict[str, Any]] = []
    for item in reversed(history):
        if item.get("action") != "forward":
            break
        forward_streak.append(item)
        if len(forward_streak) >= 4:
            break

    if len(forward_streak) < 3:
        return None

    matched = 0
    for item in forward_streak:
        prev_sig = item.get("nav_signature") or {}
        if _signatures_match_for_wall_loop(current_sig, prev_sig):
            matched += 1

    if matched >= 2:
        return current_sig.get("open_side_turn")
    return None


def _apply_persistent_wall_ahead_override(
    decision: Dict[str, Any],
    forced_turn: Optional[str],
    max_turn_deg: float,
) -> Dict[str, Any]:
    """Use recent history to break repeated forward commands into a nearby wall."""
    if decision.get("action") != "forward" or forced_turn not in ("turn_left", "turn_right"):
        return decision

    direction_text = "left" if forced_turn == "turn_left" else "right"
    return {
        **decision,
        "action": forced_turn,
        "linear_speed": 0.0,
        "turn_degrees": round(min(max_turn_deg, 55.0), 1),
        "duration_ms": decision.get("duration_ms", 700),
        "confidence": max(float(decision.get("confidence") or 0.0), 0.9),
        "reason": (
            f"[persistent-wall-ahead-override] Recent forward frames stayed visually unchanged, "
            f"so I turn {direction_text} instead of driving into the same nearby wall."
        )[:240],
        "comment_front": (
            f"My recent forward views keep showing the same nearby wall; the {direction_text} side is the repeat opening."
        )[:240],
        "plan_of_action": (
            f"I will turn {direction_text} to change my angle on the wall, then reassess the lane."
        )[:240],
        "reasoning_steps": [
            "Several recent forward ticks produced nearly the same close-range view instead of a new scene.",
            "That means I am still facing the same nearby wall rather than progressing into open space.",
            f"The repeated open side is to the {direction_text}.",
            f"I will turn {direction_text} now to get off the wall and find a usable forward lane.",
        ],
    }


def _apply_history_forward_turn_overrides(
    decision: Dict[str, Any],
    path_profile: Optional[Dict[str, Any]],
    recent_wall_escape_count: int,
    persistent_wall_turn: Optional[str],
    max_turn_deg: float,
    clear_forward_lane: bool,
) -> Dict[str, Any]:
    """Use history-based turn overrides only when the current frame is not clearly drivable."""
    if decision.get("action") != "forward" or clear_forward_lane:
        return decision

    if recent_wall_escape_count >= 2:
        decision = _apply_wall_escape_cycle_override(
            decision, path_profile, recent_wall_escape_count, max_turn_deg
        )

    if persistent_wall_turn and decision.get("action") == "forward":
        decision = _apply_persistent_wall_ahead_override(
            decision, persistent_wall_turn, max_turn_deg
        )

    return decision


def _apply_side_opening_only_override(
    decision: Dict[str, Any],
    path_profile: Optional[Dict[str, Any]],
    bot_dist_to_floor: Optional[float],
    bot_dist_to_wall: Optional[float],
    uniformity: Optional[float],
    tb_delta: Optional[float],
    color_sample_count: Optional[int],
    recent_wall_escape_count: int,
    max_turn_deg: float,
    last_turn_direction: Optional[str] = None,
) -> Dict[str, Any]:
    """If only a side opening is viable, prevent forward motion into the obstruction."""
    if decision.get("action") != "forward":
        return decision

    forced_turn = _detect_side_opening_only_turn(
        path_profile=path_profile,
        bot_dist_to_floor=bot_dist_to_floor,
        bot_dist_to_wall=bot_dist_to_wall,
        uniformity=uniformity,
        tb_delta=tb_delta,
        color_sample_count=color_sample_count,
        recent_wall_escape_count=recent_wall_escape_count,
    )
    if forced_turn not in ("turn_left", "turn_right"):
        return decision

    direction_text = "left" if forced_turn == "turn_left" else "right"
    return _build_recovery_turn_decision(
        path_profile=path_profile,
        max_turn_deg=max_turn_deg,
        reason_tag="[side-opening-only-override]",
        reason_text=(
            f"the center lane is not clear enough, so I turn {direction_text} toward the side opening"
        ),
        comment_text=(
            f"I do not have a convincing straight lane; the usable opening is to the {direction_text}."
        ),
        plan_text=f"I will turn {direction_text}, then re-check whether forward is truly open.",
        reasoning_steps=[
            "The center lane lacks strong floor evidence for a safe short forward move.",
            "The most usable opening is on one side instead of straight ahead.",
            f"The safer recovery direction is {direction_text}.",
            f"I will turn {direction_text} first and then reassess the lane.",
        ],
        preferred_turn=forced_turn,
        last_turn_direction=last_turn_direction,
        default_turn_deg=60.0,
    )


def _rgb_distance(a: Optional[List[float]], b: Optional[List[float]]) -> Optional[float]:
    """Euclidean distance between two RGB triples on the 0-255 scale. None if
    either is missing."""
    if not a or not b or len(a) != 3 or len(b) != 3:
        return None
    import math
    return math.sqrt(sum((float(a[i]) - float(b[i])) ** 2 for i in range(3)))


async def _run_autonav_loop(config: Dict[str, Any]):
    tick_interval = max(0.3, float(config["tick_ms"]) / 1000.0)
    max_linear = float(config["max_linear"])
    max_turn_deg = float(config["max_turn_deg"])
    max_forward_ms = int(config["max_forward_ms"])
    battery_floor = int(config["battery_floor"])
    history_size = int(config["history_size"])
    max_errors = int(config["max_errors"])
    model = config.get("model") or None

    autonav_loop_state["status"] = "waiting"
    last_turn_direction: Optional[str] = None
    consecutive_turns = 0
    log_dir: Optional[str] = autonav_loop_state.get("log_dir")

    logger.info(
        "Autonav loop started (tick=%.2fs, max_linear=%.2f, max_turn_deg=%.1f)",
        tick_interval,
        max_linear,
        max_turn_deg,
    )

    try:
        while True:
            tick_start = asyncio.get_event_loop().time()
            iteration = autonav_loop_state["iterations"] + 1
            autonav_loop_state["iterations"] = iteration

            # 1. Telemetry -----------------------------------------------------
            try:
                rover_data = await browser_service.data()
            except Exception as exc:
                autonav_loop_state["error_streak"] = autonav_loop_state.get("error_streak", 0) + 1
                autonav_loop_state["last_error"] = f"telemetry: {exc}"
                logger.warning("Autonav telemetry error: %s", exc)
                if autonav_loop_state["error_streak"] >= max_errors:
                    autonav_loop_state["status"] = "error"
                    await _autonav_stop_burst()
                    break
                await asyncio.sleep(tick_interval)
                continue

            battery = rover_data.get("battery")
            try:
                battery_val = int(battery) if battery is not None else None
            except (TypeError, ValueError):
                battery_val = None

            if battery_val is not None and battery_val <= battery_floor:
                autonav_loop_state["status"] = "battery_low"
                autonav_loop_state["last_error"] = f"battery {battery_val}% <= floor {battery_floor}"
                logger.warning("Autonav stopping: battery %s%% <= floor %s%%", battery_val, battery_floor)
                await _autonav_stop_burst()
                break

            # 2. Fetch frames --------------------------------------------------
            autonav_loop_state["status"] = "deciding"
            history = autonav_loop_state["history"]

            recent = [h.get("action") for h in history[-5:]]
            recent_wall_escape_count = sum(
                1
                for item in history[-6:]
                if str(item.get("reason", "")).startswith("[wall-proximity ")
            )
            recent_turn_count = sum(1 for a in recent if a in ("turn_left", "turn_right"))
            recent_has_forward = any(a == "forward" for a in recent)
            # Note: we do NOT check observed_speed anymore — this rover's
            # speed telemetry is GPS-derived and reads near-zero whenever
            # GPS is unavailable, even while the rover is clearly moving.
            # Trust the action pattern, not the speed metric.
            spin_detected = (
                len(recent) >= 4 and recent_turn_count >= 4 and not recent_has_forward
            )

            try:
                front_b64 = await get_frame_base64("front")
            except Exception as exc:
                autonav_loop_state["error_streak"] = autonav_loop_state.get("error_streak", 0) + 1
                autonav_loop_state["last_error"] = f"front frame: {exc}"
                logger.warning("Autonav front-frame error: %s", exc)
                if autonav_loop_state["error_streak"] >= max_errors:
                    autonav_loop_state["status"] = "error"
                    await _autonav_stop_burst()
                    break
                await asyncio.sleep(tick_interval)
                continue

            uniformity = _frame_uniformity(front_b64)
            tb_delta = _frame_top_bottom_delta(front_b64)
            color_sample = _frame_color_samples(front_b64)

            # Color calibration: on frames with a clear horizon (tb_delta >= 12),
            # blend the current sample into the running floor/wall baseline.
            # Cap at MAX_COLOR_SAMPLES so early readings dominate (the first few
            # normal ticks define "what floor/wall look like in this run").
            MAX_COLOR_SAMPLES = 6
            if (
                color_sample
                and tb_delta is not None
                and tb_delta >= 12.0
                and autonav_loop_state.get("color_sample_count", 0) < MAX_COLOR_SAMPLES
            ):
                n = autonav_loop_state.get("color_sample_count", 0)
                prev_floor = autonav_loop_state.get("floor_rgb")
                prev_wall = autonav_loop_state.get("wall_rgb")
                if prev_floor is None:
                    autonav_loop_state["floor_rgb"] = list(color_sample["bot_rgb"])
                    autonav_loop_state["wall_rgb"] = list(color_sample["top_rgb"])
                else:
                    autonav_loop_state["floor_rgb"] = [
                        round((prev_floor[i] * n + color_sample["bot_rgb"][i]) / (n + 1), 1)
                        for i in range(3)
                    ]
                    autonav_loop_state["wall_rgb"] = [
                        round((prev_wall[i] * n + color_sample["top_rgb"][i]) / (n + 1), 1)
                        for i in range(3)
                    ]
                autonav_loop_state["color_sample_count"] = n + 1
                if n == 0:
                    logger.info(
                        "Autonav color calibration started: floor=%s wall=%s",
                        autonav_loop_state["floor_rgb"],
                        autonav_loop_state["wall_rgb"],
                    )

            # Per-tick comparison against the learned baseline.
            floor_rgb = autonav_loop_state.get("floor_rgb")
            wall_rgb = autonav_loop_state.get("wall_rgb")
            bot_dist_to_floor = _rgb_distance(
                color_sample["bot_rgb"] if color_sample else None, floor_rgb
            )
            top_dist_to_wall = _rgb_distance(
                color_sample["top_rgb"] if color_sample else None, wall_rgb
            )
            bot_dist_to_wall = _rgb_distance(
                color_sample["bot_rgb"] if color_sample else None, wall_rgb
            )
            # Multi-signal detection for "nose-pressed against a surface":
            #   (a) truly flat frame: stddev < 8 — e.g. pure white wall.
            #   (b) textured but single-surface: small top/bottom mean delta
            #       (<6) combined with moderate stddev.
            #   (c) calibrated mismatch: the bottom half of the frame is
            #       closer in color to the learned WALL than to the learned
            #       FLOOR — meaning where floor should be, we see wall.
            looks_like_wall_at_floor = False
            if (
                color_sample
                and bot_dist_to_wall is not None
                and bot_dist_to_floor is not None
                and autonav_loop_state.get("color_sample_count", 0) >= 2
            ):
                # Needs a clear bias toward wall over floor to trip — not just
                # a lighting shift. Require bot is significantly closer to wall
                # AND far enough from floor to avoid false positives.
                looks_like_wall_at_floor = (
                    bot_dist_to_wall < bot_dist_to_floor - 15
                    and bot_dist_to_floor > 25
                )
            path_zone_b64 = _frame_path_crop_base64(front_b64)
            path_profile = _frame_path_profile(front_b64, floor_rgb, wall_rgb)
            pressed_against_wall = _is_pressed_against_wall(
                uniformity=uniformity,
                tb_delta=tb_delta,
                looks_like_wall_at_floor=looks_like_wall_at_floor,
                path_profile=path_profile,
                bot_dist_to_floor=bot_dist_to_floor,
                bot_dist_to_wall=bot_dist_to_wall,
            )
            path_profile_summary = _format_path_profile_summary(path_profile)
            current_nav_signature = _build_nav_signature(
                rover_data.get("orientation"),
                uniformity,
                tb_delta,
                bot_dist_to_floor,
                bot_dist_to_wall,
                path_profile,
            )
            persistent_wall_turn = _detect_persistent_wall_ahead_turn(
                history, current_nav_signature
            )
            clear_forward_lane = _has_clear_forward_lane(
                path_profile=path_profile,
                bot_dist_to_floor=bot_dist_to_floor,
                bot_dist_to_wall=bot_dist_to_wall,
                uniformity=uniformity,
                tb_delta=tb_delta,
            )

            want_rear = pressed_against_wall
            if history and not want_rear:
                last_action = history[-1].get("action")
                if last_action == "backward" or spin_detected:
                    want_rear = True

            rear_b64: Optional[str] = None
            if want_rear and auth_response_data.get("BOT_TYPE") == "zero":
                try:
                    rear_b64 = await get_frame_base64("rear")
                except Exception as exc:
                    logger.info("Autonav rear frame unavailable (continuing without): %s", exc)
                    rear_b64 = None

            # 3. Hint for loop/spin detection ---------------------------------
            hint_parts: List[str] = []
            if clear_forward_lane and (
                spin_detected
                or recent_wall_escape_count >= 2
                or persistent_wall_turn is not None
            ):
                hint_parts.append(
                    "Visual priority: the current images show a reopened center lane. If the center remains clearly open in front of me, prefer forward over stale history."
                )
            elif spin_detected:
                hint_parts.append(
                    f"You have turned {recent_turn_count} of the last {len(recent)} ticks with no "
                    "forward progress. This tick, do not reverse. Pick ONE direction and use the "
                    "blocked-lane turn staircase: 45°, then 90°, then 135°, then 180° if needed."
                )
            elif consecutive_turns >= 3 and last_turn_direction:
                hint_parts.append(
                    f"You have chosen {last_turn_direction} {consecutive_turns} times in a row. "
                    "Break the spin with the next blocked-lane turn step: 45°, then 90°, then 135°, then 180°."
                )
            if path_profile and path_profile.get("center_blocked"):
                preferred_turn = path_profile.get("preferred_turn")
                preferred_text = ""
                if preferred_turn == "turn_left":
                    preferred_text = " Prefer the left side."
                elif preferred_turn == "turn_right":
                    preferred_text = " Prefer the right side."
                hint_parts.append(
                    "Local path guardrail: the immediate center lane is blocked by a nearby surface. "
                    "Do NOT choose forward on this tick; choose a turn toward the more open side."
                    + preferred_text
                )
            if recent_wall_escape_count >= 2 and path_profile and not clear_forward_lane:
                open_side_turn = path_profile.get("open_side_turn")
                open_side_text = ""
                if open_side_turn == "turn_left":
                    open_side_text = " Prefer a left turn."
                elif open_side_turn == "turn_right":
                    open_side_text = " Prefer a right turn."
                hint_parts.append(
                    "Recent wall-proximity escapes show I am stuck in a forward/backward loop near a wall. "
                    "Break the cycle with a committed turn instead of another forward probe."
                    + open_side_text
                )
            if pressed_against_wall:
                wall_text = "left" if persistent_wall_turn == "turn_left" else "right" if persistent_wall_turn == "turn_right" else None
                preferred_text = f" Prefer turning {wall_text}." if wall_text else ""
                hint_parts.append(
                    "Visual guardrail: the current front frame looks nose-close to a wall or obstacle. "
                    "Base this decision on what is visible in the current images and avoid forward unless the center lane is clearly open."
                    + preferred_text
                )
            if persistent_wall_turn and not clear_forward_lane:
                direction_text = "left" if persistent_wall_turn == "turn_left" else "right"
                hint_parts.append(
                    "History guardrail: several recent forward ticks produced an almost unchanged close wall view, "
                    f"so this is not a real clear path. Turn {direction_text} instead of choosing forward again."
                )
            hint: Optional[str] = " ".join(hint_parts) if hint_parts else None

            # 4. Ask Gemini ----------------------------------------------------
            gemini_debug: Dict[str, Any] = {}
            decision_source = "gemini"
            try:
                decision = await autonav_decide(
                    front_b64=front_b64,
                    rear_b64=rear_b64,
                    front_path_b64=path_zone_b64,
                    telemetry={
                        "orientation": rover_data.get("orientation"),
                        "speed": rover_data.get("speed"),
                        "battery": battery_val,
                        "front_uniformity": (
                            round(uniformity, 1) if uniformity is not None else None
                        ),
                        "front_tb_delta": (
                            round(tb_delta, 1) if tb_delta is not None else None
                        ),
                        "learned_floor_rgb": floor_rgb,
                        "learned_wall_rgb": wall_rgb,
                        "current_bot_rgb": color_sample["bot_rgb"] if color_sample else None,
                        "current_top_rgb": color_sample["top_rgb"] if color_sample else None,
                        "bot_dist_to_floor": (
                            round(bot_dist_to_floor, 1) if bot_dist_to_floor is not None else None
                        ),
                        "bot_dist_to_wall": (
                            round(bot_dist_to_wall, 1) if bot_dist_to_wall is not None else None
                        ),
                        "path_profile_summary": path_profile_summary,
                    },
                    history=history,
                    max_linear=max_linear,
                    max_turn_deg=max_turn_deg,
                    max_forward_ms=max_forward_ms,
                    hint=hint,
                    model=model,
                    debug_out=gemini_debug,
                )
                gemini_raw_decision = dict(decision)
            except Exception as exc:
                autonav_loop_state["error_streak"] = autonav_loop_state.get("error_streak", 0) + 1
                autonav_loop_state["last_error"] = f"gemini: {exc}"
                logger.warning("Autonav Gemini error: %s", exc)
                if autonav_loop_state["error_streak"] >= max_errors:
                    autonav_loop_state["status"] = "error"
                    await _autonav_stop_burst()
                    break
                await asyncio.sleep(tick_interval)
                continue

            # Speed-based stuck override removed: on rovers with unreliable
            # speed telemetry (e.g. GPS-derived when GPS is down), this
            # triggered constant false positives and fought with Gemini.

            # Contradiction override: if Gemini picks a turn but admits in its own
            # reason/comment that the immediate path is clear, force forward.
            # Observed failure mode: Gemini says "IMAGE 2 is clear" and still picks
            # turn_left because it's anticipating a future corridor bend. Anticipatory
            # turning is wrong; each forward burst should be followed by re-evaluation.
            if decision["action"] in ("turn_left", "turn_right"):
                text_blob = " ".join(
                    [
                        str(decision.get("reason", "")),
                        str(decision.get("comment_front", "")),
                        " ".join(decision.get("reasoning_steps") or []),
                    ]
                ).lower()
                admits_clear = any(
                    phrase in text_blob
                    for phrase in (
                        "immediate path is clear",
                        "immediate forward path is clear",
                        "immediate forward path appears clear",
                        "immediate forward path appears mostly clear",
                        "forward path is clear",
                        "path is clear of obstacles",
                        "no immediate obstacle",
                        "no immediate obstacles",
                        "no obstacles directly",
                        "clear path directly ahead",
                        "center-bottom is clear",
                        "center of the frame is clear",
                    )
                )
                if admits_clear:
                    logger.info(
                        "Autonav contradiction override: Gemini picked %s but admitted "
                        "immediate path is clear — forcing forward",
                        decision["action"],
                    )
                    decision = dict(decision)
                    decision["action"] = "forward"
                    decision["linear_speed"] = min(max(0.18, 0.15), max_linear)
                    decision["duration_ms"] = 700
                    decision["turn_degrees"] = 0.0
                    decision["reason"] = (
                        (decision.get("reason", "") + " [contradiction-override: forward]").strip()
                    )

            if decision["action"] in ("turn_left", "turn_right") and clear_forward_lane:
                logger.info(
                    "Autonav visual-forward override: current frame is clearly drivable, so overriding %s to forward",
                    decision["action"],
                )
                decision = _apply_visual_forward_override(
                    decision=decision,
                    clear_forward_lane=clear_forward_lane,
                    max_linear=max_linear,
                    max_forward_ms=max_forward_ms,
                )

            if path_profile and path_profile.get("center_blocked") and decision["action"] == "forward":
                logger.info(
                    "Autonav center-block override: Gemini picked forward with blocked lane "
                    "(preferred_turn=%s)",
                    path_profile.get("preferred_turn"),
                )
                decision = _apply_center_block_override(decision, path_profile, max_turn_deg)

            if decision["action"] == "forward":
                side_opening_turn = _detect_side_opening_only_turn(
                    path_profile=path_profile,
                    bot_dist_to_floor=bot_dist_to_floor,
                    bot_dist_to_wall=bot_dist_to_wall,
                    uniformity=uniformity,
                    tb_delta=tb_delta,
                    color_sample_count=autonav_loop_state.get("color_sample_count", 0),
                    recent_wall_escape_count=recent_wall_escape_count,
                )
                if side_opening_turn:
                    logger.info(
                        "Autonav side-opening-only override: Gemini picked forward with only side opening "
                        "(turn=%s)",
                        side_opening_turn,
                    )
                    decision = _apply_side_opening_only_override(
                        decision,
                        path_profile,
                        bot_dist_to_floor,
                        bot_dist_to_wall,
                        uniformity,
                        tb_delta,
                        autonav_loop_state.get("color_sample_count", 0),
                        recent_wall_escape_count,
                        max_turn_deg,
                        last_turn_direction,
                    )

            if decision["action"] == "forward":
                if clear_forward_lane and (
                    recent_wall_escape_count >= 2 or persistent_wall_turn
                ):
                    logger.info(
                        "Autonav preserving forward: current frame is clearly drivable, so stale history overrides are skipped"
                    )
                elif recent_wall_escape_count >= 2 or persistent_wall_turn:
                    logger.info(
                        "Autonav history-forward override check: recent_wall_escape_count=%s persistent_wall_turn=%s",
                        recent_wall_escape_count,
                        persistent_wall_turn,
                    )
                decision = _apply_history_forward_turn_overrides(
                    decision=decision,
                    path_profile=path_profile,
                    recent_wall_escape_count=recent_wall_escape_count,
                    persistent_wall_turn=persistent_wall_turn,
                    max_turn_deg=max_turn_deg,
                    clear_forward_lane=clear_forward_lane,
                )

            if decision["action"] == "backward":
                logger.info(
                    "Autonav no-backward policy: converting backward into turn recovery "
                    "(Gemini picked %s)",
                    decision["action"],
                )
                decision = _apply_no_backward_policy(
                    decision, path_profile, max_turn_deg, last_turn_direction
                )

            if decision["action"] in ("turn_left", "turn_right"):
                committed = _apply_turn_commitment_override(
                    decision=decision,
                    history=history,
                    clear_forward_lane=clear_forward_lane,
                )
                if committed["action"] != decision["action"]:
                    logger.info(
                        "Autonav turn-commitment override: preserving %s instead of bouncing to %s",
                        committed["action"],
                        decision["action"],
                    )
                decision = committed

            if spin_detected and decision["action"] in ("turn_left", "turn_right"):
                # Spin override: rover is dancing left/right without progress.
                logger.info(
                    "Autonav spin override: %s turns in last %s ticks with no forward — forcing committed turn",
                    recent_turn_count,
                    len(recent),
                )
                action = _choose_recovery_turn(
                    path_profile, last_turn_direction, decision["action"]
                )
                direction_text = "left" if action == "turn_left" else "right"
                decision = {
                    **decision,
                    "action": action,
                    "linear_speed": 0.0,
                    "turn_degrees": round(min(max_turn_deg, 80.0), 1),
                    "reason": (
                        f"[spin-override] I commit to a larger {direction_text} turn to break the loop."
                    )[:240],
                    "comment_front": (
                        f"I have been turning without finding a lane, so I commit to a larger {direction_text} turn."
                    )[:240],
                    "plan_of_action": (
                        f"I will turn {direction_text} more decisively, then check the lane again."
                    )[:240],
                    "reasoning_steps": [
                        "Recent decisions show repeated turning without meaningful forward progress.",
                        "A bigger committed turn is more useful than another small adjustment.",
                        f"The best recovery direction is {direction_text}.",
                        f"I will turn {direction_text} now and reassess the forward lane.",
                    ],
                }

            decision = _apply_repeat_turn_escalation(
                decision, max_turn_deg, last_turn_direction, consecutive_turns
            )

            autonav_loop_state["error_streak"] = 0
            autonav_loop_state["last_error"] = None
            autonav_loop_state["last_decision"] = decision
            autonav_loop_state["last_action_at"] = datetime.utcnow().isoformat() + "Z"
            autonav_loop_state["status"] = "acting"

            action = decision["action"]
            logger.info(
                "Autonav tick %s: %s (reason=%r)",
                iteration,
                action,
                decision.get("reason", "")[:120],
            )

            # 5. Execute action ------------------------------------------------
            observed_speed_val: Optional[float] = None
            try:
                if action in ("forward", "backward"):
                    # Rover has a minimum usable linear speed — commands below
                    # ~0.12 m/s don't overcome motor stiction. The existing
                    # turn helper (main.py:574) uses the same 0.12 floor.
                    MIN_EFFECTIVE_SPEED = 0.12
                    requested_speed = decision["linear_speed"]
                    speed = min(
                        max(requested_speed, MIN_EFFECTIVE_SPEED), max_linear
                    )
                    if speed > requested_speed + 1e-6:
                        logger.debug(
                            "Autonav: raised %s speed %.2f → %.2f (stiction floor)",
                            action, requested_speed, speed,
                        )
                    sign = 1 if action == "forward" else -1
                    duration = decision["duration_ms"] / 1000.0
                    cmd = {"linear": sign * speed, "angular": 0, "lamp": 0}
                    # Rover RTM protocol has a watchdog — commands must be
                    # refreshed every ~350ms or the rover stops on its own.
                    # This is why /turn (main.py:742) resends at ~350ms.
                    # Refresh the drive command throughout the burst AND sample
                    # speed, taking the max observed speed as ground truth.
                    REFRESH = 0.25  # keep below rover's ~350ms watchdog
                    LOOP_DT = 0.1
                    await browser_service.send_message(cmd)
                    last_send = asyncio.get_event_loop().time()
                    end_time = last_send + duration
                    max_observed = 0.0
                    sample_count = 0
                    # Tight loop that both refreshes the drive command and
                    # samples speed. No warm-up sleep — keep pinging.
                    while asyncio.get_event_loop().time() < end_time - 0.03:
                        now = asyncio.get_event_loop().time()
                        if now - last_send >= REFRESH:
                            await browser_service.send_message(cmd)
                            last_send = now
                        try:
                            mid_data = await browser_service.data()
                            raw_speed = mid_data.get("speed")
                            if raw_speed is not None:
                                max_observed = max(max_observed, abs(float(raw_speed)))
                                sample_count += 1
                        except Exception:
                            pass
                        await asyncio.sleep(LOOP_DT)
                    observed_speed_val = max_observed if sample_count > 0 else None
                    remaining = end_time - asyncio.get_event_loop().time()
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                    await _autonav_stop_burst()
                elif action == "turn_left":
                    await _perform_turn(-decision["turn_degrees"])
                elif action == "turn_right":
                    await _perform_turn(decision["turn_degrees"])
                else:  # stop
                    await _autonav_stop_burst()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                autonav_loop_state["last_error"] = f"execute: {exc}"
                logger.warning("Autonav execute error: %s", exc)
                await _autonav_stop_burst()

            # Settle wait: let the rover fully stop and the RTC video stream
            # catch up with the new pose before the next tick grabs a frame.
            # Without this, the "front frame" fetched at the top of tick N+1
            # can still be a mid-motion frame from this tick's action.
            await asyncio.sleep(0.4)

            # 6. Update spin trackers -----------------------------------------
            post_speed_val = observed_speed_val

            if action in ("turn_left", "turn_right"):
                if last_turn_direction == action:
                    consecutive_turns += 1
                else:
                    consecutive_turns = 1
                last_turn_direction = action
            else:
                consecutive_turns = 0
                last_turn_direction = None

            history.append(
                {
                    "tick": iteration,
                    "action": action,
                    "linear_speed": decision.get("linear_speed"),
                    "turn_degrees": decision.get("turn_degrees"),
                    "duration_ms": decision.get("duration_ms"),
                    "speed_after": post_speed_val,
                    "reason": decision.get("reason", "")[:160],
                    "nav_signature": current_nav_signature,
                }
            )
            if len(history) > history_size:
                del history[: len(history) - history_size]

            _autonav_write_tick(
                log_dir,
                iteration,
                {
                    "tick": iteration,
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "path": decision_source,
                    "telemetry": {
                        "battery": battery_val,
                        "orientation": rover_data.get("orientation"),
                        "speed": rover_data.get("speed"),
                    },
                    "front_uniformity": uniformity,
                    "front_tb_delta": tb_delta,
                    "learned_floor_rgb": floor_rgb,
                    "learned_wall_rgb": wall_rgb,
                    "current_bot_rgb": color_sample["bot_rgb"] if color_sample else None,
                    "current_top_rgb": color_sample["top_rgb"] if color_sample else None,
                    "bot_dist_to_floor": bot_dist_to_floor,
                    "bot_dist_to_wall": bot_dist_to_wall,
                    "path_zone_sent": path_zone_b64 is not None,
                    "path_profile": path_profile,
                    "color_sample_count": autonav_loop_state.get("color_sample_count", 0),
                    "rear_fetched": rear_b64 is not None,
                    "spin_detected": spin_detected,
                    "recent_turn_count": recent_turn_count,
                    "recent_wall_escape_count": recent_wall_escape_count,
                    "persistent_wall_turn": persistent_wall_turn,
                    "nav_signature": current_nav_signature,
                    "hint": hint,
                    "system_prompt_sha": None,  # constant across ticks; see run.json
                    "user_prompt": gemini_debug.get("user_prompt"),
                    "model": gemini_debug.get("model"),
                    "gemini_raw_decision": gemini_raw_decision,
                    "executed_decision": decision,
                    "observed_speed_mid_motion": observed_speed_val,
                },
                front_b64,
                rear_b64,
                path_zone_b64,
            )

            # 7. Pace to tick_interval
            autonav_loop_state["status"] = "waiting"
            elapsed = asyncio.get_event_loop().time() - tick_start
            sleep_for = tick_interval - elapsed
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

    except asyncio.CancelledError:
        logger.info("Autonav loop cancelled — issuing stop burst")
        try:
            await _autonav_stop_burst()
        except Exception as exc:
            logger.warning("Autonav stop burst on cancel failed: %s", exc)
        raise
    except Exception as exc:
        logger.error("Autonav loop crashed: %s", exc)
        autonav_loop_state["status"] = "error"
        autonav_loop_state["last_error"] = str(exc)
        try:
            await _autonav_stop_burst()
        except Exception:
            pass
    finally:
        autonav_loop_state["running"] = False


async def _stop_autonav_loop_task(reason: str) -> bool:
    global autonav_loop_task
    task = autonav_loop_task
    if not task or task.done():
        autonav_loop_task = None
        autonav_loop_state["running"] = False
        return False

    autonav_loop_state["running"] = False
    autonav_loop_state["status"] = reason
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.error("Autonav loop stop error: %s", exc)

    autonav_loop_task = None
    logger.info("Autonav loop stopped (%s)", reason)
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
    async with autonav_loop_lock:
        await _stop_autonav_loop_task("shutdown")
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
# Autonomous Navigation endpoints
# ---------------------------------------------------------------------------

@app.post("/autonav/start")
async def start_autonav(request: Request):
    """Start the autonomous maze-navigation loop (Gemini Flash closed-loop driver).

    Body (all optional):
      tick_ms, max_linear, max_turn_deg, max_forward_ms,
      battery_floor, history_size, max_errors, gps_trail_size
    """
    global autonav_loop_task
    await need_start_mission()
    if not auth_response_data:
        await auth()

    body = await _parse_json_body(request)

    def _cfg(key: str, env_key: str, default, cast):
        raw = body.get(key)
        if raw is None:
            raw = os.getenv(env_key, default)
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return cast(default)

    config = {
        "tick_ms": _cfg("tick_ms", "AUTONAV_TICK_MS", 1500, int),
        "max_linear": _cfg("max_linear", "AUTONAV_MAX_LINEAR", 0.25, float),
        "max_turn_deg": _cfg("max_turn_deg", "AUTONAV_MAX_TURN_DEG", 180, float),
        "max_forward_ms": _cfg("max_forward_ms", "AUTONAV_MAX_FORWARD_MS", 3000, int),
        "battery_floor": _cfg("battery_floor", "AUTONAV_BATTERY_FLOOR", 15, int),
        "history_size": _cfg("history_size", "AUTONAV_HISTORY_SIZE", 8, int),
        "max_errors": _cfg("max_errors", "AUTONAV_MAX_ERRORS", 3, int),
        "model": (
            str(body.get("model")).strip()
            if body.get("model")
            else os.getenv("AUTONAV_GEMINI_MODEL", "gemini-2.5-flash")
        ),
        "tick_logging_enabled": _autonav_tick_logging_enabled(),
    }

    if not os.getenv("GEMINI_API_KEY"):
        raise HTTPException(
            status_code=400,
            detail="GEMINI_API_KEY is not configured — autonav requires Gemini Flash",
        )

    async with autonav_loop_lock:
        if _is_autonav_loop_running():
            return JSONResponse(
                status_code=409,
                content={"status": "already_running", **_autonav_loop_snapshot()},
            )

        log_dir: Optional[str] = None
        tick_logging_enabled = bool(config.get("tick_logging_enabled"))
        if tick_logging_enabled:
            try:
                log_dir = _autonav_start_run_dir()
                with open(os.path.join(log_dir, "run.json"), "w") as fh:
                    json.dump(
                        {
                            "started_at": datetime.utcnow().isoformat() + "Z",
                            "config": config,
                        },
                        fh,
                        indent=2,
                    )
                # SYSTEM_PROMPT is constant across ticks — write it once.
                try:
                    from autonav_service import SYSTEM_PROMPT as _SP
                    with open(os.path.join(log_dir, "system_prompt.txt"), "w") as fh:
                        fh.write(_SP)
                except Exception:
                    pass
                logger.info("Autonav tick logging enabled: %s", log_dir)
            except Exception as exc:
                logger.warning("Autonav log dir setup failed (continuing without logs): %s", exc)
                log_dir = None
        else:
            logger.info(
                "Autonav tick logging disabled (set AUTONAV_SAVE_TICK_LOGS=true to enable)"
            )

        autonav_loop_state.update(
            {
                "running": True,
                "status": "starting",
                "started_at": datetime.utcnow().isoformat() + "Z",
                "iterations": 0,
                "config": config,
                "last_decision": None,
                "last_action_at": None,
                "last_error": None,
                "error_streak": 0,
                "history": [],
                "log_dir": log_dir,
                "floor_rgb": None,
                "wall_rgb": None,
                "color_sample_count": 0,
            }
        )
        autonav_loop_task = asyncio.create_task(_run_autonav_loop(config))

    return JSONResponse(content={**_autonav_loop_snapshot(), "status": "started"})


@app.post("/autonav/stop")
async def stop_autonav():
    """Stop the autonomous navigation loop and hard-stop the rover."""
    async with autonav_loop_lock:
        stopped = await _stop_autonav_loop_task("stopped")
        snapshot = _autonav_loop_snapshot()

    return JSONResponse(
        content={"status": "stopped" if stopped else "not_running", **snapshot}
    )


@app.get("/autonav/status")
async def autonav_status():
    """Inspect current autonav state, including last decision and recent history."""
    async with autonav_loop_lock:
        return JSONResponse(content=_autonav_loop_snapshot())


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
      color           – common color name, e.g. red | green | blue | yellow | pink | black
                        (default: red)
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
    requested_color = str(body.get("color", "red"))
    color = _normalize_track_color_name(requested_color)
    if color not in _TRACK_COLOR_RANGES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown color '{requested_color}'. Choose from: {_track_color_choices()}",
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
