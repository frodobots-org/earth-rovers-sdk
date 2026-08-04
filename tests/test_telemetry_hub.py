import unittest

from telemetry_hub import TelemetryHub


class TelemetryHubTest(unittest.IsolatedAsyncioTestCase):
    async def test_slow_client_keeps_only_latest_message(self):
        hub = TelemetryHub()
        queue = hub.subscribe()

        hub.publish({"sequence": 1})
        hub.publish({"sequence": 2})

        message = queue.get_nowait()
        self.assertEqual(message["data"]["sequence"], 2)
        self.assertTrue(queue.empty())

    async def test_ingest_status_tracks_multiple_connections(self):
        hub = TelemetryHub()
        first = hub.connect_ingest()
        second = hub.connect_ingest()
        self.assertEqual(hub.status()["ingest_connections"], 2)
        hub.disconnect_ingest(first)
        self.assertTrue(hub.ingest_connected)
        self.assertEqual(hub.status()["ingest_connections"], 1)
        hub.disconnect_ingest(second)
        self.assertFalse(hub.ingest_connected)
        self.assertEqual(hub.status()["ingest_connections"], 0)


if __name__ == "__main__":
    unittest.main()
