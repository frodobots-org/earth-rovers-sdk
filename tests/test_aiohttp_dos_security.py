"""Bounded regressions for aiohttp CVE-2026-128126 and CVE-2026-128131."""
import gzip
import importlib.metadata
import unittest
from unittest.mock import patch

from aiohttp import web, compression_utils
from aiohttp.test_utils import TestClient, TestServer


class AiohttpDosSecurityTest(unittest.IsolatedAsyncioTestCase):
    def test_fixed_version_is_installed(self):
        self.assertGreaterEqual(
            tuple(int(x) for x in importlib.metadata.version("aiohttp").split(".")[:3]),
            (3, 14, 4),
        )

    async def test_urlencoded_form_field_limit(self):
        async def form(request):
            data = await request.post()
            return web.json_response({"count": len(data.getall("key", []))})

        app = web.Application(client_max_fields=3)
        app.router.add_post("/", form)
        async with TestClient(TestServer(app)) as client:
            good = await client.post("/", data="key=a&key=b&key=c",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(good.status, 200)
            self.assertEqual(await good.json(), {"count": 3})
            bad = await client.post("/", data="key=a&key=b&key=c&key=d",
                                    headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(bad.status, 413)

    def test_gzip_concatenation_preserves_normal_payload(self):
        decoder = compression_utils.ZLibDecompressor(encoding="gzip")
        self.assertEqual(
            decoder.decompress_sync(gzip.compress(b"first") + gzip.compress(b"second")),
            b"firstsecond",
        )

    def test_empty_gzip_members_are_bounded(self):
        # Lower the upstream limit for a small deterministic test, not a CPU stress test.
        with patch.object(compression_utils, "MAX_DECOMPRESS_MEMBERS", 3):
            decoder = compression_utils.ZLibDecompressor(encoding="gzip")
            with self.assertRaises(compression_utils.TooManyMembersError):
                decoder.decompress_sync(gzip.compress(b"") * 4)
