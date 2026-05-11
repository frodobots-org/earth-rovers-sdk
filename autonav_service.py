import asyncio
import base64
import hashlib
import json
import os
from typing import Any, Dict, List, Optional


DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_OPENAI_MODEL = "gpt-4o"
PROMPT_VERSION = "autonav-vision-guardrails-2026-04-29-7-cautious-probe-no-path-zone"

ACTIONS = ("forward", "backward", "turn_left", "turn_right", "stop")

PROVIDERS = ("gemini", "openai")


def _resolve_provider(model: Optional[str], explicit: Optional[str] = None) -> str:
    """Decide which LLM backend to call. Priority:
      1. explicit argument (from /autonav/start body)
      2. AUTONAV_LLM_PROVIDER env var
      3. auto-detect from model name (gpt/o1/o3 → openai, gemini → gemini)
      4. default: gemini
    """
    for source in (explicit, os.getenv("AUTONAV_LLM_PROVIDER")):
        if source:
            cleaned = str(source).strip().lower()
            if cleaned in PROVIDERS:
                return cleaned
    if isinstance(model, str):
        m = model.lower().strip()
        if m.startswith("gpt-") or m.startswith("o1") or m.startswith("o3") or m.startswith("o4"):
            return "openai"
        if m.startswith("gemini"):
            return "gemini"
    return "gemini"


# Memory-bank descriptions are shared between providers; each backend wraps
# them in its own image-content format.
_MEMORY_DESCRIPTIONS = {
    "clear_corridor": (
        "REFERENCE — what a known-drivable corridor in this environment looks like. "
        "If the current frame matches this, forward is safe."
    ),
    "wall_close": (
        "REFERENCE — what a wall pressed against the camera looks like in this run. "
        "If the current frame matches this, do NOT pick forward."
    ),
    "cardboard_face": (
        "REFERENCE — what a cardboard surface PRESSED AGAINST OR FILLING THE WHOLE LENS "
        "looks like. ONLY cite this reference when the entire current frame is dominated "
        "by a uniform cardboard surface at very close range with NO visible floor. A box "
        "visible at a distance, or a box on one side of the frame with floor still "
        "visible elsewhere in the bottom of the frame, is NOT a match for this reference."
    ),
    "narrow_obstacle": (
        "REFERENCE — what a too-narrow gap between two physical objects looks like. "
        "Cite this only when the current frame shows TWO objects flanking a gap so "
        "tight the rover cannot fit through it. A box on only one side does NOT match."
    ),
    "named_obstacle": (
        "REFERENCE — what a real obstacle (box, monitor, package, etc.) DIRECTLY in "
        "the rover's path looks like. Cite this only when an object actually occupies "
        "the bottom-center driving lane. An object visible only on the LEFT side or "
        "only on the RIGHT side of the frame is NOT a match for this reference."
    ),
}

