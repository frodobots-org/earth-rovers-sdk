import logging
import time
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger("telemetry_hub")


class TelemetryHub:
    """In-memory cache of the latest rover telemetry with WebSocket fan-out.

    The /sdk page (headless browser) pushes every RTM message here via
    /ws/ingest; dashboard clients subscribe via /ws/data.
    """

    def __init__(self):
        self.latest: Optional[dict] = None
        self.last_update: Optional[float] = None  # time.monotonic()
        self.ingest_connected = False
        self._clients: set = set()

    @property
    def age_seconds(self) -> Optional[float]:
        if self.last_update is None:
            return None
        return time.monotonic() - self.last_update

    def add_client(self, websocket: WebSocket):
        self._clients.add(websocket)

    def remove_client(self, websocket: WebSocket):
        self._clients.discard(websocket)

    async def publish(self, data: dict):
        self.latest = data
        self.last_update = time.monotonic()
        await self.broadcast({"type": "telemetry", "data": data})

    async def broadcast(self, message: dict):
        dead = []
        for websocket in list(self._clients):
            try:
                await websocket.send_json(message)
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            self._clients.discard(websocket)

    def status(self) -> dict:
        return {
            "ingest_connected": self.ingest_connected,
            "telemetry_age_s": self.age_seconds,
        }

    def snapshot(self) -> dict:
        return {"type": "snapshot", "data": self.latest, **self.status()}
