import os
import wave
import logging

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

    response = client.models.generate_content(
        model="gemini-2.5-flash-preview-tts",
        contents=text,
        config=types.GenerateContentConfig(
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
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(audio_data)

    return wav_path
