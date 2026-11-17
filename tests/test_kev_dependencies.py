"""Offline guards for CISA KEV dependency fixes; no robot or network access."""
import hashlib
from pathlib import Path
import unittest
from starlette.datastructures import URL

ROOT = Path(__file__).resolve().parents[1]

class KevDependenciesTest(unittest.TestCase):
    def test_host_cannot_rewrite_url_path(self):
        for host in (b'example.test/public?ignored=', b'example.test#ignored', b'example.test/../public'):
            with self.subTest(host=host):
                scope = {'type': 'http', 'scheme': 'http', 'server': ('testserver', 80),
                         'path': '/private', 'query_string': b'', 'headers': [(b'host', host)]}
                self.assertEqual(URL(scope=scope).path, '/private')
                self.assertEqual(URL(scope=scope).hostname, 'testserver')

    def test_valid_host_preserved(self):
        scope = {'type': 'http', 'scheme': 'http', 'server': ('testserver', 80),
                 'path': '/private', 'query_string': b'a=1', 'headers': [(b'host', b'example.test:8080')]}
        self.assertEqual(str(URL(scope=scope)), 'http://example.test:8080/private?a=1')

    def test_patched_jquery_is_referenced(self):
        html = (ROOT / 'index.html').read_text()
        self.assertIn('/static/jquery-3.7.1.min.js', html)
        self.assertNotIn('jquery-3.4.1', html)
        self.assertFalse((ROOT / 'static/jquery-3.4.1.min.js').exists())
        vendor = ROOT / 'static/jquery-3.7.1.min.js'
        self.assertTrue(vendor.read_text().startswith('/*! jQuery v3.7.1'))
        self.assertEqual(hashlib.sha256(vendor.read_bytes()).hexdigest(), 'fc9a93dd241f6b045cbff0481cf4e1901becd0e12fb45166a8f17f95823f0b1a')
