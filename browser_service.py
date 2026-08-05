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
FORMAT = os.getenv("IMAGE_FORMAT", "jpeg")
QUALITY = float(os.getenv("IMAGE_QUALITY", "0.8"))
FEED_QUALITY = float(os.getenv("FEED_JPEG_QUALITY", "0.8"))

if FORMAT not in ["png", "jpeg", "webp"]:
    raise ValueError("Invalid image format. Supported formats: png, jpeg, webp")

if QUALITY < 0 or QUALITY > 1:
    raise ValueError("Invalid image quality. Quality should be between 0 and 1")

if FEED_QUALITY < 0 or FEED_QUALITY > 1:
    raise ValueError("Invalid feed quality. Quality should be between 0 and 1")

SDK_PAGE_URL = os.getenv("SDK_PAGE_URL", "http://127.0.0.1:8000/sdk")


class BrowserService:
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._ready = False
        self._lock = asyncio.Lock()
        self.last_error = None
        # Large enough for the legacy front/rear/map element captures without
        # paying for an 8.3-megapixel headless render surface on every frame.
        self._viewport = {"width": 1920, "height": 1200}

    @property
    def is_ready(self) -> bool:
        return bool(
            self._ready
            and self._page
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
        self._ready = False
        try:
            if self._playwright is None:
                self._playwright = await async_playwright().start()

            launch_kwargs = {
                "headless": True,
                "args": [
                    "--ignore-certificate-errors",
                    "--no-sandbox",
                    "--autoplay-policy=no-user-gesture-required",
                    "--use-fake-ui-for-media-stream",
                    "--disable-application-cache",
                    "--disk-cache-size=0",
                ],
            }
            executable_path = os.getenv("CHROME_EXECUTABLE_PATH") or None
            if executable_path:
                self._browser = await self._playwright.chromium.launch(
                    executable_path=executable_path, **launch_kwargs
                )
                logger.info("Using browser from CHROME_EXECUTABLE_PATH")
            else:
                # Prefer installed Google Chrome: it ships the H.264/AAC
                # codecs some rover streams need; Playwright's open-source
                # Chromium does not and decodes those streams as 0x0.
                try:
                    self._browser = await self._playwright.chromium.launch(
                        channel="chrome", **launch_kwargs
                    )
                    logger.info("Using installed Google Chrome (all codecs)")
                except PlaywrightError:
                    if not os.path.exists(
                        self._playwright.chromium.executable_path
                    ):
                        raise RuntimeError(
                            "No usable browser found. Run: python -m"
                            " playwright install chromium (or install Google"
                            " Chrome, or set CHROME_EXECUTABLE_PATH)"
                        )
                    self._browser = await self._playwright.chromium.launch(
                        **launch_kwargs
                    )
                    logger.info(
                        "Using Playwright's bundled Chromium (no Google"
                        " Chrome found). If video frames stay empty, the"
                        " stream may need H.264: install Chrome or set"
                        " CHROME_EXECUTABLE_PATH to a codec-capable browser"
                    )
            self._context = await self._browser.new_context(
                viewport=self._viewport,
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            self._page = await self._context.new_page()
            await self._page.goto(SDK_PAGE_URL, wait_until="domcontentloaded")
            await self._page.click("#join")
            # Control and telemetry must remain available when a camera is
            # offline. Wait for RTM readiness, not for a video DOM element.
            await self._page.wait_for_function(
                "() => typeof window.sendMessage === 'function'"
                " && window.rtmReady === true",
                timeout=15000,
            )

            call = f"""() => {{
                window.initializeImageParams({{
                    imageFormat: "{FORMAT}",
                    imageQuality: {QUALITY}
                }});
            }}"""
            await self._page.evaluate(call)
            self._ready = True
            self.last_error = None
            logger.info("Headless browser connected to %s", SDK_PAGE_URL)
        except Exception as e:
            self.last_error = str(e).split("\n", 1)[0]
            logger.error("Error initializing browser: %s", e)
            await self._teardown()
            # A failed Playwright transport cannot recover by reusing the same
            # driver instance. Recreate it on the next warm-up attempt.
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            raise

    async def _teardown(self):
        self._ready = False
        for target in (self._page, self._context, self._browser):
            if target:
                try:
                    await target.close()
                except Exception:
                    pass
        self._page = None
        self._context = None
        self._browser = None

    async def _invalidate(self, failed_page):
        """Tear down only if the failed page is still the active generation."""
        async with self._lock:
            if self._page is failed_page:
                await self._teardown()

    async def _run(self, fn, *, retry_on_disconnect: bool = True):
        page = await self.ensure_page()
        try:
            return await fn(page)
        except PlaywrightError as e:
            disconnected = page.is_closed() or not (
                self._browser and self._browser.is_connected()
            )
            if not disconnected:
                # JavaScript/application errors are not browser crashes. Do not
                # disrupt feed/control calls that are sharing a healthy page.
                raise
            logger.warning("Browser disconnected (%s); relaunching", e)
            await self._invalidate(page)
            if not retry_on_disconnect:
                raise
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

    async def capture_screenshots(self, elements: list) -> dict[str, bytes]:
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
                start_time = time.time()
                image = await locator.screenshot(type="png", timeout=5000)
                elapsed_ms = (time.time() - start_time) * 1000
                logger.info("Screenshot for %s took %.2f ms", name, elapsed_ms)
                screenshots[name] = image
            return screenshots

        return await self._run(capture)

    async def take_screenshot(self, video_output_folder: str, elements: list):
        """Backward-compatible wrapper; new API consumers should use bytes."""
        screenshots = await self.capture_screenshots(elements)
        paths = {}
        for name, image in screenshots.items():
            output_path = os.path.join(video_output_folder, f"{name}.png")
            await asyncio.to_thread(self._write_file, output_path, image)
            paths[name] = output_path
        return paths

    @staticmethod
    def _write_file(path: str, content: bytes):
        with open(path, "wb") as output:
            output.write(content)

    async def data(self) -> dict:
        return await self._run(lambda page: page.evaluate("() => window.rtm_data"))

    async def has_rear_camera(self) -> bool:
        # Capability comes from reality, not BOT_TYPE: the rover either
        # publishes a rear video track (uid 1001) or it doesn't.
        result = await self._run(
            lambda page: page.evaluate(
                "() => !!(typeof remoteUsers !== 'undefined'"
                " && remoteUsers[1001] && remoteUsers[1001].videoTrack)"
            )
        )
        return bool(result)

    async def front(self) -> str:
        return await self._run(
            lambda page: page.evaluate("() => getLastBase64Frame(1000) || null")
        )

    async def rear(self) -> str:
        return await self._run(
            lambda page: page.evaluate("() => getLastBase64Frame(1001) || null")
        )

    async def front_feed(self) -> dict:
        return await self._run(
            lambda page: page.evaluate(
                "([quality]) => getFramePacket(1000, 'jpeg', quality)",
                [FEED_QUALITY],
            )
        )

    async def rear_feed(self) -> dict:
        return await self._run(
            lambda page: page.evaluate(
                "([quality]) => getFramePacket(1001, 'jpeg', quality)",
                [FEED_QUALITY],
            )
        )

    async def configured_frame(self, view: str) -> dict:
        uid = 1000 if view == "front" else 1001
        return await self._run(
            lambda page: page.evaluate(
                "([uid, format, quality]) => getFramePacket(uid, format, quality)",
                [uid, FORMAT, QUALITY],
            )
        )

    async def send_message(self, message: dict):
        return await self._run(
            lambda page: page.evaluate(
                "(message) => window.sendMessage(message)", message
            ),
            retry_on_disconnect=False,
        )

    async def speak(self, audio_url: str):
        return await self._run(
            lambda page: page.evaluate(
                "async (audioUrl) => await window.playAudioToRover(audioUrl)",
                audio_url,
            ),
            retry_on_disconnect=False,
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
