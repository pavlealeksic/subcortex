import copy
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import pathsetup  # noqa: F401

from subcortex import __version__, auth

from subcortex import daemon
from subcortex.config import DEFAULT_CONFIG


class FakeBackend:
    """A sure model: every prompt simple, every output needed."""

    name = "fake"

    def predict(self, state, questions):
        from subcortex.verdicts import canned_answers

        return canned_answers(questions, simple=True, disposable=False)


class ExplodingBackend:
    name = "exploding"

    def predict(self, state, questions):
        raise RuntimeError("backend exploded")


class DaemonFixture:
    """In-thread daemon on an ephemeral port with a stubbed backend factory."""

    def __init__(self, backend, factory=None):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        self.server = daemon.create_server(
            0, config=cfg, backend_factory=factory or (lambda config, name=None: backend))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _headers(**extra):
    return {"Content-Type": "application/json", auth.HEADER: auth.read_token(), **extra}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_headers()), timeout=5) as resp:
        return json.loads(resp.read().decode())


def post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST", headers=_headers())
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


class TestDaemon(unittest.TestCase):
    def setUp(self):
        self.fx = DaemonFixture(FakeBackend())

    def tearDown(self):
        self.fx.stop()

    def test_health(self):
        data = get(self.fx.url + "/health")
        self.assertTrue(data["ok"])
        self.assertEqual(data["backend"], "laya")
        self.assertEqual(data["model"], "multilingual")

    def test_decide(self):
        data = post(self.fx.url + "/decide", {
            "state": {"prompt": "hi"},
            "questions": {"hard": {"type": "noul", "instructions": "Is it hard?"}},
        })
        self.assertTrue(data["success"])
        self.assertEqual(data["answers"], {"hard": {"type": "noul", "noul": 0.5}})

    def test_decide_missing_fields(self):
        try:
            post(self.fx.url + "/decide", {"state": "x"})
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_decide_backend_failure_returns_success_false(self):
        fx = DaemonFixture(ExplodingBackend())
        try:
            data = post(fx.url + "/decide", {"state": "x", "questions": {"q": {}}})
            self.assertFalse(data["success"])
            self.assertIn("backend exploded", data["error"])
        finally:
            fx.stop()

    def test_verdict_prompt(self):
        data = post(self.fx.url + "/verdict/prompt", {"prompt": "what is 2+2?"})
        self.assertTrue(data["success"])
        self.assertEqual(data["verdict"]["label"], "simple")
        self.assertAlmostEqual(data["verdict"]["confidence"], 1.0)

    def test_verdict_output_short(self):
        data = post(self.fx.url + "/verdict/output", {"output": "short"})
        self.assertTrue(data["success"])
        self.assertEqual(data["verdict"], {"needed": True, "p_needed": 1.0})

    def test_verdict_output_long(self):
        data = post(self.fx.url + "/verdict/output",
                    {"output": "x" * 7000, "context": "Bash: make", "task": "fix the build"})
        self.assertTrue(data["success"])
        self.assertTrue(data["verdict"]["needed"])
        self.assertAlmostEqual(data["verdict"]["p_needed"], 1.0)

    def test_stats(self):
        post(self.fx.url + "/decide", {"state": "x", "questions": {"q": {}}})
        post(self.fx.url + "/verdict/prompt", {"prompt": "hi"})
        data = get(self.fx.url + "/stats")
        self.assertIn("uptime_s", data)
        self.assertGreaterEqual(data["counts"].get("decide", 0), 1)
        self.assertGreaterEqual(data["counts"].get("verdict_prompt", 0), 1)

    def test_unknown_path_404(self):
        try:
            get(self.fx.url + "/nope")
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)


class StubPolicyBackend:
    name = "stub"

    def predict(self, state, questions):
        from subcortex.verdicts import canned_answers

        return canned_answers(questions)  # every prompt simple, every output disposable


class TestPolicyEndpoints(unittest.TestCase):
    """/v1/* — the shared policy served to JS plugins."""

    def setUp(self):
        import os
        import tempfile
        from unittest import mock

        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"SUBCORTEX_DATA_DIR": self.tmp.name})
        self.env.start()
        self.d = DaemonFixture(StubPolicyBackend())

    def tearDown(self):
        self.d.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_prompt_hint(self):
        body = post(self.d.url + "/v1/prompt-hint", {"prompt": "what is 2+2?"})
        self.assertTrue(body["success"])
        self.assertIn("simple", body["hint"])
        self.assertIsNone(post(self.d.url + "/v1/prompt-hint", {"prompt": ""})["hint"])

    def test_tool_output(self):
        big = "compiling module\n" * 800
        # No request known for the session: nothing to judge against, output kept.
        self.assertIsNone(post(self.d.url + "/v1/tool-output", {"output": big, "tool": "bash",
                                                                 "session_id": "s9"})["replacement"])
        post(self.d.url + "/v1/prompt-hint", {"prompt": "why is the build slow?", "session_id": "s9", "tui": "x"})
        body = post(self.d.url + "/v1/tool-output", {"output": big, "tool": "bash", "input": {"command": "make"},
                                                     "session_id": "s9", "tui": "x"})
        self.assertIn("[subcortex: truncated", body["replacement"])
        self.assertIsNone(post(self.d.url + "/v1/tool-output", {"output": big, "failed": True})["replacement"])
        self.assertIsNone(post(self.d.url + "/v1/tool-output", {"output": "short"})["replacement"])

    def test_snapshot_restore_with_raw_plugin_messages(self):
        messages = [{"role": "user", "content": [{"type": "text", "text": "fix login"}]},
                    {"role": "info", "content": "ignored"},
                    {"role": "assistant", "text": "Fixed."}]
        self.assertTrue(post(self.d.url + "/v1/snapshot", {"session_id": "T-1", "messages": messages})["saved"])
        context = post(self.d.url + "/v1/restore", {"session_id": "T-1"})["context"]
        self.assertIn("user: fix login", context)
        self.assertIn("assistant: Fixed.", context)
        self.assertNotIn("ignored", context)
        self.assertIsNone(post(self.d.url + "/v1/restore", {"session_id": "T-1"})["context"])

    def test_bad_bodies(self):
        req = urllib.request.Request(self.d.url + "/v1/prompt-hint", data=b"not json", method="POST",
                                     headers=_headers())
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)


