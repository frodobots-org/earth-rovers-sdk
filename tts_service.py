import base64
import logging
import os
import wave
from binascii import Error as Base64Error
from pathlib import Path

logger = logging.getLogger(__name__)


async def generate_speech(text: str, output_path: str) -> str:
    """Generate speech audio from text. Returns the output file path."""
    provider = os.getenv("TTS_PROVIDER", "edge").lower()

    if provider == "gemini":
        return await _generate_gemini(text, output_path)
    else:
        return await _generate_edge(text, output_path)


async def _generate_edge(text: str, output_path: str) -> str:
    import edge_tts

    voice = os.getenv("TTS_VOICE", "en-US-GuyNeural")
    communicate = edge_tts.Communicate(text, voice)
    mp3_path = output_path.rsplit(".", 1)[0] + ".mp3"
    await communicate.save(mp3_path)
    return mp3_path


async def _generate_gemini(text: str, output_path: str) -> str:
    from google import genai
    from google.genai import types

    api_key = os.getenv("TTS_API_KEY")
    if not api_key:
        raise ValueError("TTS_API_KEY is required for Gemini TTS")

    voice = os.getenv("TTS_VOICE", "Kore")
    client = genai.Client(api_key=api_key)
    uses_developer_api = not client.vertexai and not os.getenv("GOOGLE_GEMINI_BASE_URL")

    response = client.models.generate_content(
        model=(
            "gemini-3.8-flash-tts"
            if uses_developer_api
            else "gemini-2.5-flash-preview-tts"
        ),
        contents=text,
        config=types.GenerateContentConfig(
            http_options=types.HttpOptions(api_version="v1beta")
            if uses_developer_api
            else None,
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice,
                    )
                )
            ),
        ),
    )

    audio_data = response.candidates[0].content.parts[0].inline_data.data

    wav_path = output_path.rsplit(".", 1)[0] + ".wav"
    if uses_developer_api:
        _write_gemini_wav(wav_path, audio_data)
    else:
        _write_pcm_wave(wav_path, audio_data)
    return wav_path


def _is_wav(audio_data: bytes) -> bool:
    return audio_data[:4] == b"RIFF" and audio_data[8:12] == b"WAVE"


def _write_gemini_wav(wav_path: str, audio_data: bytes) -> None:
    if not _is_wav(audio_data):
        # Older Google SDK/Pydantic combinations can leave the base64 encoded.
        try:
            audio_data = base64.b64decode(audio_data, validate=True)
        except Base64Error as exc:
            raise ValueError("Gemini TTS did not return a WAV file") from exc
    if not _is_wav(audio_data):
        raise ValueError("Gemini TTS did not return a WAV file")
    Path(wav_path).write_bytes(audio_data)


def _write_pcm_wave(wav_path: str, audio_data: bytes) -> None:
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(audio_data)
