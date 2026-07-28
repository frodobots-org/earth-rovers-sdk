import asyncio
import logging
import os
import subprocess
import time
from pyppeteer import launch
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("browser_service")

# Configuration from environment variables with defaults
FORMAT = os.getenv("IMAGE_FORMAT", "png")
QUALITY = float(os.getenv("IMAGE_QUALITY", "1.0"))
HAS_REAR_CAMERA = os.getenv("HAS_REAR_CAMERA", "False").lower() == "true"
# Telemetry older than this means the Agora RTM session is likely stale.
RTM_STALE_SECONDS = float(os.getenv("RTM_STALE_SECONDS", "8"))
# Avoid reinit storms when the bot is offline.
RTM_REINIT_COOLDOWN_SECONDS = float(os.getenv("RTM_REINIT_COOLDOWN_SECONDS", "30"))

if FORMAT not in ["png", "jpeg", "webp"]:
    raise ValueError("Invalid image format. Supported formats: png, jpeg, webp")

if QUALITY < 0 or QUALITY > 1:
    raise ValueError("Invalid image quality. Quality should be between 0 and 1")


def _chrome_profile_dir() -> str:
    return os.path.join(os.path.dirname(__file__), ".chrome-profile")


def _force_cleanup_chrome_profile():
    """
    Kill orphaned headless Chrome using this profile and clear Singleton locks.

    hypercorn --reload / failed reinits often leave Chrome holding the profile,
    which makes the next pyppeteer launch fail with:
    BrowserError: Browser closed unexpectedly
    """
    profile_dir = _chrome_profile_dir()
    try:
        # Match only Chrome processes using this project's profile path.
        subprocess.run(
            ["pkill", "-f", f"user-data-dir={profile_dir}"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        logger.warning("Failed to pkill chrome profile processes: %s", e)

    # Brief wait so Chrome can release file locks.
    time.sleep(0.5)

    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = os.path.join(profile_dir, name)
        try:
            if os.path.lexists(path):
                os.unlink(path)
        except OSError as e:
            logger.warning("Failed to remove %s: %s", path, e)


class BrowserService:
    def __init__(self):
        self.browser = None
        self.page = None
        self.default_viewport = {"width": 3840, "height": 2160}
        self._initialized_at = None
        self._last_reinit_at = 0.0
        self._reinit_lock = asyncio.Lock()

    async def initialize_browser(self):
        async with self._reinit_lock:
            if self.browser:
                return
            await self._initialize_browser_unlocked()

    async def _initialize_browser_unlocked(self):
        try:
            executable_path = os.getenv(
                "CHROME_EXECUTABLE_PATH",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            )
            # Ensure no orphaned Chrome is holding the profile lock.
            await asyncio.get_event_loop().run_in_executor(
                None, _force_cleanup_chrome_profile
            )
            self.browser = await launch(
                executablePath=executable_path,
                headless=True,
                userDataDir=_chrome_profile_dir(),
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
            await self.page.setViewport(self.default_viewport)
            await self.page.setExtraHTTPHeaders(
                {"Accept-Language": "en-US,en;q=0.9"}
            )
            await self.page.goto(
                "http://127.0.0.1:8000/sdk", {"waitUntil": "networkidle2"}
            )

            # Fail fast if auth tokens were not injected into /sdk.
            page_auth = await self.page.evaluate(
                """() => ({
                    appid: Boolean(document.getElementById('appid')?.value),
                    channel: Boolean(document.getElementById('channel')?.value),
                    uid: Boolean(document.getElementById('uid')?.value),
                    token: Boolean(document.getElementById('token')?.value),
                    rtm_token: Boolean(document.getElementById('rtm_token')?.value),
                    bot_uid: Boolean(document.getElementById('bot_uid')?.value),
                })"""
            )
            missing = [k for k, ok in (page_auth or {}).items() if not ok]
            if missing:
                raise RuntimeError(
                    "SDK page missing auth fields "
                    f"(call /start-mission first): {', '.join(missing)}"
                )

            await self.page.click("#join")
            await self.page.waitForSelector("#map", {"timeout": 30000})

            # Video is created only when the bot publishes an RTC track.
            # Personal/emu bots (or slow joins) may not show <video> quickly.
            # Control only needs RTM CONNECTED, so accept either signal.
            try:
                await self.page.waitForFunction(
                    """() => {
                        const rtmReady = window.rtmConnectionState === 'CONNECTED';
                        const hasVideo = Boolean(document.querySelector('video'));
                        const hasTelemetry = Boolean(window.rtm_data);
                        return rtmReady || hasVideo || hasTelemetry;
                    }""",
                    {"timeout": 60000},
                )
            except Exception as wait_err:
                diagnostics = await self.page.evaluate(
                    """() => ({
                        rtmState: window.rtmConnectionState || null,
                        hasVideo: Boolean(document.querySelector('video')),
                        hasTelemetry: Boolean(window.rtm_data),
                        channel: document.getElementById('channel')?.value || null,
                        botUid: document.getElementById('bot_uid')?.value || null,
                    })"""
                )
                raise TimeoutError(
                    "Timed out waiting for Agora RTM/video after Join. "
                    f"diagnostics={diagnostics}"
                ) from wait_err

            await self.page.setViewport(self.default_viewport)
            await self.page.waitFor(2000)

            call = f"""() => {{
                window.initializeImageParams({{
                    imageFormat: "{FORMAT}",
                    imageQuality: {QUALITY}
                }});
            }}"""
            await self.page.evaluate(call)
            self._initialized_at = time.time()
        except Exception as e:
            print(f"Error initializing browser: {e}")
            try:
                await self._close_browser_unlocked()
            except Exception as close_err:
                print(f"Error closing browser after failed init: {close_err}")
                self.browser = None
                self.page = None
                self._initialized_at = None
            raise

    async def close_browser(self):
        async with self._reinit_lock:
            await self._close_browser_unlocked()

    async def _close_browser_unlocked(self):
        if self.browser:
            try:
                await self.browser.close()
            except Exception as close_err:
                print(f"Error closing browser: {close_err}")
            self.browser = None
            self.page = None
            self._initialized_at = None
        # Always clear orphans/locks; browser.close() is not reliable on reload.
        await asyncio.get_event_loop().run_in_executor(
            None, _force_cleanup_chrome_profile
        )

    async def reinitialize_browser(self):
        """Tear down headless Chrome and join Agora again."""
        async with self._reinit_lock:
            now = time.time()
            if now - self._last_reinit_at < RTM_REINIT_COOLDOWN_SECONDS and self.browser:
                logger.info(
                    "Skipping RTM reinit; cooldown %.0fs remaining",
                    RTM_REINIT_COOLDOWN_SECONDS - (now - self._last_reinit_at),
                )
                return False

            logger.warning("Reinitializing browser/RTM session")
            self._last_reinit_at = now
            await self._close_browser_unlocked()
            await self._initialize_browser_unlocked()
            return True

    async def _session_health(self) -> dict:
        if not self.page:
            return {"healthy": False, "reason": "no_page"}

        try:
            info = await self.page.evaluate(
                """() => {
                    if (typeof window.getRtmSessionHealth === 'function') {
                        return window.getRtmSessionHealth();
                    }
                    const ts = window.rtm_data && window.rtm_data.timestamp;
                    return {
                        state: window.rtmConnectionState || null,
                        reason: window.rtmConnectionReason || null,
                        timestamp: ts == null ? null : String(ts),
                        hasTelemetry: Boolean(window.rtm_data),
                    };
                }"""
            )
        except Exception as e:
            return {"healthy": False, "reason": f"evaluate_failed:{e}"}

        state = (info or {}).get("state")
        if state in ("DISCONNECTED", "ABORTED"):
            return {
                "healthy": False,
                "reason": f"rtm_state:{state}",
                "details": info,
            }

        ts = (info or {}).get("timestamp")
        if ts is not None:
            try:
                age = time.time() - float(ts)
            except (TypeError, ValueError):
                age = None
            if age is not None and age > RTM_STALE_SECONDS:
                # Give a short grace period right after join before demanding telemetry.
                if (
                    self._initialized_at
                    and time.time() - self._initialized_at > RTM_STALE_SECONDS
                ):
                    return {
                        "healthy": False,
                        "reason": f"stale_telemetry:{age:.1f}s",
                        "details": info,
                    }

        return {"healthy": True, "details": info}

    async def ensure_healthy(self, force_check: bool = True):
        """Ensure browser exists and RTM session looks alive; reinit if needed."""
        await self.initialize_browser()
        if not force_check:
            return

        health = await self._session_health()
        if health.get("healthy"):
            return

        logger.warning("Unhealthy RTM session detected: %s", health.get("reason"))
        reinit_done = await self.reinitialize_browser()
        if not reinit_done:
            # Cooldown blocked a full reinit; still try using the current page.
            return

        health = await self._session_health()
        if not health.get("healthy"):
            logger.warning(
                "Session still unhealthy after reinit: %s", health.get("reason")
            )

    async def take_screenshot(self, video_output_folder: str, elements: list):
        await self.ensure_healthy()

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
        await self.ensure_healthy()

        bot_data = await self.page.evaluate(
            """() => {
        return window.rtm_data;
        }"""
        )

        return bot_data

    async def front(self) -> str:
        await self.ensure_healthy()

        front_frame = await self.page.evaluate(
            """() => {
        return getLastBase64Frame(1000) || null;
        }"""
        )

        return front_frame

    async def rear(self) -> str:
        await self.ensure_healthy()

        rear_frame = await self.page.evaluate(
            """() => {
        return getLastBase64Frame(1001) || null;
        }"""
        )

        return rear_frame

    async def _send_message_once(self, message: dict) -> dict:
        return await self.page.evaluate(
            """async (message) => {
                try {
                    if (typeof window.sendMessage !== 'function') {
                        return { ok: false, error: 'sendMessage is not available' };
                    }
                    await window.sendMessage(message);
                    return { ok: true };
                } catch (e) {
                    return {
                        ok: false,
                        error: String((e && (e.message || e.code)) || e),
                        state: window.rtmConnectionState || null,
                    };
                }
            }""",
            message,
        )

    async def send_message(self, message: dict):
        await self.ensure_healthy()

        result = await self._send_message_once(message)
        if result and result.get("ok"):
            return result

        logger.warning(
            "RTM send failed (%s); attempting browser reinit",
            (result or {}).get("error"),
        )
        await self.reinitialize_browser()
        result = await self._send_message_once(message)
        if result and result.get("ok"):
            return result

        error = (result or {}).get("error") or "RTM send failed"
        raise RuntimeError(error)

    async def speak(self, audio_url: str):
        await self.ensure_healthy()

        result = await self.page.evaluate(
            """async (audioUrl) => {
                return await window.playAudioToRover(audioUrl);
            }""",
            audio_url,
        )

        return result
