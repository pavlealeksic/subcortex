import json
import os
import tempfile
import time
import unittest
from unittest import mock

import pathsetup  # noqa: F401

from subcortex import config
from subcortex.backends import jev
from subcortex.backends.jev import (
    BackendUnavailableError,
    JevBackend,
    _check_url,
    _join_url,
    _normalize_endpoint_path,
)
from subcortex.metrics import METRICS


def v1_response(answers, tokens=392):
    """The documented v1 shape (docs.typesafe.ai quickstart)."""
    return {"model": "jev-1.13.0", "answers": answers,
            "usage": {"input_tokens": tokens, "output_tokens": 0}}


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

    def test_official_bases_as_pasted_from_the_docs(self):
        cases = {
            ("https://api.typesafe.ai", "/v1/systemone"): "https://api.typesafe.ai/v1/systemone",
            ("https://api.typesafe.ai/v1", "/v1/systemone"): "https://api.typesafe.ai/v1/systemone",
            ("https://api.typesafe.ai", "/systemone"): "https://api.typesafe.ai/v1/systemone",
            ("https://openrouter.ai/api", "/v1/systemone"): "https://openrouter.ai/api/v1/systemone",
            ("https://ai-gateway.vercel.sh/typesafe", "/v1/systemone"):
                "https://ai-gateway.vercel.sh/typesafe/v1/systemone",
            ("https://openrouter.ai", "/api/alpha/decisions"): "https://openrouter.ai/api/alpha/decisions",
        }
        for (base, path), url in cases.items():
            self.assertEqual(_join_url(base, path), url, (base, path))


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
            return 200, json.dumps(v1_response({"q": {"type": "noul", "noul": 0.7}}))

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

    def run_predict(self, response, questions, status=200, env=None):
        def transport(url, body, headers, timeout):
            return status, response if isinstance(response, str) else json.dumps(response)
        with mock.patch.dict(os.environ, env or {"TYPESAFE_API_KEY": "test-key"}), \
                mock.patch.object(jev, "_transport", transport):
            return JevBackend(jev_config()).predict({"prompt": "x"}, questions)

    def test_every_question_must_come_back_with_its_type(self):
        noul = {"q": {"type": "noul", "instructions": "?"}}
        for bad in ({}, {"q": {"type": "choice", "choice": "a"}}, {"q": {"type": "noul"}},
                    {"q": {"type": "noul", "noul": True}}, {"q": "0.5"}):
            with self.assertRaises(BackendUnavailableError, msg=bad):
                self.run_predict(v1_response(bad), noul)
        choice = {"c": {"type": "choice", "instructions": "?", "criteria": {"a": None, "b": None}}}
        with self.assertRaises(BackendUnavailableError):
            self.run_predict(v1_response({"c": {"type": "choice", "choice": "z", "confidence": 1}}), choice)
        ok = self.run_predict(v1_response({"c": {"type": "choice", "choice": "b", "confidence": 0.9},
                                           "future": {"type": "hologram"}}), choice)
        self.assertEqual(ok["answers"]["c"]["choice"], "b")  # unknown extra answers are ignored

    def test_usage_is_metered(self):
        noul = {"q": {"type": "noul", "instructions": "?"}}
        before = METRICS.snapshot()["totals"]
        self.run_predict(v1_response({"q": {"type": "noul", "noul": 0.2}}, tokens=1_000_000), noul)
        after = METRICS.snapshot()
        self.assertEqual(after["totals"]["jev_input_tokens"] - before.get("jev_input_tokens", 0), 1_000_000)
        self.assertAlmostEqual(after["totals"]["jev_cost_usd"] - before.get("jev_cost_usd", 0), 0.042, places=6)
        self.assertEqual(after["latest"]["jev_model"], "jev-1.13.0")
        routed = v1_response({"q": {"type": "noul", "noul": 0.2}}, tokens=10)
        routed["usage"]["cost"] = 0.5  # OpenRouter reports the cost itself
        self.run_predict(routed, noul)
        self.assertAlmostEqual(METRICS.snapshot()["totals"]["jev_cost_usd"] - after["totals"]["jev_cost_usd"], 0.5)

    def test_http_errors_say_what_to_do_without_echoing_the_body(self):
        noul = {"q": {"type": "noul", "instructions": "?"}}
        for status, words in ((401, "key rejected"), (403, "key missing"), (422, "invalid"),
                              (429, "rate limited"), (529, "overloaded"), (503, "server error")):
            with self.assertRaises(BackendUnavailableError) as ctx:
                self.run_predict('{"detail": "sk-live-SECRET in body"}', noul, status=status)
            self.assertIn(words, str(ctx.exception))
            self.assertNotIn("SECRET", str(ctx.exception))

    def test_keys_with_whitespace_are_refused_and_stored_keys_are_stripped(self):
        noul = {"q": {"type": "noul", "instructions": "?"}}
        with self.assertRaises(BackendUnavailableError) as ctx:
            self.run_predict(v1_response({}), noul, env={"TYPESAFE_API_KEY": "sk-abc def"})
        self.assertIn("spaces", str(ctx.exception))
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"SUBCORTEX_CONFIG": f"{tmp}/config.json"}):
                os.environ.pop("TYPESAFE_API_KEY", None)
                config.save_secret("TYPESAFE_API_KEY", "sk-pasted\n")
                self.assertEqual(config.read_secret("TYPESAFE_API_KEY"), "sk-pasted")

    def test_predict_missing_key_raises(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TYPESAFE_API_KEY", None)
            with self.assertRaises(BackendUnavailableError):
                JevBackend(jev_config()).predict("s", {"q": {}})


class TestKeepAliveTransport(unittest.TestCase):
    """The real transport against a local server (cleartext is allowed on loopback)."""

    def serve(self, handler_body):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        connections = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                connections.append(self.client_address)

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                handler_body(self)

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(jev._idle.clear)
        return f"http://127.0.0.1:{server.server_address[1]}/v1/systemone", connections

    @staticmethod
    def ok(handler, close=False):
        body = json.dumps(v1_response({"q": {"type": "noul", "noul": 0.4}})).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        if close:
            handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)

    def test_connections_are_reused(self):
        url, connections = self.serve(self.ok)
        for _ in range(4):
            status, _ = jev._default_transport(url, b"{}", {"Content-Type": "application/json"}, 5)
            self.assertEqual(status, 200)
        self.assertEqual(len(connections), 1)

    def test_a_connection_the_server_dropped_is_retried_once(self):
        def ok_then_hang_up(handler):
            self.ok(handler)
            handler.close_connection = True  # the server times the idle connection out
        url, connections = self.serve(ok_then_hang_up)
        jev._default_transport(url, b"{}", {}, 5)
        time.sleep(0.2)
        status, _ = jev._default_transport(url, b"{}", {}, 5)
        self.assertEqual(status, 200)
        self.assertEqual(len(connections), 2)

    def test_redirects_are_never_followed_with_the_key(self):
        def redirect(handler):
            handler.send_response(307)
            handler.send_header("Location", "http://127.0.0.1:1/steal")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
        url, _ = self.serve(redirect)
        status, _ = jev._default_transport(url, b"{}", {"Authorization": "Bearer k"}, 5)
        self.assertEqual(status, 307)
        with self.assertRaises(BackendUnavailableError):
            jev._parse_response(status, "")

    def test_oversized_responses_fail_closed(self):
        def huge(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", str(jev.MAX_RESPONSE_BYTES + 10))
            handler.end_headers()
            handler.wfile.write(b"x" * (jev.MAX_RESPONSE_BYTES + 10))
        url, _ = self.serve(huge)
        with self.assertRaises(BackendUnavailableError):
            jev._default_transport(url, b"{}", {}, 5)

    def test_proxy_selection(self):
        env = {"HTTPS_PROXY": "http://proxy.corp:3128", "https_proxy": "http://proxy.corp:3128",
               "NO_PROXY": "internal.corp", "no_proxy": "internal.corp"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(jev._proxy_for("https", "api.typesafe.ai"), ("proxy.corp", 3128))
            self.assertIsNone(jev._proxy_for("https", "internal.corp"))
            self.assertIsNone(jev._proxy_for("https", "127.0.0.1"))


if __name__ == "__main__":
    unittest.main()
