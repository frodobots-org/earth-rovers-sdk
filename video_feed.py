import asyncio
import base64
import binascii
import contextlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Union

import cv2
import numpy as np

logger = logging.getLogger("video_feed")

JPEG_QUALITY = 85

# How long the capture loop keeps running after the last subscriber leaves.
# Snapshot pollers (ROS querying /v2/* at 10 Hz) subscribe for a single frame
# at a time; without a linger every poll would pay loop startup — and any
# transient capture failure would run its recovery backoff inside the
# request — instead of hitting a warm latest-frame cache.
IDLE_LINGER_S = float(os.getenv("FEED_IDLE_LINGER_S", "10"))


@dataclass(frozen=True)
class Frame:
    """One encoded camera frame and its actual capture time."""

    data_url: str
    jpeg: bytes
    captured_at: float
    captured_monotonic: float

    @property
    def base64_data(self) -> str:
        return self.data_url.split(",", 1)[1]


CaptureResult = Union[str, dict, None]


def data_url_to_jpeg(data_url: str) -> Optional[bytes]:
    """Decode a canvas data URL into JPEG bytes, transcoding if needed."""
    try:
        header, payload = data_url.split(",", 1)
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, AttributeError, binascii.Error):
        return None
    if "image/jpeg" in header:
        return raw
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return encoded.tobytes() if ok else None


class FrameBroadcaster:
    """Latest-frame cache and shared capture loop for one camera.

    Feed clients and snapshot callers use the same capture. Each subscriber has
    a one-item queue, so slow consumers can never build a stale frame backlog.
    """

    def __init__(self, capture_fn: Callable[[], Awaitable[CaptureResult]]):
        self._capture_fn = capture_fn
        self._clients: dict[asyncio.Queue, int] = {}
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop = None
        self._task: Optional[asyncio.Task] = None
        self._latest: Optional[Frame] = None
        self._demand_until = 0.0
        self._linger_fps = 0
        self.last_error: Optional[str] = None
        self.captures_total = 0
        self.failures_total = 0

    def _get_lock(self) -> asyncio.Lock:
        # Broadcasters are constructed at import time, before the serving
        # event loop exists. On Python 3.9 a Lock binds to the construction-
        # time loop, so create it lazily inside the running loop instead.
        running_loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not running_loop:
            self._lock = asyncio.Lock()
            self._lock_loop = running_loop
        return self._lock

    @property
    def latest(self) -> Optional[Frame]:
        return self._latest

    def latest_if_fresh(self, max_age: float) -> Optional[Frame]:
        frame = self._latest
        if frame and time.monotonic() - frame.captured_monotonic <= max_age:
            return frame
        return None

    @property
    def loop_running(self) -> bool:
        return bool(self._task and not self._task.done())

    @property
    def latest_age_seconds(self) -> Optional[float]:
        frame = self._latest
        if frame is None:
            return None
        return time.monotonic() - frame.captured_monotonic

    def _note_demand(self, fps: int):
        self._demand_until = time.monotonic() + IDLE_LINGER_S
        self._linger_fps = max(self._linger_fps, fps)

    async def subscribe(
        self, fps: int, *, cached_max_age: float = 1.0
    ) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        async with self._get_lock():
            self._note_demand(fps)
            self._clients[queue] = fps
            cached = self.latest_if_fresh(max_age=cached_max_age)
            if cached:
                queue.put_nowait(cached)
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._capture_loop())
        return queue

    async def unsubscribe(self, queue: asyncio.Queue):
        task = None
        async with self._get_lock():
            self._clients.pop(queue, None)
            # Within the linger window the loop stays warm for the next
            # subscriber or poller; it shuts itself down once demand expires.
            if (
                not self._clients
                and self._task
                and time.monotonic() >= self._demand_until
            ):
                task = self._task
                self._task = None
                task.cancel()
        if task:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def get_frame(
        self, *, max_age: float = 0.1, timeout: float = 5.0, fps: int = 30
    ) -> Optional[Frame]:
        self._note_demand(fps)
        cached = self.latest_if_fresh(max_age)
        if cached:
            return cached
        queue = await self.subscribe(fps, cached_max_age=max_age)
        try:
            return await asyncio.wait_for(queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            await self.unsubscribe(queue)

    async def close(self):
        task = None
        async with self._get_lock():
            clients = list(self._clients)
            self._clients.clear()
            self._latest = None
            self._demand_until = 0.0
            self._linger_fps = 0
            if self._task:
                task = self._task
                self._task = None
                task.cancel()
        # Wake every waiting stream client with an end-of-stream sentinel;
        # otherwise generators blocked on queue.get() would hang forever.
        for queue in clients:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)
        if task:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _capture_fps(self) -> int:
        # A snapshot poller subscribes only for one frame at a time, so its
        # rate lives in _linger_fps rather than _clients: keep capturing at
        # the demanded cadence for the whole linger window.
        linger = self._linger_fps if time.monotonic() < self._demand_until else 0
        return max(max(self._clients.values(), default=1), linger)

    async def _build_frame(self, result: CaptureResult) -> Optional[Frame]:
        if isinstance(result, dict):
            if result.get("error"):
                # The page diagnosed the failure (e.g. missing H.264 codec) —
                # surface its message instead of a generic "not available".
                raise RuntimeError(result["error"])
            data_url = result.get("data_url")
            captured_at = float(result.get("timestamp") or time.time())
        else:
            data_url = result
            captured_at = time.time()
        if not data_url:
            return None

        # The dedicated feed capture is JPEG, making this just a cheap base64
        # decode. Keep fallback transcodes off the event loop for compatibility.
        if data_url.startswith("data:image/jpeg"):
            jpeg = data_url_to_jpeg(data_url)
        else:
            jpeg = await asyncio.to_thread(data_url_to_jpeg, data_url)
        if not jpeg:
            return None
        return Frame(data_url, jpeg, captured_at, time.monotonic())

    async def _capture_loop(self):
        failures = 0
        while True:
            async with self._get_lock():
                # Self-shutdown once nothing has wanted frames for a while.
                # Done under the same lock subscribe() uses to restart the
                # task, so a subscriber can never race a dying loop.
                if not self._clients and time.monotonic() >= self._demand_until:
                    if self._task is asyncio.current_task():
                        self._task = None
                        self._linger_fps = 0
                    return
            started = time.monotonic()
            try:
                frame = await self._build_frame(await self._capture_fn())
                if frame is None:
                    raise RuntimeError("camera frame is not available")
                failures = 0
                self.last_error = None
                self.captures_total += 1
                self._latest = frame
                for queue in list(self._clients):
                    if queue.full():
                        with contextlib.suppress(asyncio.QueueEmpty):
                            queue.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        queue.put_nowait(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                self.failures_total += 1
                self.last_error = str(exc).split("\n", 1)[0]
                if failures in (1, 5) or failures % 30 == 0:
                    logger.warning("Feed capture failing (%s): %s", failures, exc)

            if failures:
                await asyncio.sleep(min(0.25 * (2 ** min(failures - 1, 3)), 2.0))
            else:
                interval = 1.0 / self._capture_fps()
                await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))
