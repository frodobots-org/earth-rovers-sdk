import asyncio
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
    def __init__(self):
        self.browser = None
        self.page = None
        self.default_viewport = {"width": 3840, "height": 2160}
        # Serialize concurrent init calls. Without this, when 3+ async loops
        # (telemetry / perception / control) all call initialize_browser at
        # the same time, we spawn 3+ Chrome instances, only one gets stored,
        # and the orphans emit page-context-destroyed errors forever.
        # Lazy-init the lock on first acquire so it binds to whichever
        # asyncio loop is actually running (hypercorn's), not the loop that
        # happens to be current at module-import time. Python 3.9's
        # asyncio.Lock captures the current loop at construction and
        # raises "Future attached to a different loop" if that loop
        # doesn't match. Same pattern is used for _send_lock below.
        self._init_lock = None

    async def initialize_browser(self):
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        async with self._init_lock:
            if self.browser is not None:
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
                        "--disable-application-cache",
                        "--disk-cache-size=0",
                        f"--window-size={self.default_viewport['width']},{self.default_viewport['height']}",
                    ],
                )
                self.page = await self.browser.newPage()
                # Bubble browser console messages up to the Python log so we
                # can see when RTM actually sends / fails. Filters to relevant
                # messages only to avoid log spam from unrelated warnings.
                def _on_console(msg):
                    try:
                        text = msg.text or ""
                        low = text.lower()
                        if any(k in low for k in [
                            "sending message to bot",
                            "message sent successfully",
                            "error sending message",
                            "rtminvalidstatus",
                            "rtm channel join",
                            "agorartm",
                        ]):
                            print(f"[browser {msg.type}] {text[:200]}")
                    except Exception:
                        pass
                self.page.on("console", _on_console)
                await self.page.setViewport(self.default_viewport)
                await self.page.setExtraHTTPHeaders(
                    {"Accept-Language": "en-US,en;q=0.9"}
                )
                await self.page.goto(
                    "http://127.0.0.1:8000/sdk", {"waitUntil": "networkidle2"}
                )

                # 1. Wait for #join button to be in the DOM before clicking it.
                #    Previously we called click() immediately, which raced with
                #    JS-based rendering and failed with "No node found".
                try:
                    await self.page.waitForSelector("#join", {"timeout": 10000})
                    await self.page.click("#join")
                except Exception as click_exc:
                    # If #join is missing or click fails, log and continue.
                    # RTM auto-joins on page load, so control commands still
                    # work even without the RTC video subscription.
                    print(f"Warning: #join click failed ({click_exc}); "
                          f"continuing without video RTC — commands via RTM only")

                # 2. Wait for video element (soft — may not appear if #join failed).
                try:
                    await self.page.waitForSelector("video", {"timeout": 8000})
                except Exception:
                    print("Warning: no video element attached (perception will fail)")
                try:
                    await self.page.waitForSelector("#map", {"timeout": 5000})
                except Exception:
                    pass
                await self.page.setViewport(self.default_viewport)

                await self.page.waitFor(2000)

                # 3. Wait for RTM to be actually usable (window.sendMessage +
                #    the ensureRtmReady helper the new JS ships with).
                try:
                    await self.page.waitForFunction(
                        "typeof window.sendMessage === 'function'",
                        timeout=15000,
                    )
                except Exception as ready_exc:
                    print(f"Warning: window.sendMessage not defined after 15s: {ready_exc}")

                # 4. initializeImageParams sets window.imageParams which
                #    captureFrameAsBase64 needs. Skipping this makes every
                #    frame capture throw. If the function doesn't exist yet,
                #    swallow the error — perception will just skip frames.
                call = f"""() => {{
                    if (typeof window.initializeImageParams === 'function') {{
                        window.initializeImageParams({{
                            imageFormat: "{FORMAT}",
                            imageQuality: {QUALITY}
                        }});
                    }} else {{
                        // Fallback: set imageParams directly so captureFrameAsBase64
                        // doesn't crash on reading .imageFormat.
                        window.imageParams = {{
                            imageFormat: "{FORMAT}",
                            imageQuality: {QUALITY}
                        }};
                    }}
                }}"""
                try:
                    await self.page.evaluate(call)
                except Exception as init_exc:
                    print(f"Warning: initializeImageParams evaluate failed: {init_exc}")
            except Exception as e:
                print(f"Error initializing browser: {e}")
                self.browser = None
                self.page = None
                await self.close_browser()
                raise

    async def take_screenshot(self, video_output_folder: str, elements: list):
        await self.initialize_browser()

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

        bot_data = await self.page.evaluate(
            """() => {
        return window.rtm_data;
        }"""
        )

        return bot_data

    async def front(self) -> str:
        await self.initialize_browser()

        front_frame = await self.page.evaluate(
            """() => {
        return getLastBase64Frame(1000) || null;
        }"""
        )

        return front_frame

    async def rear(self) -> str:
        await self.initialize_browser()

        rear_frame = await self.page.evaluate(
            """() => {
        return getLastBase64Frame(1001) || null;
        }"""
        )

        return rear_frame

    _send_lock = None

    async def send_message(self, message: dict, retries: int = 3):
        """Send a control command over RTM.

        Ported from feature/openClaw: awaits the JS promise from
        window.sendMessage (which now checks RTM readiness + retries on
        disconnect), serializes concurrent sends behind a lock, and retries
        the whole path up to `retries` times.

        Fire-and-forget sends (as the previous version did) do NOT survive
        RTM channel disconnects — the send appears to succeed on the
        Python side but never actually leaves the browser.
        """
        await self.initialize_browser()

        if self._send_lock is None:
            self._send_lock = asyncio.Lock()

        async with self._send_lock:
            last_error: Exception | None = None
            for attempt in range(max(1, int(retries))):
                try:
                    # The awaited JS sendMessage triggers ensureRtmReady()
                    # inside the browser (auto-reconnects on ABORTED /
                    # DISCONNECTED), then awaits sendMessageToPeer's promise
                    # so we know when the RTM actually accepted the message.
                    result = await self.page.evaluate(
                        """async (message) => {
                            return await window.sendMessage(message);
                        }""",
                        message,
                    )
                    return result
                except Exception as exc:
                    last_error = exc
                    if attempt < retries - 1:
                        await asyncio.sleep(0.3)
                        continue
                    raise last_error

    async def speak(self, audio_url: str):
        await self.initialize_browser()

        result = await self.page.evaluate(
            """async (audioUrl) => {
                return await window.playAudioToRover(audioUrl);
            }""",
            audio_url,
        )

        return result

    async def close_browser(self):
        if self.browser:
            await self.browser.close()
            self.browser = None
            self.page = None
