"""Certificate verification must stay enabled on every browser launch path."""
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import Error as PlaywrightError
from browser_service import BrowserService


class BrowserTlsPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def launch(self, executable=None, fallback=False, navigation_error=False):
        page = AsyncMock()
        if navigation_error:
            page.goto.side_effect = PlaywrightError("net::ERR_CERT_AUTHORITY_INVALID")
        context = AsyncMock()
        context.new_page.return_value = page
        browser = AsyncMock()
        browser.new_context.return_value = context
        chromium = MagicMock()
        chromium.executable_path = "/test/chromium"
        chromium.launch = AsyncMock(return_value=browser)
        if fallback:
            chromium.launch.side_effect = [PlaywrightError("Chrome not installed"), browser]
        playwright = MagicMock()
        playwright.chromium = chromium
        playwright.stop = AsyncMock()
        service = BrowserService()
        service._playwright = playwright
        with patch.dict(os.environ, {"CHROME_EXECUTABLE_PATH": executable or ""}), patch("browser_service.os.path.exists", return_value=True):
            if navigation_error:
                with self.assertRaisesRegex(PlaywrightError, "ERR_CERT_AUTHORITY_INVALID"):
                    await service._launch()
                self.assertFalse(service._ready)
            else:
                await service._launch()
                self.assertTrue(service._ready)
        for call in chromium.launch.call_args_list:
            self.assertNotIn("--ignore-certificate-errors", call.kwargs["args"])
        self.assertFalse(browser.new_context.call_args.kwargs.get("ignore_https_errors", False))
        return chromium

    async def test_installed_chrome_verifies_certificates(self):
        chromium = await self.launch()
        self.assertEqual(chromium.launch.call_args.kwargs["channel"], "chrome")

    async def test_executable_override_verifies_certificates(self):
        chromium = await self.launch(executable="/test/custom-chrome")
        self.assertEqual(chromium.launch.call_args.kwargs["executable_path"], "/test/custom-chrome")

    async def test_bundled_fallback_verifies_certificates(self):
        chromium = await self.launch(fallback=True)
        self.assertEqual(chromium.launch.await_count, 2)

    async def test_invalid_certificate_is_not_silently_retried(self):
        chromium = await self.launch(navigation_error=True)
        self.assertEqual(chromium.launch.await_count, 1)
