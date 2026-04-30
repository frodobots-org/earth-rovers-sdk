import asyncio
import base64
import hashlib
import json
import os
from typing import Any, Dict, List, Optional


DEFAULT_MODEL = "gemini-2.5-flash"
PROMPT_VERSION = "autonav-vision-guardrails-2026-04-29-7-cautious-probe-no-path-zone"

ACTIONS = ("forward", "backward", "turn_left", "turn_right", "stop")

SYSTEM_PROMPT = (
    "You drive a very small ground rover through a TIGHT tabletop-scale maze. Corridors are "
    "narrow — often only a few centimeters wider than the rover itself. On each tick you "
    "receive the rover's front camera frame (and, when reversing or stuck, the rear camera "
    "frame). Your job is to pick ONE next navigation action that makes forward progress without "
    "crashing.\n\n"
    "CURRENT IMAGE FIRST: every tick's action must be based primarily on the current visual "
    "evidence in the images. Telemetry, history, and hints are secondary helpers and must not "
    "override what is visibly open or blocked right now.\n\n"
    "CRITICAL: scale is small. The fisheye front camera frequently has walls occupying most "
    "of the frame even when there IS a usable path. Do NOT require 'open space for a meter' "
    "to move forward. But a side gap is NOT a forward path unless the center driving lane is "
    "also open. Hesitation is worse than cautious forward motion only when the immediate lane "
    "is actually clear.\n\n"
    "IMAGE USAGE:\n"
    "  • IMAGE 1 = full front frame.\n"
    "  • IMAGE 2 = zoomed immediate driving lane from the same front frame, when provided.\n"
    "Use IMAGE 2 to judge whether the center-bottom driving lane is safe. Side objects that "
    "appear near the left/right edges of IMAGE 2 are not blockers unless they occupy the "
    "bottom-center lane. If IMAGE 2 shows the center lane blocked, do NOT choose forward even "
    "if IMAGE 1 shows a side opening — choose a turn toward that opening instead. If IMAGE 2 "
    "is filled by a printed box/cardboard/sign surface, large "
    "letters/logos, tape stripe, or a smooth vertical surface with no floor perspective, "
    "that is a close obstacle, not floor.\n\n"
    "DECISION PRIORITY — focus on the CENTER-BOTTOM of the fisheye (the rover's immediate path):\n"
    "  • Center-bottom shows mostly floor / open ground → you MUST pick forward. Do not pick "
    "turn because the corridor appears to bend ahead — you will re-evaluate after the forward "
    "burst. Anticipatory turning is WRONG and wastes ticks. If the immediate path is clear, "
    "GO FORWARD.\n"
    "  • Center-bottom shows a wall, object, box, bag, chair leg, bottle, cable, printed text, "
    "tape stripe, smooth cardboard face, or any 3D barrier in the rover's direct path → forward "
    "is BLOCKED. Turn toward whichever side has visible floor.\n"
    "  • Clear floor only on the LEFT side of the frame (not center) → turn_left.\n"
    "  • Clear floor only on the RIGHT side of the frame (not center) → turn_right.\n"
    "  • No visible floor anywhere → commit to a 45-90° search turn toward the side with more structure.\n\n"
    "IMPORTANT: turning is a recovery action for when forward is impossible. If you pick turn "
    "while admitting in your reasoning that the immediate forward path is clear, you are wrong. "
    "Pick forward and re-plan next tick.\n\n"
    "Rules:\n"
    "- Use action=forward with a short duration (400-900ms). IMPORTANT: linear_speed must be "
    "at least 0.15; below that the motors cannot overcome stiction. Prefer values close to "
    "max_linear for forward motion.\n"
    "- If the immediate lane is not valid, prefer turning to reacquire a new lane. Do NOT use "
    "backward as your default recovery action.\n"
    "- Obstacle definition: an object sitting on the floor in the bottom-center of the frame "
    "within roughly one rover-length is a block. Box, bag, chair leg, bottle, cable, cord, or "
    "any 3D object in that region means forward is NOT safe — turn toward whichever side has "
    "visible floor instead. Do not decide to climb or drive over a box/package/tape edge; this "
    "rover should treat those as barriers. Walls visible on the sides of a narrow corridor are NORMAL and "
    "not obstacles; do not let them make you turn.\n"
    "- When genuinely blocked (wall meeting the rover across the whole width of the frame), "
    "COMMIT to one turn direction and use the blocked-lane bounded scan: first 45°, then 90° "
    "on that side. If the lane is still blocked after the 90° check, switch to the opposite "
    "side instead of increasing the same-side turn beyond 90°. Do NOT alternate tiny left/right "
    "nudges before finishing the current side's 45° then 90° scan.\n"
    "- Trust what you see, not what you infer. If the previous action was forward and the new "
    "frame looks different from the last one you described (different wall angle, new objects, "
    "changed floor perspective), the rover DID move — keep going forward; do not flip to turning.\n"
    "- CRITICAL — wall-pressed-against-camera detection: a wall filling the fisheye at very "
    "close range looks like a uniform field of one color (often light gray/white). There is "
    "NO visible horizon, NO visible floor-wall boundary, NO distance cues. This is NOT a "
    "clear path — it is the camera pressed against a wall. Pick a committed turn, not backward. "
    "Signs to watch for:\n"
    "    * Top half and bottom half of the frame look the same color (no horizon).\n"
    "    * No 3D structure or perspective cues visible anywhere.\n"
    "  Telemetry signals that confirm wall-close:\n"
    "    * front_uniformity < 10 — frame is nearly flat.\n"
    "    * front_tb_delta < 6 — top and bottom halves are the same brightness (no horizon).\n"
    "    * bot_dist_to_wall < bot_dist_to_floor — the bottom of the frame is closer in color "
    "to the LEARNED WALL than to the LEARNED FLOOR. This means the camera sees wall where "
    "floor should be.\n"
    "  When any of these signal wall-close, pick turn_left or turn_right toward whichever side "
    "shows more structure/opening. If neither side is clear, still commit to one large turn.\n"
    "- Calibrated color profile: learned_floor_rgb and learned_wall_rgb are the running mean "
    "RGB of what FLOOR and WALL looked like on earlier good frames this run. current_bot_rgb "
    "is the bottom-half of the current frame. These color numbers are advisory and can be "
    "wrong when a cardboard box, printed label, or shadow has a floor-like color. Never use "
    "low bot_dist_to_floor or center_blocked=False to overrule a visible close obstacle. If "
    "bot_dist_to_floor is large AND bot_dist_to_wall is small, the rover's forward zone shows "
    "wall, not floor — do NOT pick forward.\n"
    "- front_uniformity is pixel-stddev; front_tb_delta is top-vs-bottom brightness gap. "
    "Real corridors have front_tb_delta >= 10.\n"
    "- Avoid backward for maze recovery. If the current lane is blocked or uncertain, turn to "
    "search for a valid path instead.\n"
    "- Keep linear_speed at or below max_linear; keep turn_degrees at or below max_turn_deg.\n"
    "- Obstacles to avoid: objects that clearly block the rover's body (boxes, bottles, tools, "
    "cables, large blocks), drop-offs, liquid. Walls on the side of a narrow corridor are "
    "normal and NOT obstacles.\n\n"
    "Text fields must be first-person, present/future tense, max 200 chars each. Avoid the "
    "words 'camera', 'robot', 'front view', 'rear view', 'fisheye', 'angle'. Use 'in front of "
    "me' and 'behind me' instead.\n"
    "Output exactly 4 reasoning_steps and they must justify the chosen action."
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "linear_speed": {"type": "number"},
        "turn_degrees": {"type": "number"},
        "duration_ms": {"type": "integer"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "comment_front": {"type": "string"},
        "comment_rear": {"type": "string"},
        "plan_of_action": {"type": "string"},
        "reasoning_steps": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["action", "reason", "reasoning_steps"],
}


def _sniff_mime(image_bytes: bytes) -> str:
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _format_history(history: List[Dict[str, Any]]) -> str:
    if not history:
        return "(no prior actions this run)"
    lines = []
    for item in history[-8:]:
        act = item.get("action", "?")
        extra = []
        if "turn_degrees" in item and item.get("turn_degrees"):
            extra.append(f"{item['turn_degrees']}°")
        if "linear_speed" in item and item.get("linear_speed"):
            extra.append(f"v={item['linear_speed']}")
        # observed_speed deliberately omitted — this rover's speed telemetry
        # is unreliable (GPS-derived) and misled the model into thinking
        # every forward action failed.
        suffix = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"- {act}{suffix}")
    return "\n".join(lines)