SYSTEM_PROMPT = (
    "You drive a very small ground rover through a TIGHT tabletop-scale maze. Corridors are "
    "narrow — often only a few centimeters wider than the rover itself. On each tick you "
    "receive the rover's front camera frame (and, when reversing or stuck, the rear camera "
    "frame). Your job is to pick ONE next navigation action that makes forward progress without "
    "crashing.\n\n"
    "ROVER PHYSICAL DIMENSIONS:\n"
    "  • Width: 16 cm (about 6.3 inches)\n"
    "  • Length: 25 cm (about 9.8 inches)\n"
    "  • Add ~2-3 cm clearance on each side for safe driving (total ~22 cm wide).\n"
    "Use these dimensions when evaluating whether a visible gap is passable:\n"
    "  • Gap visibly wider than ~25 cm → comfortable, drive through.\n"
    "  • Gap 16-22 cm → tight but possible — creep through with low speed (0.15 m/s).\n"
    "  • Gap clearly narrower than ~16 cm → the rover physically cannot fit, treat as blocked.\n"
    "Do NOT label a gap 'too narrow' just because boxes flank it. The rover only needs ~22 cm "
    "of clearance — many corridors that LOOK narrow in fisheye are still wider than that.\n\n"
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
    "if IMAGE 1 shows a side opening — choose a turn toward that opening instead. If IMAGE 2's "
    "CENTER-BOTTOM is filled by a flat cardboard / printed surface with NO floor visible "
    "anywhere in the bottom strip, that is a close obstacle. If floor IS still visible in any "
    "part of the bottom-center strip, do NOT call it blocked — even if large printed objects "
    "dominate the sides or upper portion of the frame.\n\n"
    "REFERENCE PANEL — labeled images BEFORE the current frame are reference examples captured "
    "earlier in this run from THIS environment. Use them as visual ground truth: 'clear_corridor' "
    "is what real drivable floor looks like here; 'wall_close', 'cardboard_face', 'narrow_obstacle', "
    "and 'named_obstacle' are scenarios where forward was wrong. Named-obstacle patterns apply "
    "ONLY when the obstacle occupies the CENTER-BOTTOM driving lane. Resemblance to "
    "'cardboard_face' / 'named_obstacle' / 'narrow_obstacle' on the SIDES of the frame while "
    "floor remains visible in the center-bottom does NOT justify turning. If the CURRENT frame's "
    "center-bottom matches 'wall_close' / 'cardboard_face' / 'named_obstacle', pick a turn. If "
    "the center-bottom resembles 'clear_corridor', forward is safe.\n\n"
    "DECISION PRIORITY — focus on the CENTER-BOTTOM of the fisheye (the rover's immediate path):\n"
    "  • FLOOR VISIBLE IN CENTER-BOTTOM = FORWARD. This is the highest-priority rule. No other "
    "signal — named-obstacle pattern match, large objects elsewhere in the frame, uniformity "
    "stats, or reference-panel resemblance — can override actual visible floor in the "
    "center-bottom lane. If you can see floor there, you MUST pick forward.\n"
    "  • TWO-SIDED OBSTACLE GAP (NARROW CORRIDOR): if large objects appear on BOTH the left "
    "AND right sides simultaneously but floor is still visible in the center-bottom strip "
    "between them, this is a NARROW CORRIDOR — pick forward. Do NOT treat the two flanking "
    "objects as a single combined blocker. The view is only blocked when the objects physically "
    "connect across the center lane and there is no floor gap between them.\n"
    "  • Center-bottom shows mostly floor / open ground → you MUST pick forward. Do not pick "
    "turn because the corridor appears to bend ahead — you will re-evaluate after the forward "
    "burst. Anticipatory turning is WRONG and wastes ticks. If the immediate path is clear, "
    "GO FORWARD.\n"
    "  • Center-bottom shows a wall, object, box, bag, chair leg, bottle, cable, printed text, "
    "tape stripe, smooth cardboard face, or any 3D barrier in the rover's direct path → forward "
    "is BLOCKED. Turn toward whichever side has visible floor.\n"
    "  • IMPORTANT — SIDE-ONLY OBJECTS ARE NOT BLOCKERS. If you see boxes, packages, walls, or "
    "other objects only on the LEFT half OR only on the RIGHT half of the frame, BUT the "
    "center-bottom (the rover's actual driving lane) still shows clear floor extending forward "
    "for at least roughly half a rover-length, pick forward — NOT turn. The rover only needs "
    "the center lane to be clear; objects parked off to one side are scenery, not blockers. "
    "Only call it 'blocked' when an object actually occupies the center-bottom lane.\n"
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
    "- Obstacle definition: an object that occupies the BOTTOM-CENTER region of the frame "
    "within roughly one rover-length is a block. Box, bag, chair leg, bottle, cable, cord, or "
    "any 3D object in that center region means forward is NOT safe — turn toward whichever "
    "side has visible floor. The same object sitting OFF to one side of the frame, with "
    "visible floor still showing in the bottom-center, is NOT a blocker. Walls visible on the "
    "sides of a narrow corridor are NORMAL and not obstacles; do not let them make you turn. "
    "Be specific in your reasoning_steps about which region of the frame the object actually "
    "occupies — describing position as 'right side' is different from 'center'.\n"
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
    visual_memory: Optional[List[Dict[str, Any]]] = None,
    provider: Optional[str] = None,
) -> Dict[str, Any]:
    """Dispatcher: routes the navigation decision to either Gemini or OpenAI.

    Provider precedence (in `_resolve_provider`):
      1. `provider` argument (from /autonav/start body)
      2. AUTONAV_LLM_PROVIDER env var
      3. auto-detect from `model` name
      4. default: gemini

    Returns a validated decision dict. On any failure the loop should treat
    this as an error and NOT issue a drive command.
    """
    resolved_provider = _resolve_provider(model, explicit=provider)

    if resolved_provider == "openai":
        selected_model = model or os.getenv("AUTONAV_OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    else:
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
        debug_out["provider"] = resolved_provider

    if resolved_provider == "openai":
        return await _decide_openai(
            front_b64=front_b64,
            rear_b64=rear_b64,
            front_path_b64=front_path_b64,
            user_prompt=user_prompt,
            visual_memory=visual_memory,
            selected_model=selected_model,
            max_linear=max_linear,
            max_turn_deg=max_turn_deg,
            max_forward_ms=max_forward_ms,
        )
    return await _decide_gemini(
        front_b64=front_b64,
        rear_b64=rear_b64,
        front_path_b64=front_path_b64,
        user_prompt=user_prompt,
        visual_memory=visual_memory,
        selected_model=selected_model,
        max_linear=max_linear,
        max_turn_deg=max_turn_deg,
        max_forward_ms=max_forward_ms,
        thinking_budget=thinking_budget,
    )


async def _decide_gemini(
    *,
    front_b64: str,
    rear_b64: Optional[str],
    front_path_b64: Optional[str],
    user_prompt: str,
    visual_memory: Optional[List[Dict[str, Any]]],
    selected_model: str,
    max_linear: float,
    max_turn_deg: float,
    max_forward_ms: int,
    thinking_budget: Optional[int],
) -> Dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is required for autonav (provider=gemini)")

    def _request() -> Dict[str, Any]:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        contents: List[Any] = []

        for entry in (visual_memory or []):
            label = entry.get("label")
            b64 = entry.get("b64")
            if not b64 or label not in _MEMORY_DESCRIPTIONS:
                continue
            try:
                ref_bytes = base64.b64decode(b64)
            except Exception:
                continue
            contents.append(_MEMORY_DESCRIPTIONS[label])
            contents.append(
                types.Part.from_bytes(data=ref_bytes, mime_type=_sniff_mime(ref_bytes))
            )
        if visual_memory:
            contents.append(
                "End of reference panel. The next image is the CURRENT front frame; compare it "
                "to the references above and pick the matching scenario."
            )

        front_bytes = base64.b64decode(front_b64)
        contents.extend(
            [
                "IMAGE 1: full front frame (the current view).",
                types.Part.from_bytes(data=front_bytes, mime_type=_sniff_mime(front_bytes)),
            ]
        )
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
            raise RuntimeError("autonav gemini returned empty response")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"autonav gemini returned non-JSON: {text[:200]}") from exc
        return _validate_decision(raw, max_linear, max_turn_deg, max_forward_ms)

    return await asyncio.to_thread(_request)


