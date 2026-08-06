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


if __name__ == "__main__":
    unittest.main()
