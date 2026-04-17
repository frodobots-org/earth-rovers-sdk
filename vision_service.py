import asyncio
import base64
import os


DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_PROMPT = (
    "Describe what you see in this rover camera image in 1-2 concise sentences. "
    "Focus on obstacles, people, terrain, and notable objects."
)


async def describe_scene(image_base64: str, user_prompt: str = "") -> str:
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
        response = client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                prompt,
            ],
            config=types.GenerateContentConfig(max_output_tokens=200),
        )
        caption = (response.text or "").strip()
        if not caption:
            raise RuntimeError("Vision model returned an empty caption")
        return caption

    return await asyncio.to_thread(_request_caption)
