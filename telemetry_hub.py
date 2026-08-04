import asyncio
import contextlib
import time
from typing import Optional


class TelemetryHub:
    """Latest telemetry cache with non-blocking, latest-wins fan-out."""

    def __init__(self):
        self.latest: Optional[dict] = None
        self.last_update: Optional[float] = None
        self._ingest_connections: set[object] = set()
        self._clients: set[asyncio.Queue] = set()

    @property
    def ingest_connected(self) -> bool:
        return bool(self._ingest_connections)

    @property
    def age_seconds(self) -> Optional[float]:
        if self.last_update is None:
            return None
        return time.monotonic() - self.last_update

    def connect_ingest(self) -> object:
        connection = object()
        self._ingest_connections.add(connection)
        self.broadcast({"type": "status", **self.status()})
        return connection

    def disconnect_ingest(self, connection: object):
        self._ingest_connections.discard(connection)
        self.broadcast({"type": "status", **self.status()})

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._clients.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        self._clients.discard(queue)

    def publish(self, data: dict):
        self.latest = data
        self.last_update = time.monotonic()
        self.broadcast({"type": "telemetry", "data": data})

    def broadcast(self, message: dict):
        for queue in list(self._clients):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(message)

    def status(self) -> dict:
        return {
            "ingest_connected": self.ingest_connected,
            "ingest_connections": len(self._ingest_connections),
            "telemetry_age_s": self.age_seconds,
        }

    def snapshot(self) -> dict:
        return {"type": "snapshot", "data": self.latest, **self.status()}
