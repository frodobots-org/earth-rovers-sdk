import os
import time
from pyppeteer import launch
from dotenv import load_dotenv

load_dotenv()

# Configuration from environment variables with defaults
FORMAT = os.getenv("IMAGE_FORMAT", "png")
QUALITY = float(os.getenv("IMAGE_QUALITY", "1.0"))
HAS_REAR_CAMERA = os.getenv("HAS_REAR_CAMERA", "False").lower() == "true"

if FORMAT not in ["png", "jpeg", "webp"]:
    raise ValueError("Invalid image format. Supported formats: png, jpeg, webp")

if QUALITY < 0 or QUALITY > 1:
    raise ValueError("Invalid image quality. Quality should be between 0 and 1")


class BrowserService:
    def __init__(self, page_path: str = "/sdk", require_rtm: bool = True):
        self.browser = None
        self.page = None
        self.default_viewport = {"width": 3840, "height": 2160}
        self.send_lock = None
        self.init_lock = None
        self.page_path = page_path
        self.require_rtm = require_rtm

    async def initialize_browser(self):
        if self.browser:
            return

        if self.init_lock is None:
            import asyncio

            self.init_lock = asyncio.Lock()

        async with self.init_lock:
            if self.browser:
                return

            try:
                executable_path = os.getenv(
                    "CHROME_EXECUTABLE_PATH",
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                )
                self.browser = await launch(
                    executablePath=executable_path,
                    headless=True,
                    args=[
                        "--ignore-certificate-errors",
                        "--no-sandbox",
                        "--autoplay-policy=no-user-gesture-required",
                        "--use-fake-ui-for-media-stream",
                        "--enable-usermedia-screen-capturing",
                        "--allow-http-screen-capture",
                        f"--window-size={self.default_viewport['width']},{self.default_viewport['height']}",
                    ],
                )
                self.page = await self.browser.newPage()
                await self.page.setViewport(self.default_viewport)
                await self.page.setExtraHTTPHeaders(
                    {"Accept-Language": "en-US,en;q=0.9"}
                )
                await self.page.goto(
                    f"http://127.0.0.1:8000{self.page_path}",
                    {"waitUntil": "networkidle2"},
                )
                await self.page.waitForSelector("#map")

                # IMPORTANT: do not RTC-join in headless control path.
                # `/control` only needs RTM (window.sendMessage). Forcing RTC join here can
                # collide with a human browser session using the same UID and result in
                # Agora "UID_BANNED" reconnect blocks.
                if self.require_rtm:
                    await self.page.waitForFunction(
                        """() =>
                        Boolean(
                            window.sendMessage &&
                            window.ensureRtmReady
                        )"""
                    )
                else:
                    await self.page.click("#join")
                    await self.page.waitForSelector("#remote-playerlist")
                    await self.page.waitForFunction(
                        """() =>
                        Boolean(
                            window.recordRoverAudio &&
                            window.ensureRtcReady
                        )"""
                    )
                await self.page.setViewport(self.default_viewport)

                self.page.on("console", lambda msg: print(f"[browser] {msg.type}: {msg.text}"))

                await self.page.waitFor(2000)

                call = f"""() => {{
                    window.initializeImageParams({{
                        imageFormat: "{FORMAT}",
                        imageQuality: {QUALITY}
                    }});
                }}"""
                await self.page.evaluate(call)
            except Exception as e:
                print(f"Error initializing browser: {e}")
                self.browser = None
                self.page = None
                await self.close_browser()
                raise

    async def take_screenshot(self, video_output_folder: str, elements: list):
        await self.initialize_browser()
        await self.ensure_session_ready()

        dimensions = await self.page.evaluate(
            """() => {
            return {
                width: Math.max(document.documentElement.scrollWidth, window.innerWidth),
                height: Math.max(document.documentElement.scrollHeight, window.innerHeight),
            }
        }"""
        )

        if (
            dimensions["width"] > self.default_viewport["width"]
            or dimensions["height"] > self.default_viewport["height"]
        ):
            await self.page.setViewport(dimensions)

        element_map = {"front": "#player-1000", "rear": "#player-1001", "map": "#map"}

        screenshots = {}
        for name in elements:
            if name in element_map:
                element_id = element_map[name]
                output_path = f"{video_output_folder}/{name}.png"
                element = await self.page.querySelector(element_id)
                if element:
                    start_time = time.time()  # Start time
                    await element.screenshot({"path": output_path})
                    end_time = time.time()  # End time
                    elapsed_time = (
                        end_time - start_time
                    ) * 1000  # Convert to milliseconds
                    print(f"Screenshot for {name} took {elapsed_time:.2f} ms")
                    screenshots[name] = output_path
                else:
                    print(f"Element {element_id} not found")
            else:
                print(f"Invalid element name: {name}")

        return screenshots

    async def data(self) -> dict:
        await self.initialize_browser()
        await self.ensure_session_ready()

        bot_data = await self.page.evaluate(
            """() => {
        return window.rtm_data;
        }"""
        )

        return bot_data

    async def front(self) -> str:
        await self.initialize_browser()
        await self.ensure_session_ready(require_rtm=False)

        try:
            front_frame = await self.page.evaluate(
                """() => {
            return getLastBase64Frame(1000) || null;
            }"""
            )
        except Exception as error:
            print(f"front frame capture failed: {error}")
            return None

        return front_frame

    async def rear(self) -> str:
        await self.initialize_browser()
        await self.ensure_session_ready(require_rtm=False)

        try:
            rear_frame = await self.page.evaluate(
                """() => {
            return getLastBase64Frame(1001) || null;
            }"""
            )
        except Exception as error:
            print(f"rear frame capture failed: {error}")
            return None

        return rear_frame

    async def send_message(self, message: dict, retries: int = 3):
        await self.initialize_browser()
        await self.ensure_session_ready()
        if self.send_lock is None:
            import asyncio

            self.send_lock = asyncio.Lock()

        async with self.send_lock:
            for attempt in range(retries):
                try:
                    result = await self.page.evaluate(
                        """async (message) => {
                            return await window.sendMessage(message);
                        }""",
                        message,
                    )
                    return result
                except Exception as e:
                    print(f"send_message attempt {attempt + 1}/{retries} failed: {e}")
                    if attempt < retries - 1:
                        import asyncio

                        await asyncio.sleep(0.3)
                    else:
                        raise

    async def speak(self, audio_url: str):
        await self.initialize_browser()
        await self.ensure_session_ready()

        result = await self.page.evaluate(
            """async (audioUrl) => {
                return await window.playAudioToRover(audioUrl);
            }""",
            audio_url,
        )

        return result

    async def record_rover_audio(self, duration_ms: int = 4000):
        """Record audio from the rover's mic via Agora RTC. Returns base64 data URL or None."""
        await self.initialize_browser()
        await self.ensure_session_ready(require_rtm=False)
        result = await self.page.evaluate(
            """async (durationMs) => {
                return await window.recordRoverAudio(durationMs);
            }""",
            duration_ms,
        )
        return result

    async def ensure_session_ready(self, require_rtm: bool = None):
        if require_rtm is None:
            require_rtm = self.require_rtm
        last_error = None

        for attempt in range(2):
            await self.initialize_browser()
            try:
                status = await self.page.evaluate(
                    """async (requireRtm) => {
                        let rtcReady = null;
                        if (!requireRtm) {
                            rtcReady = window.ensureRtcReady
                                ? await window.ensureRtcReady()
                                : false;
                        }
                        let rtmReady = null;
                        if (requireRtm) {
                            rtmReady = window.ensureRtmReady
                                ? await window.ensureRtmReady()
                                : false;
                        }
                        return {
                            rtc_ready: rtcReady === null ? null : Boolean(rtcReady),
                            rtm_ready: rtmReady === null ? null : Boolean(rtmReady),
                            rtc_state: window.rtcConnectionState || null,
                            rtm_state: window.rtmConnectionState || null,
                        };
                    }""",
                    require_rtm,
                )
            except Exception as error:
                last_error = error
                print(f"ensure_session_ready evaluate failed: {error}")
                if attempt == 0:
                    await self.close_browser()
                    continue
                raise

            if (require_rtm and status.get("rtm_ready")) or (
                not require_rtm and status.get("rtc_ready")
            ):
                return

            rtc_state = status.get("rtc_state")
            rtm_state = status.get("rtm_state")
            last_error = RuntimeError(
                f"RTC session not ready (state={rtc_state}); RTM state={rtm_state}"
            )
            if attempt == 0:
                print(f"ensure_session_ready recovering after unhealthy session: {last_error}")
                await self.close_browser()
                continue

            if require_rtm:
                raise RuntimeError(f"RTM session not ready (state={rtm_state})")
            raise RuntimeError(f"RTC session not ready (state={rtc_state})")

        if last_error:
            raise last_error

    async def close_browser(self):
        if self.browser:
            await self.browser.close()
            self.browser = None
            self.page = None