def _build_user_prompt(
    telemetry: Dict[str, Any],
    history: List[Dict[str, Any]],
    max_linear: float,
    max_turn_deg: float,
    max_forward_ms: int,
    hint: Optional[str],
) -> str:
    orientation = telemetry.get("orientation", "?")
    speed = telemetry.get("speed", "?")
    battery = telemetry.get("battery", "?")
    uniformity = telemetry.get("front_uniformity")
    tb_delta = telemetry.get("front_tb_delta")
    rear_included = telemetry.get("_rear_included")
    floor_rgb = telemetry.get("learned_floor_rgb")
    wall_rgb = telemetry.get("learned_wall_rgb")
    initial_floor_rgb = telemetry.get("initial_floor_rgb")
    initial_wall_rgb = telemetry.get("initial_wall_rgb")
    cur_bot = telemetry.get("current_bot_rgb")
    cur_top = telemetry.get("current_top_rgb")
    bot_f = telemetry.get("bot_dist_to_floor")
    bot_w = telemetry.get("bot_dist_to_wall")
    bot_initial_f = telemetry.get("bot_dist_to_initial_floor")
    bot_rolling_f = telemetry.get("bot_dist_to_rolling_floor")
    path_summary = telemetry.get("path_profile_summary")
    uniformity_str = f"{uniformity}" if uniformity is not None else "?"
    tb_delta_str = f"{tb_delta}" if tb_delta is not None else "?"
    color_line = (
        "Color calibration (learned so far this run): "
        f"floor_rgb={floor_rgb}, wall_rgb={wall_rgb}. "
        f"initial_floor_rgb={initial_floor_rgb}, initial_wall_rgb={initial_wall_rgb}. "
        f"Current frame: bot_rgb={cur_bot}, top_rgb={cur_top}. "
        f"bot_dist_to_floor={bot_f}, bot_dist_to_wall={bot_w}. "
        f"bot_dist_to_initial_floor={bot_initial_f}, bot_dist_to_rolling_floor={bot_rolling_f}. "
        "These are advisory color distances only. Low bot_dist_to_floor can be wrong when "
        "a box/cardboard/sign surface has a floor-like color; if the image shows a close "
        "printed surface, tape stripe, smooth cardboard face, or obstacle, treat forward as "
        "blocked. High bot_dist_to_floor + low bot_dist_to_wall still suggests wall where "
        "floor should be."
        if floor_rgb
        else "Color calibration: not yet learned (need a clear-horizon frame first)."
    )
    parts = [
        f"Telemetry: orientation={orientation}°, speed={speed}, battery={battery}%, "
        f"front_uniformity={uniformity_str}, front_tb_delta={tb_delta_str}.",
        color_line,
        f"Constraints: max_linear={max_linear}, max_turn_deg={max_turn_deg}, max_duration_ms={max_forward_ms}.",
        f"Recent actions:\n{_format_history(history)}",
    ]
    if path_summary:
        parts.append(
            "Local path analysis (advisory, not authoritative): "
            f"{path_summary}. If this disagrees with the current images, trust the images."
        )
    parts.append(
        "Rear camera is attached below the front frame in this prompt."
        if rear_included
        else "Rear camera not included this tick — assume unknown and prefer turning over backward."
    )
    if hint:
        parts.append(f"Navigator hint: {hint}")
    parts.append(
        "Base the action on the current images first. Use telemetry/history only as supporting context."
    )
    parts.append(
        "Pick ONE action. If action is forward or backward, set linear_speed (0 < v ≤ max_linear) "
        "and duration_ms. If action is turn_left or turn_right, set turn_degrees (0 < d ≤ max_turn_deg). "
        "Always fill reason, plan_of_action, comment_front, comment_rear, and exactly 4 reasoning_steps."
    )
    return "\n\n".join(parts)


