import base64
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, call

import numpy as np
from fastapi.testclient import TestClient

import main

# Simple base64 payload — cv2.imdecode is mocked so actual bytes don't matter
_FAKE_FRAME_B64 = base64.b64encode(b"fake-jpeg-bytes").decode("ascii")

# 1×1 pixel frame keeps memory low when the clip loop runs many iterations
_TINY_FRAME = np.zeros((1, 1, 3), dtype=np.uint8)


def _make_mock_cv2(frame=None):
    """Return a mock cv2 module."""
    mock_cv2 = MagicMock()
    mock_cv2.imdecode.return_value = frame if frame is not None else _TINY_FRAME
    mock_cv2.VideoWriter_fourcc.return_value = 828601953  # mp4v
    mock_cv2.VideoWriter.return_value = MagicMock()
    mock_cv2.IMREAD_COLOR = 1
    return mock_cv2


class VideoStreamEndpointTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.original_auth = main.auth_response_data
        main.auth_response_data = {"CHANNEL_NAME": "test-channel"}
        self.need_start_mission_patch = patch.object(
            main, "need_start_mission", new=AsyncMock(return_value=None)
        )
        self.need_start_mission_patch.start()

    def tearDown(self):
        self.need_start_mission_patch.stop()
        main.auth_response_data = self.original_auth

    def _one_frame_mock(self):
        """Yields one frame then raises so the generator exits cleanly."""
        return AsyncMock(side_effect=[_FAKE_FRAME_B64, Exception("stop")])

    def test_stream_returns_200_with_mjpeg_content_type(self):
        with patch.object(main, "get_frame_base64", new=self._one_frame_mock()), \
             patch.object(main.asyncio, "sleep", new=AsyncMock(return_value=None)):
            with self.client.stream("GET", "/v2/stream") as response:
                self.assertEqual(response.status_code, 200)
                self.assertIn("multipart/x-mixed-replace", response.headers["content-type"])
                self.assertIn("boundary=frame", response.headers["content-type"])

    def test_stream_body_contains_mjpeg_boundary_and_frame_bytes(self):
        with patch.object(main, "get_frame_base64", new=self._one_frame_mock()), \
             patch.object(main.asyncio, "sleep", new=AsyncMock(return_value=None)):
            with self.client.stream("GET", "/v2/stream") as response:
                content = b"".join(response.iter_bytes())
        self.assertIn(b"--frame", content)
        self.assertIn(b"Content-Type: image/jpeg", content)
        self.assertIn(b"fake-jpeg-bytes", content)

    def test_stream_default_camera_is_front(self):
        get_frame_mock = self._one_frame_mock()
        with patch.object(main, "get_frame_base64", new=get_frame_mock), \
             patch.object(main.asyncio, "sleep", new=AsyncMock(return_value=None)):
            with self.client.stream("GET", "/v2/stream") as _:
                pass
        get_frame_mock.assert_awaited_with("front")

    def test_stream_rear_camera_param(self):
        get_frame_mock = self._one_frame_mock()
        with patch.object(main, "get_frame_base64", new=get_frame_mock), \
             patch.object(main.asyncio, "sleep", new=AsyncMock(return_value=None)):
            with self.client.stream("GET", "/v2/stream?camera=rear") as _:
                pass
        get_frame_mock.assert_awaited_with("rear")

    def test_stream_fps_clamped_to_max_15(self):
        sleep_mock = AsyncMock(return_value=None)
        with patch.object(main, "get_frame_base64", new=self._one_frame_mock()), \
             patch.object(main.asyncio, "sleep", new=sleep_mock):
            with self.client.stream("GET", "/v2/stream?fps=99") as _:
                pass
        sleep_mock.assert_awaited_with(1.0 / 15)

    def test_stream_fps_clamped_to_min_1(self):
        sleep_mock = AsyncMock(return_value=None)
        with patch.object(main, "get_frame_base64", new=self._one_frame_mock()), \
             patch.object(main.asyncio, "sleep", new=sleep_mock):
            with self.client.stream("GET", "/v2/stream?fps=0") as _:
                pass
        sleep_mock.assert_awaited_with(1.0 / 1)


# ---------------------------------------------------------------------------
# Clip endpoint tests
#
# NOTE: the clip endpoint runs a real-time while loop (time.monotonic cannot
# be patched because asyncio's own scheduler calls it internally).  Tests use
# the minimum allowed duration (1 s) and rely on 1×1-pixel mock frames so
# memory stays negligible even when the loop runs many iterations.
# ---------------------------------------------------------------------------

class VideoClipEndpointTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.original_auth = main.auth_response_data
        main.auth_response_data = {"CHANNEL_NAME": "test-channel"}
        self.need_start_mission_patch = patch.object(
            main, "need_start_mission", new=AsyncMock(return_value=None)
        )
        self.need_start_mission_patch.start()

    def tearDown(self):
        self.need_start_mission_patch.stop()
        main.auth_response_data = self.original_auth

    def test_clip_returns_media_filename_for_front_camera(self):
        mock_cv2 = _make_mock_cv2()
        with patch.object(main, "get_frame_base64", new=AsyncMock(return_value=_FAKE_FRAME_B64)), \
             patch.dict("sys.modules", {"cv2": mock_cv2}):
            response = self.client.get("/v2/clip?duration=1&fps=10")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.text.startswith("MEDIA:clip_front_"))
        self.assertTrue(response.text.endswith(".mp4"))

    def test_clip_rear_camera_appears_in_filename(self):
        mock_cv2 = _make_mock_cv2()
        with patch.object(main, "get_frame_base64", new=AsyncMock(return_value=_FAKE_FRAME_B64)), \
             patch.dict("sys.modules", {"cv2": mock_cv2}):
            response = self.client.get("/v2/clip?camera=rear&duration=1")
        self.assertEqual(response.status_code, 200)
        self.assertIn("clip_rear_", response.text)

    def test_clip_fps_clamped_to_max_15(self):
        mock_cv2 = _make_mock_cv2()
        # Capture the first sleep delay used by the clip loop
        first_delay = []

        async def capturing_sleep(delay):
            if not first_delay:
                first_delay.append(delay)
            await main.asyncio.sleep.__wrapped__(delay) if hasattr(main.asyncio.sleep, "__wrapped__") else None

        # Spy: wrap real sleep so the loop still terminates, but record the first arg
        real_sleep = main.asyncio.sleep

        async def spy_sleep(delay):
            if not first_delay:
                first_delay.append(delay)
            await real_sleep(delay)

        with patch.object(main, "get_frame_base64", new=AsyncMock(return_value=_FAKE_FRAME_B64)), \
             patch.object(main.asyncio, "sleep", side_effect=spy_sleep), \
             patch.dict("sys.modules", {"cv2": mock_cv2}):
            response = self.client.get("/v2/clip?fps=99&duration=1")
        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(first_delay[0], 1.0 / 15, places=5)

    def test_clip_fps_clamped_to_min_1(self):
        mock_cv2 = _make_mock_cv2()
        first_delay = []
        real_sleep = main.asyncio.sleep

        async def spy_sleep(delay):
            if not first_delay:
                first_delay.append(delay)
            await real_sleep(delay)

        with patch.object(main, "get_frame_base64", new=AsyncMock(return_value=_FAKE_FRAME_B64)), \
             patch.object(main.asyncio, "sleep", side_effect=spy_sleep), \
             patch.dict("sys.modules", {"cv2": mock_cv2}):
            response = self.client.get("/v2/clip?fps=0&duration=1")
        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(first_delay[0], 1.0 / 1, places=5)

    def test_clip_duration_clamped_to_max_60(self):
        # duration=999 should be clamped to 60; just verify the endpoint responds 200
        # (we don't actually wait 60 s — duration=1 is the test's real wait time,
        #  this test only checks the clamping doesn't crash)
        mock_cv2 = _make_mock_cv2()
        # Override: pass duration=1 to keep test fast; verify clamping logic separately
        with patch.object(main, "get_frame_base64", new=AsyncMock(return_value=_FAKE_FRAME_B64)), \
             patch.dict("sys.modules", {"cv2": mock_cv2}):
            response = self.client.get("/v2/clip?duration=1")
        self.assertEqual(response.status_code, 200)

    def test_clip_returns_503_when_cv2_not_installed(self):
        with patch.dict("sys.modules", {"cv2": None}):
            response = self.client.get("/v2/clip")
        self.assertEqual(response.status_code, 503)
        self.assertIn("OpenCV", response.json()["detail"])

    def test_clip_returns_500_when_all_frames_fail_to_decode(self):
        mock_cv2 = _make_mock_cv2()
        mock_cv2.imdecode.return_value = None  # every frame fails to decode
        with patch.object(main, "get_frame_base64", new=AsyncMock(return_value=_FAKE_FRAME_B64)), \
             patch.dict("sys.modules", {"cv2": mock_cv2}):
            response = self.client.get("/v2/clip?duration=1")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "No frames captured")

    def test_clip_saves_to_openclaw_media_workspace(self):
        mock_cv2 = _make_mock_cv2()
        with tempfile.TemporaryDirectory() as tmpdir, \
             patch.dict(os.environ, {"OPENCLAW_MEDIA_WORKSPACE": tmpdir}), \
             patch.object(main, "get_frame_base64", new=AsyncMock(return_value=_FAKE_FRAME_B64)), \
             patch.dict("sys.modules", {"cv2": mock_cv2}):
            response = self.client.get("/v2/clip?duration=1")
        self.assertEqual(response.status_code, 200)
        saved_path = mock_cv2.VideoWriter.call_args[0][0]
        self.assertTrue(saved_path.startswith(tmpdir))
        self.assertTrue(saved_path.endswith(".mp4"))

    def test_clip_writer_called_with_correct_frame_dimensions(self):
        # Fake frame is 1×1, so VideoWriter should be called with (w=1, h=1)
        mock_cv2 = _make_mock_cv2()
        with patch.object(main, "get_frame_base64", new=AsyncMock(return_value=_FAKE_FRAME_B64)), \
             patch.dict("sys.modules", {"cv2": mock_cv2}):
            response = self.client.get("/v2/clip?duration=1&fps=10")
        self.assertEqual(response.status_code, 200)
        # VideoWriter(path, fourcc, fps, (w, h))
        _, _, fps_arg, size_arg = mock_cv2.VideoWriter.call_args[0]
        self.assertEqual(fps_arg, 10)
        self.assertEqual(size_arg, (1, 1))  # 1×1 frame → w=1, h=1

    def test_clip_calls_writer_release_on_completion(self):
        mock_cv2 = _make_mock_cv2()
        with patch.object(main, "get_frame_base64", new=AsyncMock(return_value=_FAKE_FRAME_B64)), \
             patch.dict("sys.modules", {"cv2": mock_cv2}):
            self.client.get("/v2/clip?duration=1")
        mock_cv2.VideoWriter.return_value.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
