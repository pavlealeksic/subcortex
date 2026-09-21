import copy
import json
import threading
import unittest
import urllib.error
import urllib.request

import pathsetup  # noqa: F401

from subcortex import daemon
from subcortex.config import DEFAULT_CONFIG


class FakeBackend:
    name = "fake"

    def predict(self, state, questions):
        return {"answers": {name: {"noul": 0.9} for name in questions}}


class ExplodingBackend:
    name = "exploding"

    def predict(self, state, questions):
        raise RuntimeError("backend exploded")


class DaemonFixture:
    """In-thread daemon on an ephemeral port with a stubbed backend factory."""

    def __init__(self, backend):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        self.server = daemon.create_server(
            0, config=cfg, backend_factory=lambda config, name=None: backend)
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


def get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode())


def post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
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
        self.assertEqual(data["answers"], {"hard": {"noul": 0.9}})

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
        self.assertAlmostEqual(data["verdict"]["confidence"], 0.9)

    def test_verdict_output_short(self):
        data = post(self.fx.url + "/verdict/output", {"output": "short"})
        self.assertTrue(data["success"])
        self.assertEqual(data["verdict"], {"needed": True, "p_needed": 1.0})

    def test_verdict_output_long(self):
        data = post(self.fx.url + "/verdict/output",
                    {"output": "x" * 7000, "context": "task"})
        self.assertTrue(data["success"])
        self.assertTrue(data["verdict"]["needed"])
        self.assertAlmostEqual(data["verdict"]["p_needed"], 0.9)

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


if __name__ == "__main__":
    unittest.main()
