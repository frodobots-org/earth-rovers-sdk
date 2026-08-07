import asyncio
import base64
import time
import unittest
from unittest.mock import patch

import video_feed
from video_feed import FrameBroadcaster, FrameCaptureError


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
            await broadcaster.close()

    async def test_snapshot_reuses_fresh_cached_frame(self):
        calls = 0

        async def capture():
            nonlocal calls
            calls += 1
            return {"data_url": JPEG_DATA_URL, "timestamp": time.time()}

        broadcaster = FrameBroadcaster(capture)
        # Linger disabled: this test pins the cache contract when the loop
        # is cold — a fresh cached frame is served without a new capture.
        with patch.object(video_feed, "IDLE_LINGER_S", 0):
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
            await broadcaster.close()

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
        await broadcaster.close()

    async def test_close_replaces_stale_frame_with_sentinel(self):
        # A slow client with an undelivered frame still gets the sentinel.
        async def capture():
            return None

        broadcaster = FrameBroadcaster(capture)
        queue = await broadcaster.subscribe(30, cached_max_age=0)
        queue.put_nowait(object())  # simulate an unconsumed frame
        await broadcaster.close()
        self.assertIsNone(queue.get_nowait())

    async def test_polling_keeps_capture_loop_warm_between_requests(self):
        # A 10 Hz snapshot poller must hit a warm cache, not pay loop
        # startup (or a failure backoff) inside every request.
        calls = 0

        async def capture():
            nonlocal calls
            calls += 1
            return {"data_url": JPEG_DATA_URL, "timestamp": time.time()}

        broadcaster = FrameBroadcaster(capture)
        try:
            first = await broadcaster.get_frame(max_age=1 / 30, timeout=1)
            self.assertIsNotNone(first)
            self.assertTrue(broadcaster.loop_running)
            task = broadcaster._task

            await asyncio.sleep(0.15)  # several capture intervals, no clients
            self.assertTrue(broadcaster.loop_running)
            self.assertIs(broadcaster._task, task)
            self.assertGreater(calls, 1)  # kept capturing with no subscribers

            started = time.monotonic()
            second = await broadcaster.get_frame(max_age=1 / 30, timeout=1)
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertIsNotNone(second)
            self.assertIsNot(first, second)  # a fresh frame, not the old one
            self.assertGreater(broadcaster.captures_total, 1)
        finally:
            await broadcaster.close()

    async def test_capture_loop_exits_after_idle_linger(self):
        async def capture():
            return {"data_url": JPEG_DATA_URL, "timestamp": time.time()}

        broadcaster = FrameBroadcaster(capture)
        with patch.object(video_feed, "IDLE_LINGER_S", 0.05):
            await broadcaster.get_frame(max_age=0, timeout=1)
            self.assertTrue(broadcaster.loop_running)
            await asyncio.sleep(0.3)
        self.assertFalse(broadcaster.loop_running)
        self.assertIsNone(broadcaster._task)

    async def test_failing_capture_notifies_snapshot_before_backoff(self):
        # The recovery backoff belongs to the background loop. A request is
        # bounded by its own timeout — never by backoff sleeps (which used
        # to stretch a poll to 2-5 s before returning empty).
        async def capture():
            return None

        broadcaster = FrameBroadcaster(capture)
        try:
            started = time.monotonic()
            with self.assertRaises(FrameCaptureError) as ctx:
                await broadcaster.get_frame(max_age=1 / 30, timeout=0.5)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.2)
            self.assertGreaterEqual(broadcaster.failures_total, 1)
            self.assertEqual(str(ctx.exception), "camera frame is not available")
            self.assertEqual(
                broadcaster.last_error, "camera frame is not available"
            )
        finally:
            await broadcaster.close()

    async def test_snapshot_during_recovery_does_not_join_backoff(self):
        async def capture():
            return None

        broadcaster = FrameBroadcaster(capture)
        try:
            with self.assertRaises(FrameCaptureError):
                await broadcaster.get_frame(max_age=1 / 30, timeout=1)

            started = time.monotonic()
            with self.assertRaises(FrameCaptureError):
                await broadcaster.get_frame(max_age=1 / 30, timeout=1)
            self.assertLess(time.monotonic() - started, 0.05)
        finally:
            await broadcaster.close()

    async def test_feed_client_stays_subscribed_across_capture_failure(self):
        calls = 0

        async def capture():
            nonlocal calls
            calls += 1
            if calls == 1:
                return None
            return {"data_url": JPEG_DATA_URL, "timestamp": time.time()}

        broadcaster = FrameBroadcaster(capture)
        queue = await broadcaster.subscribe(30, cached_max_age=0)
        try:
            frame = await asyncio.wait_for(queue.get(), timeout=1)
            self.assertEqual(frame.jpeg, b"\xff\xd8test-frame\xff\xd9")
            self.assertEqual(broadcaster.failures_total, 1)
        finally:
            await broadcaster.unsubscribe(queue)
            await broadcaster.close()

    async def test_close_during_linger_stops_loop_promptly(self):
        # Mission teardown must not wait out the idle linger.
        async def capture():
            return {"data_url": JPEG_DATA_URL, "timestamp": time.time()}

        broadcaster = FrameBroadcaster(capture)
        await broadcaster.get_frame(max_age=0, timeout=1)
        self.assertTrue(broadcaster.loop_running)
        await broadcaster.close()
        self.assertFalse(broadcaster.loop_running)
        self.assertIsNone(broadcaster._task)


if __name__ == "__main__":
    unittest.main()
