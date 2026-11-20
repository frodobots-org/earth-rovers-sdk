import base64
import io
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import httpx
from google import genai
from google.genai import types

from tts_service import generate_speech


def _wav(frames):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(frames)
    return output.getvalue()


def _reply(audio_bytes, mime_type):
    part = {
        "inlineData": {
            "data": base64.b64encode(audio_bytes).decode(),
            "mimeType": mime_type,
        }
    }
    return {"candidates": [{"content": {"role": "model", "parts": [part]}}]}


class GeminiSpeechTest(unittest.IsolatedAsyncioTestCase):
    async def _speak(self, audio_bytes, mime_type, **environment):
        requests = []

        def provider(request):
            requests.append(request)
            return httpx.Response(200, json=_reply(audio_bytes, mime_type))

        make_client = genai.Client
        with (
            tempfile.TemporaryDirectory() as output_dir,
            httpx.Client(
                transport=httpx.MockTransport(provider), trust_env=False
            ) as http,
        ):

            def client(*, api_key):
                return make_client(
                    api_key=api_key, http_options=types.HttpOptions(httpx_client=http)
                )

            with (
                patch.object(genai, "Client", client),
                patch.dict(
                    os.environ,
                    {
                        "TTS_PROVIDER": "gemini",
                        "TTS_API_KEY": "fake-test-key",
                        "TTS_VOICE": "Puck",
                        **environment,
                    },
                    clear=True,
                ),
            ):
                filename = await generate_speech(
                    "Hello", str(Path(output_dir) / "clip.mp3")
                )
                self.assertEqual(Path(filename).name, "clip.wav")
                return Path(filename).read_bytes(), requests

    async def test_gemini_developer_api_writes_returned_wav_without_double_wrapping(
        self,
    ):
        frames = b"\x00\x00\x01\x00"
        output, requests = await self._speak(_wav(frames), "audio/wav")
        self.assertEqual(output, _wav(frames))
        with wave.open(io.BytesIO(output), "rb") as audio:
            self.assertEqual(audio.readframes(audio.getnframes()), frames)

        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.url.host, "generativelanguage.googleapis.com")
        self.assertEqual(
            request.url.path, "/v1beta/models/gemini-3.8-flash-tts:generateContent"
        )
        body = json.loads(request.content)
        self.assertEqual(body["contents"][0]["parts"][0]["text"], "Hello")
        config = body["generationConfig"]
        self.assertEqual(config["responseModalities"], ["AUDIO"])
        speech_config = types.SpeechConfig.model_validate(config["speechConfig"])
        self.assertEqual(
            speech_config.voice_config.prebuilt_voice_config.voice_name, "Puck"
        )

    async def test_invalid_developer_api_audio_is_not_written_as_pcm(self):
        with self.assertRaisesRegex(ValueError, "did not return a WAV file"):
            await self._speak(b"not a WAV", "audio/wav")

    async def test_custom_gemini_endpoint_keeps_its_previous_model(self):
        output, requests = await self._speak(
            b"\x00\x00", "audio/l16", GOOGLE_GEMINI_BASE_URL="https://gateway.example"
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, "gateway.example")
        self.assertIn(
            "gemini-2.5-flash-preview-tts:generateContent", requests[0].url.path
        )
        self.assertTrue(output.startswith(b"RIFF"))
