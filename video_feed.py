import asyncio
import base64
import logging
import time
from typing import Callable, Optional

import cv2
import numpy as np

logger = logging.getLogger("video_feed")

JPEG_QUALITY = 85


def data_url_to_jpeg(data_url: str) -> Optional[bytes]:
    """Decode a canvas data URL into JPEG bytes, transcoding if needed."""
    try:
        header, payload = data_url.split(",", 1)
    except (ValueError, AttributeError):
        return None
    raw = base64.b64decode(payload)
    if "image/jpeg" in header:
        return raw
    # IMAGE_FORMAT may be png/webp; MJPEG consumers (cv2, browsers) need JPEG.
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return encoded.tobytes() if ok else None


class FrameBroadcaster:
    """Shared capture loop for one camera view.

    One browser round-trip per frame no matter how many /feed clients are
    connected; each client gets a latest-wins queue so slow consumers never
    delay fresh frames (stale frames are dropped, which is what robotics
    consumers want).
    """

    def __init__(self, capture_fn: Callable, default_fps: int = 15):
        self._capture_fn = capture_fn  # async () -> Optional[str] (data URL)
        self._default_fps = default_fps
        self._clients: dict = {}  # asyncio.Queue -> requested fps
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None

    async def subscribe(self, fps: int) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        async with self._lock:
            self._clients[queue] = fps
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._capture_loop())
        return queue

    async def unsubscribe(self, queue: asyncio.Queue):
        async with self._lock:
            self._clients.pop(queue, None)
            if not self._clients and self._task:
                self._task.cancel()
                self._task = None

    def _capture_fps(self) -> int:
        return max(self._clients.values(), default=self._default_fps)

    async def _capture_loop(self):
        failures = 0
        while True:
            started = time.monotonic()
            jpeg = None
            try:
                data_url = await self._capture_fn()
                if data_url:
                    jpeg = data_url_to_jpeg(data_url)
                failures = 0
            except Exception as e:
                failures += 1
                if failures in (1, 5) or failures % 30 == 0:
                    logger.warning("Feed capture failing (%s): %s", failures, e)

            if jpeg:
                for queue in list(self._clients):
                    if queue.full():
                        try:
                            queue.get_nowait()  # drop the stale frame
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        queue.put_nowait(jpeg)
                    except asyncio.QueueFull:
                        pass

            if failures:
                # Rover/browser not ready; don't hammer relaunch attempts.
                await asyncio.sleep(min(2.0 * failures, 10.0))
            else:
                interval = 1.0 / self._capture_fps()
                elapsed = time.monotonic() - started
                await asyncio.sleep(max(0.0, interval - elapsed))
