import json
import os
import unittest
from unittest import mock

import pathsetup  # noqa: F401

from subcortex.backends import jev
from subcortex.backends.jev import (
    BackendUnavailableError,
    JevBackend,
    _check_url,
    _join_url,
    _normalize_endpoint_path,
)


def jev_config(**overrides):
    cfg = {"jev": {}}
    cfg["jev"].update(overrides)
    return cfg


class TestUrlPolicy(unittest.TestCase):
    def test_https_public_allowed(self):
        scheme, host = _check_url("https://api.typesafe.ai/v1/systemone")
        self.assertEqual((scheme, host), ("https", "api.typesafe.ai"))

    def test_http_public_rejected(self):
        with self.assertRaises(BackendUnavailableError):
            _check_url("http://example.com/systemone")

    def test_http_loopback_allowed(self):
        _check_url("http://127.0.0.1:8080/systemone")
        _check_url("http://localhost:8080/systemone")
        _check_url("http://[::1]:8080/systemone")

    def test_http_private_lan_allowed(self):
        _check_url("http://192.168.1.10:8080/systemone")
        _check_url("http://10.0.0.5/systemone")

    def test_http_public_dns_name_rejected(self):
        with self.assertRaises(BackendUnavailableError):
            _check_url("http://internal.lan/systemone")

    def test_userinfo_rejected(self):
        with self.assertRaises(BackendUnavailableError):
            _check_url("https://user:pass@api.typesafe.ai/v1")

    def test_non_http_scheme_rejected(self):
        with self.assertRaises(BackendUnavailableError):
            _check_url("ftp://127.0.0.1/x")

    def test_metadata_endpoint_rejected(self):
        # 169.254.169.254 is link-local — must NOT count as private.
        with self.assertRaises(BackendUnavailableError):
            _check_url("http://169.254.169.254/latest/meta-data")


class TestEndpointPath(unittest.TestCase):
    def test_good_paths_kept(self):
        self.assertEqual(_normalize_endpoint_path("/systemone"), "/systemone")
        self.assertEqual(_normalize_endpoint_path("/api/alpha/decisions"),
                         "/api/alpha/decisions")

    def test_bad_paths_fail_closed_to_default(self):
        for bad in ("", "systemone", "/x y", "https://evil.com/x", "/x?y=1",
                    "/x#frag", "/x%2fy", "/x@y", "/x\ny", "/ü"):
            self.assertEqual(_normalize_endpoint_path(bad),
                             jev.DEFAULT_ENDPOINT_PATH, bad)

    def test_join_url(self):
        self.assertEqual(
            _join_url("https://api.typesafe.ai/v1", "/systemone"),
            "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(
            _join_url("https://api.typesafe.ai/v1/", "/systemone/"),
            "https://api.typesafe.ai/v1/systemone")


class TestJevBackend(unittest.TestCase):
    def test_available_without_key_fails_with_fix(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TYPESAFE_API_KEY", None)
            ok, reason = JevBackend(jev_config()).available()
        self.assertFalse(ok)
        self.assertIn("TYPESAFE_API_KEY", reason)
        self.assertIn("export", reason)

    def test_available_with_key(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}):
            ok, reason = JevBackend(jev_config()).available()
        self.assertTrue(ok, reason)

    def test_available_rejects_bad_url(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}):
            ok, reason = JevBackend(jev_config(base_url="http://example.com")).available()
        self.assertFalse(ok)
        self.assertIn("cleartext", reason)

    def test_predict_with_stubbed_transport(self):
        seen = {}

        def fake_transport(url, body, headers, timeout):
            seen["url"] = url
            seen["body"] = json.loads(body.decode())
            seen["auth"] = headers["Authorization"]
            return 200, json.dumps({"answers": {"q": {"noul": 0.7}}})

        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}), \
                mock.patch.object(jev, "_transport", fake_transport):
            backend = JevBackend(jev_config())
            result = backend.predict({"prompt": "hi"}, {"q": {"type": "noul", "instructions": "?"}})
        self.assertEqual(result["answers"]["q"]["noul"], 0.7)
        self.assertEqual(seen["url"], "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(seen["body"]["model"], "jev-latest")
        self.assertEqual(seen["auth"], "Bearer test-key")

    def test_predict_validates_probabilities(self):
        def bad_transport(url, body, headers, timeout):
            return 200, json.dumps({"answers": {"q": {"noul": 1.5}}})

        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-key"}), \
                mock.patch.object(jev, "_transport", bad_transport):
            with self.assertRaises(BackendUnavailableError):
                JevBackend(jev_config()).predict("s", {"q": {"type": "noul"}})

    def test_predict_missing_key_raises(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TYPESAFE_API_KEY", None)
            with self.assertRaises(BackendUnavailableError):
                JevBackend(jev_config()).predict("s", {"q": {}})


if __name__ == "__main__":
    unittest.main()