async def _decide_openai(
    *,
    front_b64: str,
    rear_b64: Optional[str],
    front_path_b64: Optional[str],
    user_prompt: str,
    visual_memory: Optional[List[Dict[str, Any]]],
    selected_model: str,
    max_linear: float,
    max_turn_deg: float,
    max_forward_ms: int,
) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required for autonav (provider=openai)")

    def _image_block(b64: str) -> Dict[str, Any]:
        try:
            mime = _sniff_mime(base64.b64decode(b64))
        except Exception:
            mime = "image/jpeg"
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime};base64,{b64}",
                "detail": "high",
            },
        }

    def _request() -> Dict[str, Any]:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)

        # User message content: reference panel → current images → user prompt.
        # SYSTEM_PROMPT goes in a separate system message.
        content: List[Dict[str, Any]] = []

        for entry in (visual_memory or []):
            label = entry.get("label")
            b64 = entry.get("b64")
            if not b64 or label not in _MEMORY_DESCRIPTIONS:
                continue
            content.append({"type": "text", "text": _MEMORY_DESCRIPTIONS[label]})
            content.append(_image_block(b64))
        if visual_memory:
            content.append(
                {
                    "type": "text",
                    "text": (
                        "End of reference panel. The next image is the CURRENT front frame; "
                        "compare it to the references above and pick the matching scenario."
                    ),
                }
            )

        content.append({"type": "text", "text": "IMAGE 1: full front frame (the current view)."})
        content.append(_image_block(front_b64))

        if front_path_b64:
            content.append(
                {
                    "type": "text",
                    "text": "IMAGE 2: zoomed immediate driving lane from the same front frame.",
                }
            )
            content.append(_image_block(front_path_b64))

        if rear_b64:
            content.append(
                {"type": "text", "text": "REAR IMAGE: rear frame for reverse-safety only."}
            )
            content.append(_image_block(rear_b64))

        content.append({"type": "text", "text": user_prompt})

        # Strict JSON schema: OpenAI's structured outputs require
        # additionalProperties=False and `required` listing all properties.
        strict_schema = {
            "type": "object",
            "properties": RESPONSE_SCHEMA["properties"],
            "required": list(RESPONSE_SCHEMA["properties"].keys()),
            "additionalProperties": False,
        }

        response = client.chat.completions.create(
            model=selected_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "autonav_decision",
                    "schema": strict_schema,
                    "strict": True,
                },
            },
            max_tokens=700,
        )
        text = (response.choices[0].message.content or "").strip()
        if not text:
            raise RuntimeError("autonav openai returned empty response")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"autonav openai returned non-JSON: {text[:200]}") from exc
        return _validate_decision(raw, max_linear, max_turn_deg, max_forward_ms)

    return await asyncio.to_thread(_request)
