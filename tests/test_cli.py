import contextlib
import copy
import io
import json
import os
import threading
from pathlib import Path
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


class TestTuiCommands(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HOME": self.tmp.name, "XDG_CONFIG_HOME": self.tmp.name + "/.config",
                                                "SUBCORTEX_CONFIG": self.tmp.name + "/sc.json"})
        self.env.start()
        for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPILOT_HOME", "KIMI_CODE_HOME", "GEMINI_CLI_HOME"):
            os.environ.pop(var, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_hook_is_routed_before_argparse(self):
        with mock.patch("subcortex.hook.main", return_value=0) as hook_main:
            self.assertEqual(cli.main(["hook", "claude", "UserPromptSubmit", "--bogus"]), 0)
        hook_main.assert_called_once_with(["claude", "UserPromptSubmit", "--bogus"])

    def test_status_lists_every_tui(self):
        from subcortex import installers

        code, out, _ = self.run_cli(["status", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual({row["tui"] for row in json.loads(out)}, set(installers.names()))
        code, out, _ = self.run_cli(["tuis"])
        self.assertIn("claude-code", out)

    def test_install_requires_confirmation_when_not_interactive(self):
        code, out, _ = self.run_cli(["install", "claude-code", "--no-self-test"])
        self.assertEqual(code, 1)
        self.assertIn("not confirmed", out)
        self.assertFalse(os.path.exists(self.tmp.name + "/.claude/settings.json"))

    def test_install_dry_run_uninstall(self):
        code, out, _ = self.run_cli(["install", "claude", "--dry-run", "--no-self-test", "--ignore-version"])
        self.assertEqual(code, 0)
        self.assertIn("UserPromptSubmit", out)  # the diff
        self.assertFalse(os.path.exists(self.tmp.name + "/.claude/settings.json"))
        code, _, _ = self.run_cli(["install", "--tui", "claude-code", "--yes", "--no-self-test", "--ignore-version",
                                   "--backend", "jev"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.tmp.name + "/.claude/settings.json"))
        self.assertEqual(json.loads(Path(self.tmp.name, "sc.json").read_text())["backend"], "jev")
        code, _, _ = self.run_cli(["uninstall", "claude-code"])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.tmp.name + "/.claude/settings.json"))

    def test_detected_selects_tuis_on_path(self):
        from subcortex import installers

        with mock.patch("shutil.which", side_effect=lambda b: "/bin/x" if b in ("claude", "kimi") else None):
            tuis = cli._resolve_tuis(cli.build_parser().parse_args(["install", "detected"]))
        self.assertEqual(sorted(tuis), ["claude-code", "kimi-code"])
        self.assertTrue(set(tuis) <= set(installers.names()))

    def test_unknown_tui(self):
        code, _, err = self.run_cli(["install", "nope", "--yes"])
        self.assertEqual(code, 2)
        self.assertIn("unknown TUI", err)


class TestWrap(CliFixture):
    """`subcortex wrap` (Aider's test-cmd/lint-cmd) against the in-process daemon."""

    def test_exit_code_and_failures_pass_through(self):
        code, out, _ = self.run_cli(["wrap", "--", "sh", "-c", "echo broken; exit 3"])
        self.assertEqual((code, out), (3, "broken\n"))

    def test_small_output_is_untouched(self):
        code, out, _ = self.run_cli(["wrap", "--", "echo", "hello"])
        self.assertEqual((code, out), (0, "hello\n"))

    def test_missing_command(self):
        code, _, err = self.run_cli(["wrap", "--", "/nonexistent/binary"])
        self.assertEqual(code, 127)
        code, _, err = self.run_cli(["wrap"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
