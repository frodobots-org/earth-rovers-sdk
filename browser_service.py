import asyncio
import logging
import os
import time

from dotenv import load_dotenv
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

load_dotenv()

logger = logging.getLogger("browser_service")

# Configuration from environment variables with defaults
FORMAT = os.getenv("IMAGE_FORMAT", "png")
QUALITY = float(os.getenv("IMAGE_QUALITY", "1.0"))

if FORMAT not in ["png", "jpeg", "webp"]:
    raise ValueError("Invalid image format. Supported formats: png, jpeg, webp")

if QUALITY < 0 or QUALITY > 1:
    raise ValueError("Invalid image quality. Quality should be between 0 and 1")

SDK_PAGE_URL = os.getenv("SDK_PAGE_URL", "http://127.0.0.1:8000/sdk")


class BrowserService:
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._lock = asyncio.Lock()
        self._viewport = {"width": 3840, "height": 2160}

    @property
    def is_ready(self) -> bool:
        return bool(
            self._page
            and not self._page.is_closed()
            and self._browser
            and self._browser.is_connected()
        )

    async def ensure_page(self):
        # Lock-free fast path: concurrent /control, /feed and /v2 calls must
        # not serialize on the init lock once the page is up.
        if self.is_ready:
            return self._page
        async with self._lock:
            if self.is_ready:
                return self._page
            await self._teardown()
            await self._launch()
            return self._page

    async def _launch(self):
        try:
            if self._playwright is None:
                self._playwright = await async_playwright().start()

            # Playwright manages its own Chromium; CHROME_EXECUTABLE_PATH
            # remains an override (e.g. real Chrome for H.264 streams).
            executable_path = os.getenv("CHROME_EXECUTABLE_PATH") or None
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                executable_path=executable_path,
                args=[
                    "--ignore-certificate-errors",
                    "--no-sandbox",
                    "--autoplay-policy=no-user-gesture-required",
                    "--use-fake-ui-for-media-stream",
                    "--disable-application-cache",
                    "--disk-cache-size=0",
                ],
            )
            self._context = await self._browser.new_context(
                viewport=self._viewport,
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            self._page = await self._context.new_page()
            await self._page.goto(SDK_PAGE_URL, wait_until="networkidle")
            await self._page.click("#join")
            await self._page.wait_for_selector("video")
            await self._page.wait_for_selector("#map")

            try:
                await self._page.wait_for_function(
                    "() => typeof remoteUsers !== 'undefined'"
                    " && Object.values(remoteUsers).some((u) => u.videoTrack)",
                    timeout=15000,
                )
            except PlaywrightError:
                await self._page.wait_for_timeout(2000)

            call = f"""() => {{
                window.initializeImageParams({{
                    imageFormat: "{FORMAT}",
                    imageQuality: {QUALITY}
                }});
            }}"""
            await self._page.evaluate(call)
            logger.info("Headless browser connected to %s", SDK_PAGE_URL)
        except Exception as e:
            logger.error("Error initializing browser: %s", e)
            await self._teardown()
            raise

    async def _teardown(self):
        for target in (self._page, self._context, self._browser):
            if target:
                try:
                    await target.close()
                except Exception:
                    pass
        self._page = None
        self._context = None
        self._browser = None

    async def _run(self, fn):
        page = await self.ensure_page()
        try:
            return await fn(page)
        except PlaywrightError as e:
            logger.warning("Browser call failed (%s); relaunching and retrying", e)
            async with self._lock:
                await self._teardown()
            page = await self.ensure_page()
            return await fn(page)

    async def warmup(self, max_attempts: int = 5):
        delay = 2
        for attempt in range(1, max_attempts + 1):
            try:
                await self.ensure_page()
                return True
            except Exception as e:
                logger.warning(
                    "Browser warm-up attempt %s/%s failed: %s",
                    attempt,
                    max_attempts,
                    e,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
        logger.warning(
            "Browser warm-up gave up after %s attempts;"
            " it will initialize lazily on the next request",
            max_attempts,
        )
        return False

    async def take_screenshot(self, video_output_folder: str, elements: list):
        element_map = {"front": "#player-1000", "rear": "#player-1001", "map": "#map"}

        async def capture(page):
            screenshots = {}
            for name in elements:
                if name not in element_map:
                    logger.warning("Invalid element name: %s", name)
                    continue
                locator = page.locator(element_map[name])
                if await locator.count() == 0:
                    logger.warning("Element %s not found", element_map[name])
                    continue
                output_path = f"{video_output_folder}/{name}.png"
                start_time = time.time()
                await locator.screenshot(path=output_path, timeout=5000)
                elapsed_ms = (time.time() - start_time) * 1000
                logger.info("Screenshot for %s took %.2f ms", name, elapsed_ms)
                screenshots[name] = output_path
            return screenshots

        return await self._run(capture)

    async def data(self) -> dict:
        return await self._run(lambda page: page.evaluate("() => window.rtm_data"))

    async def front(self) -> str:
        return await self._run(
            lambda page: page.evaluate("() => getLastBase64Frame(1000) || null")
        )

    async def rear(self) -> str:
        return await self._run(
            lambda page: page.evaluate("() => getLastBase64Frame(1001) || null")
        )

    async def send_message(self, message: dict):
        return await self._run(
            lambda page: page.evaluate(
                "(message) => window.sendMessage(message)", message
            )
        )

    async def speak(self, audio_url: str):
        return await self._run(
            lambda page: page.evaluate(
                "async (audioUrl) => await window.playAudioToRover(audioUrl)",
                audio_url,
            )
        )

    async def close(self):
        async with self._lock:
            await self._teardown()
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

    # Backward-compatible alias
    async def close_browser(self):
        await self.close()
