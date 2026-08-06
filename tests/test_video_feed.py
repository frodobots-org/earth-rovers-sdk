import asyncio
import base64
import time
import unittest

from video_feed import FrameBroadcaster


JPEG_DATA_URL = "data:image/jpeg;base64," + base64.b64encode(
    b"\xff\xd8test-frame\xff\xd9"
).decode("ascii")


class FrameBroadcasterTest(unittest.IsolatedAsyncioTestCase):
    async def test_clients_share_one_capture_and_receive_frame(self):
        calls = 0

        async def capture():
            nonlocal calls
            calls += 1
            return {"data_url": JPEG_DATA_URL, "timestamp": time.time()}

        broadcaster = FrameBroadcaster(capture)
        first = await broadcaster.subscribe(30, cached_max_age=0)
        second = await broadcaster.subscribe(15, cached_max_age=0)
        try:
            frame1, frame2 = await asyncio.gather(first.get(), second.get())
            self.assertEqual(frame1.jpeg, frame2.jpeg)
            self.assertEqual(calls, 1)
        finally:
            await broadcaster.unsubscribe(first)
            await broadcaster.unsubscribe(second)

    async def test_snapshot_reuses_fresh_cached_frame(self):
        calls = 0

        async def capture():
            nonlocal calls
            calls += 1
            return {"data_url": JPEG_DATA_URL, "timestamp": time.time()}

        broadcaster = FrameBroadcaster(capture)
        first = await broadcaster.get_frame(max_age=1, timeout=1)
        second = await broadcaster.get_frame(max_age=1, timeout=1)
        self.assertIs(first, second)
        self.assertEqual(calls, 1)

    async def test_unavailable_camera_backs_off(self):
        calls = 0

        async def capture():
            nonlocal calls
            calls += 1
            return None

        broadcaster = FrameBroadcaster(capture)
        queue = await broadcaster.subscribe(30, cached_max_age=0)
        try:
            await asyncio.sleep(0.3)
            self.assertLessEqual(calls, 2)
        finally:
            await broadcaster.unsubscribe(queue)

    async def test_close_wakes_waiting_clients_with_sentinel(self):
        # A camera that never produces a frame keeps stream clients blocked
        # on queue.get(); close() must wake them so /feed responses can end
        # instead of hanging when the mission completes.
        async def capture():
            return None

        broadcaster = FrameBroadcaster(capture)
        queue = await broadcaster.subscribe(30, cached_max_age=0)
        waiter = asyncio.create_task(queue.get())
        await asyncio.sleep(0.05)
        self.assertFalse(waiter.done())

        await broadcaster.close()
        sentinel = await asyncio.wait_for(waiter, timeout=1)
        self.assertIsNone(sentinel)

    async def test_lock_is_rebound_when_the_event_loop_changes(self):
        # Broadcasters are module-level singletons created at import time;
        # their lock must rebind to the loop actually serving requests.
        async def capture():
            return None

        broadcaster = FrameBroadcaster(capture)
        broadcaster._lock = asyncio.Lock()
        broadcaster._lock_loop = object()  # simulates a dead import-time loop
        stale = broadcaster._lock

        lock = broadcaster._get_lock()
        self.assertIsNot(lock, stale)
        self.assertIs(broadcaster._get_lock(), lock)
        queue = await broadcaster.subscribe(10, cached_max_age=0)
        await broadcaster.unsubscribe(queue)

    async def test_close_replaces_stale_frame_with_sentinel(self):
        # A slow client with an undelivered frame still gets the sentinel.
        async def capture():
            return None

        broadcaster = FrameBroadcaster(capture)
        queue = await broadcaster.subscribe(30, cached_max_age=0)
        queue.put_nowait(object())  # simulate an unconsumed frame
        await broadcaster.close()
        self.assertIsNone(queue.get_nowait())


if __name__ == "__main__":
    unittest.main()
