import asyncio
import os

from openai import OpenAI


DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_PROMPT = (
    "Describe what you see in this rover camera image in 1-2 concise sentences. "
    "Focus on obstacles, people, terrain, and notable objects."
)


async def describe_scene(image_base64: str, user_prompt: str = "") -> str:
    """Generate a concise caption for a base64 camera frame."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required for OpenAI vision captions")

    model = os.getenv("OPENAI_VISION_MODEL", DEFAULT_MODEL)
    timeout = float(os.getenv("OPENAI_VISION_TIMEOUT_SECONDS", "15"))
    prompt = user_prompt.strip() or DEFAULT_PROMPT
    data_url = f"data:image/png;base64,{image_base64}"

    def _request_caption() -> str:
        client = OpenAI(api_key=api_key, timeout=timeout)
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": data_url},
                    ],
                }
            ],
            max_output_tokens=120,
        )
        caption = (response.output_text or "").strip()
        if not caption:
            raise RuntimeError("Vision model returned an empty caption")
        return caption

    return await asyncio.to_thread(_request_caption)
