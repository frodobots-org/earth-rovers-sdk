import json
import os
import unittest
from unittest.mock import patch

import main


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return json.dumps(self._body)


class _FakeRequestContextManager:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    closed = False

    def __init__(self, response):
        self._response = response
        self.last_request = None

    def request(self, method, url, **kwargs):
        self.last_request = (method, url, kwargs)
        return _FakeRequestContextManager(self._response)


class ExternalRequestTest(unittest.IsolatedAsyncioTestCase):
    async def _call(self, status, body, debug):
        session = _FakeSession(_FakeResponse(status, body))
        env = {"DEBUG": "true"} if debug else {"DEBUG": "false"}
        with patch.dict(os.environ, env), \
                patch.object(main.app.state, "http_session", session, create=True), \
                patch.object(main, "logger") as mock_logger:
            result = await main.external_request("POST", "http://api/sdk/start_ride")
        return result, mock_logger

    async def test_returns_backend_status_and_body(self):
        error_body = {"error": "Bot is currently in use by another user"}
        (status, body), _ = await self._call(403, error_body, debug=False)

        self.assertEqual(status, 403)
        self.assertEqual(body, error_body)

    async def test_logs_real_backend_error_in_debug(self):
        error_body = {"error": "Bot is currently in use by another user"}
        _, mock_logger = await self._call(403, error_body, debug=True)

        mock_logger.error.assert_called_once()
        logged = str(mock_logger.error.call_args)
        self.assertIn("Bot is currently in use by another user", logged)
        self.assertIn("403", logged)

    async def test_does_not_log_error_without_debug(self):
        error_body = {"error": "Bot is currently in use by another user"}
        _, mock_logger = await self._call(403, error_body, debug=False)

        mock_logger.error.assert_not_called()

    async def test_does_not_log_error_on_success(self):
        _, mock_logger = await self._call(200, {"CHANNEL_NAME": "ride_1"}, debug=True)

        mock_logger.error.assert_not_called()


class BackendErrorDetailTest(unittest.TestCase):
    def test_uses_backend_error_message(self):
        detail = main._backend_error_detail(
            {"error": "Bot is currently in use by another user"}, "fallback"
        )
        self.assertEqual(detail, "Bot is currently in use by another user")

    def test_uses_backend_detail_message(self):
        detail = main._backend_error_detail({"detail": "Mission not found"}, "fallback")
        self.assertEqual(detail, "Mission not found")

    def test_falls_back_when_no_message(self):
        self.assertEqual(main._backend_error_detail({}, "fallback"), "fallback")
        self.assertEqual(main._backend_error_detail(None, "fallback"), "fallback")

    def test_falls_back_when_error_is_not_a_plain_string(self):
        detail = main._backend_error_detail({"error": {"mission": ["blank"]}}, "fallback")
        self.assertEqual(detail, "fallback")

    def test_falls_back_when_error_is_blank(self):
        self.assertEqual(main._backend_error_detail({"error": "  "}, "fallback"), "fallback")


if __name__ == "__main__":
    unittest.main()
