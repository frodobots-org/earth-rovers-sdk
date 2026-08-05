import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import main


class ControlWatchdogTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.auth_response_data = {"BOT_UID": "bot"}
        main.cancel_control_watchdog()

    async def asyncTearDown(self):
        main.cancel_control_watchdog()
        main.auth_response_data = {}

    async def test_motion_without_followup_sends_safety_stop(self):
        send = AsyncMock()
        with (
            patch.object(main, "CONTROL_WATCHDOG_S", 0.05),
            patch.object(main.browser_service, "send_message", send),
        ):
            main.arm_control_watchdog({"linear": 1, "angular": 0, "lamp": 1})
            await asyncio.sleep(0.2)

        send.assert_awaited_once_with({"linear": 0, "angular": 0, "lamp": 1})

    async def test_zero_command_disarms_watchdog(self):
        send = AsyncMock()
        with (
            patch.object(main, "CONTROL_WATCHDOG_S", 0.05),
            patch.object(main.browser_service, "send_message", send),
        ):
            main.arm_control_watchdog({"linear": 1, "angular": 0})
            main.arm_control_watchdog({"linear": 0, "angular": 0})
            await asyncio.sleep(0.2)

        send.assert_not_awaited()

    async def test_fresh_motion_command_resets_the_timer(self):
        send = AsyncMock()
        with (
            patch.object(main, "CONTROL_WATCHDOG_S", 0.15),
            patch.object(main.browser_service, "send_message", send),
        ):
            main.arm_control_watchdog({"linear": 1, "angular": 0})
            await asyncio.sleep(0.1)
            main.arm_control_watchdog({"linear": 1, "angular": 0})
            await asyncio.sleep(0.1)
            # 0.2s elapsed but never 0.15s without a fresh command
            send.assert_not_awaited()
            await asyncio.sleep(0.15)

        send.assert_awaited_once()

    async def test_safety_stop_retries_until_delivered(self):
        send = AsyncMock(side_effect=[RuntimeError("rtm down"), None])
        with (
            patch.object(main, "CONTROL_WATCHDOG_S", 0.05),
            patch.object(main.browser_service, "send_message", send),
        ):
            main.arm_control_watchdog({"linear": 0.5, "angular": 0})
            for _ in range(60):
                await asyncio.sleep(0.05)
                if send.await_count >= 2:
                    break

        self.assertEqual(send.await_count, 2)

    async def test_watchdog_skips_when_session_cleared(self):
        send = AsyncMock()
        with (
            patch.object(main, "CONTROL_WATCHDOG_S", 0.05),
            patch.object(main.browser_service, "send_message", send),
        ):
            main.arm_control_watchdog({"linear": 1, "angular": 0})
            main.auth_response_data = {}
            await asyncio.sleep(0.2)

        send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