class TestRequestGuards(unittest.TestCase):
    """Only this user's local processes may use the daemon: not web pages, not other users."""

    def setUp(self):
        self.d = DaemonFixture(FakeBackend())

    def tearDown(self):
        self.d.stop()

    def status(self, path="/v1/restore", body=b'{"session_id": "s"}', **headers):
        req = urllib.request.Request(self.d.url + path, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_token_is_required(self):
        self.assertEqual(self.status(**{"Content-Type": "application/json"}), 401)
        self.assertEqual(self.status(**{"Content-Type": "application/json", auth.HEADER: "0" * 64}), 401)
        self.assertEqual(self.status(**_headers()), 200)

    def test_browser_shaped_requests_are_refused(self):
        # A simple cross-origin POST (text/plain, with Origin) and DNS rebinding (foreign Host).
        self.assertEqual(self.status(**_headers(Origin="https://evil.example")), 403)
        self.assertEqual(self.status(**_headers(Host="evil.example:7707")), 403)
        self.assertEqual(self.status(**{**_headers(), "Content-Type": "text/plain"}), 400)

    def test_health_needs_no_token_but_stats_do(self):
        with urllib.request.urlopen(self.d.url + "/health", timeout=5) as resp:
            self.assertEqual(json.loads(resp.read())["version"], __version__)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.d.url + "/stats", timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_hook_routes_ignore_a_per_request_backend(self):
        seen = []
        fx = DaemonFixture(FakeBackend(), factory=lambda cfg, name=None: seen.append(name) or FakeBackend())
        try:
            post(fx.url + "/verdict/prompt", {"prompt": "hi", "backend": "jev"})
            post(fx.url + "/v1/prompt-hint", {"prompt": "hi", "backend": "jev"})
        finally:
            fx.stop()
        self.assertNotIn("jev", seen)


class TestLoadShedding(unittest.TestCase):
    def test_requests_that_cannot_start_in_time_get_503_quickly(self):
        release = threading.Event()

        class Slow(FakeBackend):
            def predict(self, state, questions):
                release.wait(10)
                return super().predict(state, questions)
        fx = DaemonFixture(Slow())
        results = []

        def call(timeout_ms):
            req = urllib.request.Request(fx.url + "/verdict/prompt", data=b'{"prompt": "x"}', method="POST",
                                         headers=_headers(**{"X-Subcortex-Timeout-Ms": str(timeout_ms)}))
            started = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    results.append((resp.status, time.monotonic() - started))
            except urllib.error.HTTPError as exc:
                results.append((exc.code, time.monotonic() - started))
        try:
            busy = [threading.Thread(target=call, args=(10000,)) for _ in range(daemon.MAX_CONCURRENT_DECISIONS)]
            for t in busy:
                t.start()
            time.sleep(0.3)
            call(500)  # every slot is taken: refused within its own budget
            release.set()
            for t in busy:
                t.join()
        finally:
            fx.stop()
        code, elapsed = results[0]
        self.assertEqual(code, 503)
        self.assertLess(elapsed, 1.5)
        self.assertEqual(sorted(r[0] for r in results[1:]), [200] * daemon.MAX_CONCURRENT_DECISIONS)


class TestConfigReload(unittest.TestCase):
    def test_an_opt_out_applies_without_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp, "config.json")
            config.write_text("{}")
            with mock.patch.dict(os.environ, {"SUBCORTEX_CONFIG": str(config)}):
                server = daemon.create_server(0, None, backend_factory=lambda c, name=None: FakeBackend())
                threading.Thread(target=server.serve_forever, daemon=True).start()
                url = f"http://127.0.0.1:{server.server_address[1]}"
                try:
                    self.assertIsNotNone(post(url + "/v1/prompt-hint", {"prompt": "what is 2+2?"})["hint"])
                    config.write_text(json.dumps({"features": {"prompt_hint": False}}))
                    os.utime(config, (time.time() + 5, time.time() + 5))
                    time.sleep(1.1)
                    self.assertIsNone(post(url + "/v1/prompt-hint", {"prompt": "what is 2+2?"})["hint"])
                finally:
                    server.shutdown()
                    server.server_close()


if __name__ == "__main__":
    unittest.main()