def _validate_decision(
    raw: Dict[str, Any],
    max_linear: float,
    max_turn_deg: float,
    max_forward_ms: int,
) -> Dict[str, Any]:
    action = raw.get("action")
    if action not in ACTIONS:
        raise ValueError(f"invalid action from model: {action!r}")

    linear_speed = float(raw.get("linear_speed") or 0.0)
    turn_degrees = float(raw.get("turn_degrees") or 0.0)
    duration_ms = int(raw.get("duration_ms") or 800)

    linear_speed = max(0.0, min(linear_speed, max_linear))
    turn_degrees = max(0.0, min(abs(turn_degrees), max_turn_deg))
    duration_ms = max(200, min(duration_ms, max_forward_ms))

    # Rover stiction floor: below ~0.12 m/s the motors don't overcome friction.
    # For forward/backward always clamp UP to at least 0.15 (or max_linear if
    # that is configured lower, in which case the rover may not move at all).
    if action in ("forward", "backward"):
        linear_speed = max(linear_speed, min(0.15, max_linear))
    if action in ("turn_left", "turn_right") and turn_degrees <= 0:
        turn_degrees = min(20.0, max_turn_deg)

    return {
        "action": action,
        "linear_speed": round(linear_speed, 3),
        "turn_degrees": round(turn_degrees, 1),
        "duration_ms": duration_ms,
        "confidence": float(raw.get("confidence") or 0.0),
        "reason": str(raw.get("reason", ""))[:240],
        "comment_front": str(raw.get("comment_front", ""))[:240],
        "comment_rear": str(raw.get("comment_rear", ""))[:240],
        "plan_of_action": str(raw.get("plan_of_action", ""))[:240],
        "reasoning_steps": [str(s)[:240] for s in (raw.get("reasoning_steps") or [])][:6],
    }


