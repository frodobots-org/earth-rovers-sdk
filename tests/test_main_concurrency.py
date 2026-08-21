import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import main


TOKENS = {
    "CHANNEL_NAME": "channel",
    "RTC_TOKEN": "rtc",
    "RTM_TOKEN": "rtm",
    "USERID": 123,
    "APP_ID": "app",
    "BOT_UID": "bot",
    "SPECTATOR_USERID": 456,
    "SPECTATOR_RTC_TOKEN": "spectator",
    "BOT_TYPE": "mini",
}


class MainConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.auth_response_data = {}
        main.checkpoints_list_data = {}
        main.mission_completion_data = None
        main.auth_lock = None
        main.auth_lock_loop = None

    async def test_concurrent_auth_starts_ride_once(self):
        async def start_once(*_args):
            await asyncio.sleep(0.01)
            return TOKENS

        environment = {
            "SDK_API_TOKEN": "sdk-token",
            "BOT_SLUG": "rover",
            "MISSION_SLUG": "mission",
            "CHANNEL_NAME": "",
            "RTC_TOKEN": "",
            "RTM_TOKEN": "",
            "USERID": "",
            "APP_ID": "",
            "BOT_UID": "",
        }
        start = AsyncMock(side_effect=start_once)
        with (
            patch.dict(os.environ, environment),
            patch.object(main, "start_ride", start),
        ):
            first, second = await asyncio.gather(main.auth_common(), main.auth_common())

        self.assertEqual(first, second)
        self.assertEqual(start.await_count, 1)

    async def test_auth_replaces_lock_from_an_old_event_loop_generation(self):
        stale_lock = asyncio.Lock()
        main.auth_lock = stale_lock
        main.auth_lock_loop = object()
        environment = {
            "CHANNEL_NAME": "channel",
            "RTC_TOKEN": "rtc",
            "RTM_TOKEN": "rtm",
            "USERID": "123",
            "APP_ID": "app",
            "BOT_UID": "bot",
        }

        with patch.dict(os.environ, environment):
            result = await main.auth_common()

        self.assertEqual(result["CHANNEL_NAME"], "channel")
        self.assertIsNot(main.auth_lock, stale_lock)
        self.assertIs(main.auth_lock_loop, asyncio.get_running_loop())

    async def test_mission_history_does_not_reauthenticate_or_restart_ride(self):
        response = {"mission_rides": []}
        request = AsyncMock(return_value=(200, response))
        auth = AsyncMock()
        with (
            patch.dict(os.environ, {"SDK_API_TOKEN": "sdk-token", "BOT_SLUG": "rover"}),
            patch.object(main, "external_request", request),
            patch.object(main, "auth_common", auth),
        ):
            result = await main.missions_history()

        auth.assert_not_awaited()
        self.assertEqual(json.loads(result.body), response)

    async def test_cancelled_feed_wait_unsubscribes(self):
        class UnavailableFeed:
            def __init__(self):
                self.queue = asyncio.Queue()
                self.unsubscribed = False

            async def subscribe(self, *_args, **_kwargs):
                return self.queue

            async def unsubscribe(self, queue):
                self.assert_queue = queue
                self.unsubscribed = True

        feed = UnavailableFeed()
        main.auth_response_data = TOKENS.copy()
        with patch.dict(main.feed_broadcasters, {"front": feed}):
            request = asyncio.create_task(main.feed(view="front"))
            await asyncio.sleep(0)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request

        self.assertTrue(feed.unsubscribed)
        self.assertIs(feed.assert_queue, feed.queue)

    async def test_mission_progress_endpoint_uses_local_cache(self):
        main.auth_response_data = TOKENS.copy()
        main.checkpoints_list_data = {
            "checkpoints_list": [
                {"sequence": 1},
                {"sequence": 2},
                {"sequence": 3},
            ],
            "latest_scanned_checkpoint": 1,
        }

        with patch.dict(os.environ, {"MISSION_SLUG": "mission"}):
            response = await main.get_mission_progress()

        self.assertEqual(
            json.loads(response.body),
            {
                "mission_started": True,
                "mission_completed": False,
                "latest_scanned_checkpoint": 1,
                "next_checkpoint_sequence": 2,
            },
        )


if __name__ == "__main__":
    unittest.main()
