import unittest

from playwright.async_api import Error as PlaywrightError

from browser_service import BrowserService


class FakePage:
    def __init__(self, closed=False):
        self.closed = closed
        self.close_calls = 0

    def is_closed(self):
        return self.closed

    async def close(self):
        self.close_calls += 1
        self.closed = True


class FakeBrowser:
    def __init__(self, connected=True):
        self.connected = connected

    def is_connected(self):
        return self.connected

    async def close(self):
        self.connected = False


class BrowserRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_stale_failure_cannot_destroy_new_generation(self):
        service = BrowserService()
        stale = FakePage(closed=True)
        current = FakePage()
        service._page = current
        service._browser = FakeBrowser()
        service._ready = True

        await service._invalidate(stale)

        self.assertIs(service._page, current)
        self.assertEqual(current.close_calls, 0)

    async def test_application_error_does_not_destroy_healthy_browser(self):
        service = BrowserService()
        page = FakePage()
        service._page = page
        service._browser = FakeBrowser()
        service._ready = True

        async def fail(_page):
            raise PlaywrightError("JavaScript application error")

        with self.assertRaises(PlaywrightError):
            await service._run(fail)

        self.assertIs(service._page, page)
        self.assertEqual(page.close_calls, 0)

    async def test_page_is_not_ready_until_launch_finishes(self):
        service = BrowserService()
        service._page = FakePage()
        service._browser = FakeBrowser()

        self.assertFalse(service.is_ready)

        service._ready = True
        self.assertTrue(service.is_ready)


class LockLoopBindingTest(unittest.IsolatedAsyncioTestCase):
    async def test_lock_is_rebound_when_the_event_loop_changes(self):
        # The service is constructed at import time, before hypercorn's loop
        # exists. On Python 3.9 a Lock made then is bound to the wrong loop
        # and raises "attached to a different loop" under contention.
        import asyncio

        from browser_service import BrowserService

        service = BrowserService()
        service._lock = asyncio.Lock()
        service._lock_loop = object()  # simulates a dead import-time loop
        stale = service._lock

        lock = service._get_lock()
        self.assertIsNot(lock, stale)
        # Stable within the same running loop
        self.assertIs(service._get_lock(), lock)
        async with service._get_lock():
            pass  # must be acquirable on this loop


if __name__ == "__main__":
    unittest.main()