async def decide(
    front_b64: str,
    rear_b64: Optional[str],
    front_path_b64: Optional[str],
    telemetry: Dict[str, Any],
    history: List[Dict[str, Any]],
    max_linear: float = 0.25,
    max_turn_deg: float = 180.0,
    max_forward_ms: int = 1500,
    model: Optional[str] = None,
    thinking_budget: Optional[int] = 0,
    hint: Optional[str] = None,
    debug_out: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Ask Gemini Flash for the next navigation decision.

    Returns a validated decision dict. On any failure the loop should treat
    this as an error and NOT issue a drive command.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is required for autonav decisions")

    selected_model = model or os.getenv("AUTONAV_GEMINI_MODEL", DEFAULT_MODEL)
    telemetry = dict(telemetry)
    telemetry["_rear_included"] = bool(rear_b64)
    user_prompt = _build_user_prompt(
        telemetry, history, max_linear, max_turn_deg, max_forward_ms, hint
    )
    if debug_out is not None:
        debug_out["system_prompt"] = SYSTEM_PROMPT
        debug_out["system_prompt_sha"] = hashlib.sha256(
            SYSTEM_PROMPT.encode("utf-8")
        ).hexdigest()[:12]
        debug_out["prompt_version"] = PROMPT_VERSION
        debug_out["user_prompt"] = user_prompt
        debug_out["model"] = selected_model

    def _request() -> Dict[str, Any]:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)

        front_bytes = base64.b64decode(front_b64)
        contents: List[Any] = [
            "IMAGE 1: full front frame.",
            types.Part.from_bytes(data=front_bytes, mime_type=_sniff_mime(front_bytes)),
        ]
        if front_path_b64:
            path_bytes = base64.b64decode(front_path_b64)
            contents.extend(
                [
                    "IMAGE 2: zoomed immediate driving lane from the same front frame.",
                    types.Part.from_bytes(
                        data=path_bytes, mime_type=_sniff_mime(path_bytes)
                    ),
                ]
            )
        if rear_b64:
            rear_bytes = base64.b64decode(rear_b64)
            contents.extend(
                [
                    "REAR IMAGE: rear frame for reverse-safety only.",
                    types.Part.from_bytes(
                        data=rear_bytes, mime_type=_sniff_mime(rear_bytes)
                    ),
                ]
            )
        contents.append(SYSTEM_PROMPT)
        contents.append(user_prompt)

        config_kwargs: Dict[str, Any] = {
            "response_mime_type": "application/json",
            "response_schema": RESPONSE_SCHEMA,
            "max_output_tokens": 700,
        }
        if thinking_budget is not None:
            try:
                config_kwargs["thinking_config"] = types.ThinkingConfig(
                    thinking_budget=thinking_budget
                )
            except AttributeError:
                pass

        response = client.models.generate_content(
            model=selected_model,
            contents=contents,
            config=types.GenerateContentConfig(**config_kwargs),
        )
        text = (response.text or "").strip()
        if not text:
            raise RuntimeError("autonav model returned empty response")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"autonav model returned non-JSON: {text[:200]}") from exc
        return _validate_decision(raw, max_linear, max_turn_deg, max_forward_ms)

    return await asyncio.to_thread(_request)
