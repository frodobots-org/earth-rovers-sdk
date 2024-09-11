import os
import time
import asyncio
from pyppeteer import launch
from concurrent.futures import ThreadPoolExecutor


class BrowserService:
    def __init__(self, max_browsers=3):
        self.max_browsers = max_browsers
        self.browser_pool = asyncio.Queue(maxsize=max_browsers)
        self.default_viewport = {"width": 3840, "height": 2160}
        self.lock = asyncio.Lock()

    async def initialize_browser_pool(self):
        async with self.lock:
            if self.browser_pool.empty():
                for _ in range(self.max_browsers):
                    browser = await self._create_browser()
                    await self.browser_pool.put(browser)

    async def _create_browser(self):
        executable_path = os.getenv(
            "CHROME_EXECUTABLE_PATH",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        browser = await launch(
            executablePath=executable_path,
            headless=True,
            args=[
                "--ignore-certificate-errors",
                "--no-sandbox",
                f"--window-size={self.default_viewport['width']},{self.default_viewport['height']}",
            ],
        )
        return browser

    async def _get_browser(self):
        await self.initialize_browser_pool()
        return await self.browser_pool.get()

    async def _return_browser(self, browser):
        await self.browser_pool.put(browser)

    async def take_screenshot(self, video_output_folder: str, elements: list):
        browser = await self._get_browser()
        try:
            page = await browser.newPage()
            await page.setViewport(self.default_viewport)
            await page.setExtraHTTPHeaders({"Accept-Language": "en-US,en;q=0.9"})
            await page.goto("http://127.0.0.1:8000/sdk", {"waitUntil": "networkidle2"})
            await page.click("#join")
            await page.waitForSelector("video")
            await page.waitForSelector("#map")
            await page.waitFor(2000)

            element_map = {"front": "#player-1000", "rear": "#player-1001", "map": "#map"}
            screenshots = {}

            for name in elements:
                if name in element_map:
                    element_id = element_map[name]
                    output_path = f"{video_output_folder}/{name}.png"
                    element = await page.querySelector(element_id)
                    if element:
                        start_time = time.time()
                        await element.screenshot({"path": output_path})
                        end_time = time.time()
                        elapsed_time = (end_time - start_time) * 1000
                        print(f"Screenshot for {name} took {elapsed_time:.2f} ms")
                        screenshots[name] = output_path
                    else:
                        print(f"Element {element_id} not found")
                else:
                    print(f"Invalid element name: {name}")

            return screenshots
        finally:
            await page.close()
            await self._return_browser(browser)

    async def data(self) -> dict:
        browser = await self._get_browser()
        try:
            page = await browser.newPage()
            await page.goto("http://127.0.0.1:8000/sdk", {"waitUntil": "networkidle2"})
            bot_data = await page.evaluate("() => window.rtm_data")
            return bot_data
        finally:
            await page.close()
            await self._return_browser(browser)

    async def send_message(self, message: dict):
        browser = await self._get_browser()
        try:
            page = await browser.newPage()
            await page.goto("http://127.0.0.1:8000/sdk", {"waitUntil": "networkidle2"})
            await page.evaluate("(message) => window.sendMessage(message)", message)
        finally:
            await page.close()
            await self._return_browser(browser)

    async def close_all_browsers(self):
        while not self.browser_pool.empty():
            browser = await self.browser_pool.get()
            await browser.close()
