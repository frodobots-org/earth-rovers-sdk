import base64
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main


class OpenclawMediaEndpointsTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.original_auth_response_data = main.auth_response_data
        main.auth_response_data = {"CHANNEL_NAME": "test-channel"}
        self.need_start_mission_patch = patch.object(
            main, "need_start_mission", new=AsyncMock(return_value=None)
        )
        self.need_start_mission_patch.start()

    def tearDown(self):
        self.need_start_mission_patch.stop()
        main.auth_response_data = self.original_auth_response_data

    def test_openclaw_take_photo_returns_media_marker_and_writes_front_png(self):
        frame_b64 = base64.b64encode(b"front-image-bytes").decode("ascii")
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"OPENCLAW_MEDIA_WORKSPACE": tmpdir}), patch.object(
                main, "get_frame_base64", new=AsyncMock(return_value=frame_b64)
            ):
                response = self.client.get("/photo")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, "MEDIA:front.png")

            expected_path = os.path.join(tmpdir, "front.png")
            self.assertTrue(os.path.exists(expected_path))
            with open(expected_path, "rb") as file:
                self.assertEqual(file.read(), b"front-image-bytes")

    def test_openclaw_vision_returns_caption_and_media_marker(self):
        frame_b64 = base64.b64encode(b"scene-image-bytes").decode("ascii")
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"OPENCLAW_MEDIA_WORKSPACE": tmpdir}), patch.object(
                main, "get_frame_base64", new=AsyncMock(return_value=frame_b64)
            ), patch.object(
                main, "describe_scene", new=AsyncMock(return_value="Desk and cables visible.")
            ):
                response = self.client.post(
                    "/describe-scene",
                    json={"text": "what do you see?"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, "Desk and cables visible.\nMEDIA:scene.png")

            expected_path = os.path.join(tmpdir, "scene.png")
            self.assertTrue(os.path.exists(expected_path))
            with open(expected_path, "rb") as file:
                self.assertEqual(file.read(), b"scene-image-bytes")


if __name__ == "__main__":
    unittest.main()
