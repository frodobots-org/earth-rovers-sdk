import unittest
from itertools import repeat
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main


def reset_voice_loop_state():
    main.voice_loop_task = None
    main.voice_loop_state.update(
        {
            "running": False,
            "status": "idle",
            "duration_ms": None,
            "listen_windows": None,
            "poll_delay_ms": None,
            "started_at": None,
            "last_transcript": "",
            "last_attempts": 0,
            "last_hook_status_code": None,
            "last_error": None,
            "last_timings": {},
            "iterations": 0,
            "forwarded_count": 0,
        }
    )


def reset_track_color_state():
    main.track_color_task = None
    main.track_color_state.update(
        {
            "running": False,
            "status": "idle",
            "color": None,
            "duration_seconds": None,
            "started_at": None,
            "linear": 0.0,
            "angular": 0.0,
            "fill_pct": None,
            "camera": None,
            "last_error": None,
        }
    )


def profiled_result(transcript, timings=None):
    return {
        "transcript": transcript,
        "timings": timings
        or {
            "capture_ms": 1.0,
            "decode_ms": 1.0,
            "tempfile_ms": 1.0,
            "stt_ms": 1.0,
            "total_ms": 4.0,
        },
    }


class FakeTask:
    def __init__(self):
        self._done = False

    def done(self):
        return self._done

    def cancel(self):
        self._done = True

    def __await__(self):
        async def _noop():
            return None

        return _noop().__await__()


class VoiceEndpointsTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.original_auth_response_data = main.auth_response_data
        main.auth_response_data = {"CHANNEL_NAME": "test-channel"}
        reset_voice_loop_state()
        reset_track_color_state()
        self.need_start_mission_patch = patch.object(
            main, "need_start_mission", new=AsyncMock(return_value=None)
        )
        self.need_start_mission_patch.start()

    def tearDown(self):
        self.need_start_mission_patch.stop()
        main.auth_response_data = self.original_auth_response_data
        reset_voice_loop_state()
        reset_track_color_state()

    def test_voice_listen_accepts_valid_duration(self):
        with patch.object(
            main,
            "_record_and_transcribe_with_metrics",
            new=AsyncMock(return_value=profiled_result("hello rover")),
        ):
            response = self.client.post("/voice-listen", json={"duration_ms": 1800})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["duration_ms"], 1800)
        self.assertEqual(response.json()["transcript"], "hello rover")
        self.assertIn("timings", response.json())

    def test_speak_uses_voice_browser_service_with_generated_static_audio(self):
        with patch.object(
            main,
            "generate_speech",
            new=AsyncMock(return_value="static/generated-audio.mp3"),
        ) as generate_mock, patch.object(
            main.voice_browser_service,
            "speak",
            new=AsyncMock(return_value="done"),
        ) as voice_speak_mock, patch.object(
            main.browser_service,
            "speak",
            new=AsyncMock(return_value="done"),
        ) as browser_speak_mock:
            response = self.client.post("/speak", json={"text": "hello rover"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "Speech sent to rover")
        generate_mock.assert_awaited_once_with("hello rover", "static/tts_output")
        self.assertEqual(voice_speak_mock.await_count, 1)
        spoken_url = voice_speak_mock.await_args[0][0]
        self.assertTrue(
            spoken_url.startswith(
                "http://127.0.0.1:8000/static/generated-audio.mp3?v="
            ),
            msg=f"unexpected speak URL: {spoken_url!r}",
        )
        browser_speak_mock.assert_not_awaited()

    def test_infers_turn_left_from_whisper_one_left(self):
        normalized = main._infer_normalized_voice_command("One left, 90 degrees.")
        self.assertEqual(normalized, "turn left 90 degrees")

    def test_infers_turn_left_defaults_to_90_degrees(self):
        normalized = main._infer_normalized_voice_command("turn left")
        self.assertEqual(normalized, "turn left 90 degrees")

    def test_infers_turn_right_defaults_to_90_degrees(self):
        normalized = main._infer_normalized_voice_command("turn right")
        self.assertEqual(normalized, "turn right 90 degrees")

    def test_infers_slight_turn_left_as_30_degrees(self):
        normalized = main._infer_normalized_voice_command("turn slightly left")
        self.assertEqual(normalized, "turn left 30 degrees")

    def test_infers_slight_turn_right_as_30_degrees(self):
        normalized = main._infer_normalized_voice_command("turn slightly right")
        self.assertEqual(normalized, "turn right 30 degrees")

    def test_build_hook_message_includes_normalized_and_raw_text(self):
        message = main._build_openclaw_hook_message(
            "One left, 90 degrees.",
            "turn left 90 degrees",
        )
        self.assertIn("Task: Hook", message)
        self.assertIn("Normalized Rover Command: turn left 90 degrees", message)
        self.assertIn("Raw Transcript: One left, 90 degrees.", message)

    def test_voice_listen_rejects_invalid_duration_type(self):
        with patch.object(
            main,
            "_record_and_transcribe_with_metrics",
            new=AsyncMock(return_value=profiled_result("unused")),
        ):
            response = self.client.post("/voice-listen", json={"duration_ms": "abc"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "duration_ms must be an integer")

    def test_voice_listen_rejects_negative_duration(self):
        response = self.client.post("/voice-listen", json={"duration_ms": -5})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "duration_ms must be greater than 0")

    def test_voice_listen_clamps_oversized_duration(self):
        with patch.object(
            main,
            "_record_and_transcribe_with_metrics",
            new=AsyncMock(return_value=profiled_result("clamped")),
        ):
            response = self.client.post("/voice-listen", json={"duration_ms": 99999})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["duration_ms"], 10000)

    def test_voice_listen_rejects_invalid_json_body(self):
        response = self.client.post(
            "/voice-listen",
            data="{invalid json",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Invalid JSON body")

    def test_voice_command_returns_silence_when_no_transcript(self):
        with patch.object(
            main,
            "_record_and_transcribe_with_metrics",
            new=AsyncMock(return_value=profiled_result(None)),
        ):
            response = self.client.post("/voice-command", json={"duration_ms": 2000})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "silence")
        self.assertEqual(response.json()["duration_ms"], 2000)
        self.assertEqual(response.json()["attempts"], 3)
        self.assertIn("timings", response.json())

    def test_voice_command_forwards_to_hook(self):
        hook_mock = AsyncMock(
            return_value={
                "status_code": 200,
                "response": "ok",
                "timings": {"hook_request_ms": 12.0},
            }
        )
        with patch.object(
            main,
            "_record_and_transcribe_with_metrics",
            new=AsyncMock(return_value=profiled_result("turn left")),
        ), patch.object(main, "_send_to_openclaw_hook", new=hook_mock):
            response = self.client.post("/voice-command", json={"duration_ms": 2600})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "forwarded")
        self.assertEqual(response.json()["hook_status_code"], 200)
        self.assertEqual(response.json()["attempts"], 1)
        self.assertEqual(response.json()["timings"]["hook_request_ms"], 12.0)
        hook_mock.assert_awaited_once_with("turn left", 2600)

    def test_voice_command_normalizes_follow_common_color_card(self):
        self.assertEqual(
            main._infer_normalized_voice_command("can you folow black color card"),
            "follow black card",
        )
        self.assertEqual(
            main._infer_normalized_voice_command("please track the grey card"),
            "follow gray card",
        )
        self.assertEqual(
            main._infer_normalized_voice_command("follow sky blue card"),
            "follow skyblue card",
        )

    def test_openclaw_hook_message_lists_common_tracking_colors(self):
        message = main._build_openclaw_hook_message(
            "can you folow black color card",
            "follow black card",
        )

        self.assertIn("Normalized Rover Command: follow black card", message)
        self.assertIn("Supported tracking colors are", message)
        self.assertIn("black", message)
        self.assertIn("POST /track-color", message)

    def test_track_color_endpoint_accepts_common_color_alias(self):
        fake_task = FakeTask()

        def fake_create_task(coro):
            coro.close()
            return fake_task

        with patch.object(main.asyncio, "create_task", side_effect=fake_create_task):
            response = self.client.post("/track-color", json={"color": "grey"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "started")
        self.assertEqual(response.json()["color"], "gray")

    def test_color_blob_detection_ignores_tiny_false_hits(self):
        frame = main.np.full((100, 100, 3), 255, dtype=main.np.uint8)
        frame[10:15, 10:15] = (0, 0, 0)

        self.assertIsNone(main._detect_color_blob(frame, "black"))

    def test_color_blob_detection_accepts_card_sized_hit(self):
        frame = main.np.full((100, 100, 3), 255, dtype=main.np.uint8)
        frame[20:60, 20:60] = (0, 0, 0)

        blob = main._detect_color_blob(frame, "black")

        self.assertIsNotNone(blob)
        self.assertGreater(blob[1], main._TRACK_COLOR_MIN_DETECT_FILL)

    def test_color_blob_detection_accepts_distant_card_hit(self):
        frame = main.np.full((480, 640, 3), 255, dtype=main.np.uint8)
        frame[220:234, 300:314] = (0, 0, 0)

        blob = main._detect_color_blob(frame, "black")

        self.assertIsNotNone(blob)
        self.assertGreater(blob[1], main._TRACK_COLOR_MIN_DETECT_FILL)

    def test_voice_command_retries_until_transcript_detected(self):
        record_mock = AsyncMock(
            side_effect=[
                profiled_result(None),
                profiled_result(None),
                profiled_result("turn left"),
            ]
        )
        hook_mock = AsyncMock(
            return_value={
                "status_code": 200,
                "response": "ok",
                "timings": {"hook_request_ms": 9.0},
            }
        )
        with patch.object(main, "_record_and_transcribe_with_metrics", new=record_mock), patch.object(
            main, "_send_to_openclaw_hook", new=hook_mock
        ), patch.object(main.asyncio, "sleep", new=AsyncMock(return_value=None)):
            response = self.client.post(
                "/voice-command", json={"duration_ms": 1800, "listen_windows": 3}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "forwarded")
        self.assertEqual(response.json()["attempts"], 3)
        self.assertEqual(record_mock.await_count, 3)
        hook_mock.assert_awaited_once_with("turn left", 1800)

    def test_voice_command_rejects_invalid_listen_windows(self):
        response = self.client.post(
            "/voice-command", json={"duration_ms": 2000, "listen_windows": "abc"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "listen_windows must be an integer")

    def test_voice_command_loop_start_starts_background_listener(self):
        fake_task = FakeTask()
        def fake_create_task(coro):
            coro.close()
            return fake_task

        with patch.object(main.asyncio, "create_task", side_effect=fake_create_task):
            response = self.client.post(
                "/voice-command-loop/start",
                json={"duration_ms": 2200, "listen_windows": 4, "poll_delay_ms": 350},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "started")
        self.assertEqual(response.json()["running"], True)
        self.assertEqual(response.json()["duration_ms"], 2200)
        self.assertEqual(response.json()["listen_windows"], 4)
        self.assertEqual(response.json()["poll_delay_ms"], 350)

    def test_voice_command_loop_start_rejects_invalid_poll_delay(self):
        response = self.client.post(
            "/voice-command-loop/start",
            json={"duration_ms": 2000, "poll_delay_ms": "abc"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "poll_delay_ms must be an integer")

    def test_voice_command_returns_hook_error_without_local_fallback(self):
        send_message_mock = AsyncMock()
        with patch.object(
            main,
            "_record_and_transcribe_with_metrics",
            new=AsyncMock(return_value=profiled_result("move forward")),
        ), patch.object(
            main,
            "_send_to_openclaw_hook",
            new=AsyncMock(
                side_effect=main.HTTPException(status_code=502, detail="mock hook down")
            ),
        ), patch.object(main.browser_service, "send_message", new=send_message_mock):
            response = self.client.post("/voice-command", json={"duration_ms": 2000})

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["status"], "hook_error")
        self.assertEqual(response.json()["attempts"], 1)
        send_message_mock.assert_not_awaited()

    def test_turn_endpoint_sends_turn_then_stop(self):
        data_mock = AsyncMock(
            side_effect=[
                {"orientation": 16, "timestamp": "1.0"},
                {"orientation": 16, "timestamp": "1.1"},
                {"orientation": 286, "timestamp": "1.2"},
                {"orientation": 286, "timestamp": "1.3"},
            ]
        )
        send_message_mock = AsyncMock(return_value={"success": True})

        with patch.object(main.browser_service, "data", new=data_mock), patch.object(
            main.browser_service, "send_message", new=send_message_mock
        ):
            response = self.client.post("/turn", json={"degrees": 90, "timeout": 2})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["requested"], 90.0)
        self.assertAlmostEqual(payload["actual"], -90.0)
        self.assertEqual(len(payload["steps"]), 1)
        self.assertFalse(payload["steps"][0]["timed_out"])
        self.assertGreaterEqual(send_message_mock.await_count, 3)
        first_cmd = send_message_mock.await_args_list[0].args[0]
        self.assertGreater(first_cmd["angular"], 0)
        self.assertEqual(send_message_mock.await_args_list[-1].args[0]["angular"], 0)

    def test_turn_endpoint_times_out_on_nonadvancing_synthetic_heading_and_stops(self):
        data_mock = AsyncMock(
            side_effect=repeat({"orientation": 16, "timestamp": "1.0"})
        )
        send_message_mock = AsyncMock(return_value={"success": True})

        with patch.object(main.browser_service, "data", new=data_mock), patch.object(
            main.browser_service, "send_message", new=send_message_mock
        ):
            response = self.client.post(
                "/turn",
                json={"degrees": 90, "timeout": 2, "control_interval": 0.05},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["requested"], 90.0)
        self.assertNotIn("aborted", payload["steps"][0])
        self.assertTrue(payload["steps"][0]["timed_out"])
        self.assertGreaterEqual(send_message_mock.await_count, 4)
        self.assertGreater(send_message_mock.await_args_list[0].args[0]["angular"], 0)
        angular_commands = [call.args[0]["angular"] for call in send_message_mock.await_args_list]
        self.assertTrue(all(value > 0 for value in angular_commands[:-3]))
        self.assertEqual(angular_commands[-3:], [0, 0, 0])


if __name__ == "__main__":
    unittest.main()
