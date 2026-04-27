import asyncio
import base64
import os
from typing import Optional


DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_PROMPT = (
    "Describe what you see in this rover camera image in 1-2 concise sentences. "
    "Focus on obstacles, people, terrain, and notable objects."
)


async def describe_scene(
    image_base64: str,
    user_prompt: str = "",
    max_output_tokens: int = 200,
    thinking_budget: Optional[int] = None,
) -> str:
    """Generate a concise caption for a base64 camera frame using Gemini."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is required for Gemini vision captions")

    model = os.getenv("GEMINI_VISION_MODEL", DEFAULT_MODEL)
    prompt = user_prompt.strip() or DEFAULT_PROMPT

    def _request_caption() -> str:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        image_bytes = base64.b64decode(image_base64)
        # Sniff the magic bytes — JPEG starts with FF D8, PNG with 89 50 4E 47.
        if image_bytes[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            mime = "image/jpeg"  # rover frames are JPEG by default
        config_kwargs = {"max_output_tokens": max_output_tokens}
        if thinking_budget is not None:
            # Disable / cap Gemini's internal reasoning to reduce latency.
            try:
                config_kwargs["thinking_config"] = types.ThinkingConfig(
                    thinking_budget=thinking_budget
                )
            except AttributeError:
                pass  # older SDKs without ThinkingConfig — ignore silently
        response = client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime),
                prompt,
            ],
            config=types.GenerateContentConfig(**config_kwargs),
        )
        caption = (response.text or "").strip()
        if not caption:
            raise RuntimeError("Vision model returned an empty caption")
        return caption

    return await asyncio.to_thread(_request_caption)
