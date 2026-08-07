import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import main


class ScreenshotPersistenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_screenshot_saves_files_and_returns_base64(self):
        # Documented since v3: /screenshot leaves the PNGs in screenshots/.
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                main.auth_response_data = {"BOT_UID": "bot"}
                capture = AsyncMock(return_value={"map": b"fake-png-bytes"})
                with (
                    patch.dict(os.environ, {"MISSION_SLUG": ""}),
                    patch.object(
                        main.browser_service, "capture_screenshots", capture
                    ),
                ):
                    response = await main.get_screenshot("map")

                body = json.loads(response.body)
                self.assertIn("map_frame", body)
                self.assertTrue(os.path.exists("screenshots/map.png"))
                with open("screenshots/map.png", "rb") as saved:
                    self.assertEqual(saved.read(), b"fake-png-bytes")
            finally:
                os.chdir(previous_cwd)
                main.auth_response_data = {}


class StubBroadcaster:
    def __init__(self, last_error=None):
        self.last_error = last_error
        self.get_frame_kwargs = None

    async def get_frame(self, **kwargs):
        self.get_frame_kwargs = kwargs
        return None


class CameraFrameFailFastTest(unittest.IsolatedAsyncioTestCase):
    async def test_v2_uses_short_timeout_and_surfaces_capture_error(self):
        stub = StubBroadcaster(last_error="camera frame is not available")
        with patch.dict(main.feed_broadcasters, {"front": stub}):
            with self.assertRaises(main.HTTPException) as ctx:
                await main.get_camera_frame("front")

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail, "camera frame is not available")
        # Fail fast: a poll is bounded by the short v2 budget, not the old 5s.
        self.assertEqual(
            stub.get_frame_kwargs["timeout"], main.V2_FRAME_TIMEOUT_S
        )

    async def test_front_endpoint_404s_when_frame_missing_without_error(self):
        main.auth_response_data = {"BOT_UID": "bot"}
        try:
            with (
                patch.dict(os.environ, {"MISSION_SLUG": ""}),
                patch.dict(main.feed_broadcasters, {"front": StubBroadcaster()}),
            ):
                with self.assertRaises(main.HTTPException) as ctx:
                    await main.get_front_frame()
            self.assertEqual(ctx.exception.status_code, 404)
        finally:
            main.auth_response_data = {}


if __name__ == "__main__":
    unittest.main()
