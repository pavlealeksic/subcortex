import contextlib
import copy
import io
import json
import os
import threading
import unittest
from unittest import mock

import pathsetup  # noqa: F401

from subcortex import cli, daemon
from subcortex.config import DEFAULT_CONFIG


class FakeBackend:
    name = "fake"

    def predict(self, state, questions):
        return {"answers": {name: {"noul": 0.42} for name in questions}}


class CliFixture(unittest.TestCase):
    """Starts an in-process daemon on an ephemeral port and points the CLI at
    it via SUBCORTEX_PORT, so no subprocess and no real backend are needed."""

    def setUp(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        self.server = daemon.create_server(
            0, config=cfg, backend_factory=lambda config, name=None: FakeBackend())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._env = mock.patch.dict(os.environ, {
            "SUBCORTEX_CONFIG": "/nonexistent/subcortex/config.json",
            "SUBCORTEX_PORT": str(self.port),
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()


class TestCli(CliFixture):
    def test_decide(self):
        code, out, _ = self.run_cli([
            "decide", "--state", "hello",
            "--questions", json.dumps({"hard": {"type": "noul", "instructions": "hard?"}}),
        ])
        self.assertEqual(code, 0)
        resp = json.loads(out)
        self.assertTrue(resp["success"])
        self.assertEqual(resp["answers"], {"hard": {"noul": 0.42}})

    def test_decide_backend_override_accepted(self):
        code, out, _ = self.run_cli([
            "decide", "--state", "x", "--questions", '{"q": {"type": "noul"}}',
            "--backend", "jev",
        ])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["success"])

    def test_decide_bad_questions_json(self):
        code, _, err = self.run_cli(["decide", "--state", "x", "--questions", "{nope"])
        self.assertEqual(code, 2)
        self.assertIn("JSON", err)

    def test_stats(self):
        code, out, _ = self.run_cli(["stats"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("uptime_s", data)
        self.assertIn("counts", data)

    def test_stats_daemon_down(self):
        with mock.patch.dict(os.environ, {"SUBCORTEX_PORT": "1"}):
            code, _, err = self.run_cli(["stats"])
        self.assertEqual(code, 1)
        self.assertIn("not reachable", err)


if __name__ == "__main__":
    unittest.main()
