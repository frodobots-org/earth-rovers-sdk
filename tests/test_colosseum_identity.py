import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
import main


class ColosseumIdentityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.previous = main.auth_response_data
        self.identity = {'bot_id': 1, 'bot_slug': 'mini', 'device_ref_id': 'abc', 'ride_id': 42}
        main.auth_response_data = {'ROVER_IDENTITY': self.identity, 'RTC_TOKEN': 'never-in-status'}

    async def asyncTearDown(self):
        main.auth_response_data = self.previous
        main.cancel_control_watchdog()

    async def test_status_has_identity_without_tokens(self):
        with patch.object(main.browser_service, 'rtm_health', AsyncMock(return_value={})), patch.dict(os.environ, {'BOT_SLUG': 'mini', 'MISSION_SLUG': 'eval-mini'}):
            response = await main.get_status()
        value = json.loads(response.body)
        self.assertEqual(value['identity'], self.identity)
        self.assertEqual(value['configured_bot_slug'], 'mini')
        self.assertNotIn('never-in-status', response.body.decode())

    async def test_stop_requires_receipt_and_keeps_ride(self):
        with patch.object(main, '_require_confirmed_stop', AsyncMock()) as stop:
            response = await main.stop_motion()
        self.assertTrue(json.loads(response.body)['stop_confirmed'])
        self.assertFalse(json.loads(response.body)['stationary_verified'])
        self.assertEqual(main.auth_response_data['ROVER_IDENTITY']['ride_id'], 42)
        stop.assert_awaited_once()

    async def test_failed_stop_is_not_reported_as_confirmed(self):
        with patch.object(main, '_require_confirmed_stop', AsyncMock(side_effect=HTTPException(503, 'unconfirmed'))):
            with self.assertRaises(HTTPException):
                await main.stop_motion()

    async def test_evaluation_flag_reaches_api_before_acquisition(self):
        response = AsyncMock(return_value=(200, {'ROVER_IDENTITY': self.identity}))
        with patch.object(main, 'external_request', response):
            await main.start_ride({}, 'mini', 'eval-mini', evaluation=True)
        self.assertTrue(response.call_args.kwargs['json']['evaluation'])
