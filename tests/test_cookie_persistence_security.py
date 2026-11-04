"""Regression for CVE-2026-54279: persisted host-only cookies stay host-only."""
import tempfile
import unittest
from pathlib import Path

import aiohttp
from yarl import URL


class CookiePersistenceSecurityTest(unittest.IsolatedAsyncioTestCase):
    async def test_host_only_cookie_does_not_leak_after_reload(self):
        jar = aiohttp.CookieJar()
        jar.update_cookies({"session": "test-only-value"}, response_url=URL("https://example.test/"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies"
            jar.save(path)
            restored = aiohttp.CookieJar()
            restored.load(path)
        self.assertIn("session", restored.filter_cookies(URL("https://example.test/")))
        self.assertNotIn("session", restored.filter_cookies(URL("https://child.example.test/")))

    async def test_domain_cookie_still_reaches_subdomain_after_reload(self):
        from http.cookies import SimpleCookie
        cookies = SimpleCookie()
        cookies["shared"] = "test-only-value"
        cookies["shared"]["domain"] = "example.test"
        jar = aiohttp.CookieJar()
        jar.update_cookies(cookies, response_url=URL("https://example.test/"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies"
            jar.save(path)
            restored = aiohttp.CookieJar()
            restored.load(path)
        self.assertIn("shared", restored.filter_cookies(URL("https://child.example.test/")))
