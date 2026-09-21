import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
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

    def test_stats_report_what_was_delivered(self):
        from subcortex import ledger

        ledger.record("hint", "claude-code")
        ledger.record("trim", "claude-code", before=40000, after=4000)
        ledger.record("restore", "codex")
        ledger.record("jev", tokens=1_000_000, usd=0.042)
        code, out, _ = self.run_cli(["stats", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertGreaterEqual(data["total"]["trims"], 1)
        self.assertGreaterEqual(data["total"]["chars_removed"], 36000)
        self.assertGreaterEqual(data["total"]["jev_cost_usd"], 0.042)
        self.assertIn("uptime_s", data["daemon"])
        code, out, _ = self.run_cli(["stats"])
        self.assertIn("tokens of context saved", out)
        self.assertIn("claude-code", out)

    def test_stats_work_with_the_daemon_down(self):
        with mock.patch.dict(os.environ, {"SUBCORTEX_PORT": "1"}):
            code, out, _ = self.run_cli(["stats"])
        self.assertEqual(code, 0)
        self.assertIn("daemon not running", out)


class TestEval(unittest.TestCase):
    """`subcortex eval` must catch a model that would hint complex work or trim needed output."""

    def run_eval(self, backend):
        out = io.StringIO()
        with mock.patch("subcortex.backends.get_backend", return_value=backend), \
                mock.patch.dict(os.environ, {"SUBCORTEX_CONFIG": "/nonexistent/c.json"}), \
                contextlib.redirect_stdout(out):
            code = cli.main(["eval"])
        return code, out.getvalue()

    def test_a_reckless_model_fails(self):
        from subcortex.verdicts import canned_answers

        class Reckless:
            name = "jev"

            def available(self):
                return True, "ok"

            def predict(self, state, questions):
                return canned_answers(questions, simple=True, disposable=True)
        code, out = self.run_eval(Reckless())
        self.assertEqual(code, 1)
        self.assertIn("hinted a complex request", out)
        self.assertIn("trimmed needed output", out)

    def test_an_oracle_passes(self):
        from subcortex import evalset
        from subcortex.verdicts import canned_answers

        simple = {p: s for p, s in evalset.PROMPTS + evalset.HELDOUT_PROMPTS}
        needed = {(t, c): n for t, c, _, n in evalset.OUTPUTS + evalset.HELDOUT_OUTPUTS}

        class Oracle:
            name = "jev"

            def available(self):
                return True, "ok"

            def predict(self, state, questions):
                if "prompt" in state:
                    return canned_answers(questions, simple=simple.get(state["prompt"], False))
                return canned_answers(questions, disposable=not needed.get((state["task"], state["tool_call"]), True))
        code, out = self.run_eval(Oracle())
        self.assertEqual(code, 0, out)
        self.assertIn("held out", out)


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

    def run_wrap(self, *command):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        proc = subprocess.run([sys.executable, "-m", "subcortex", "wrap", "--", *command],
                              capture_output=True, env=env, timeout=60)
        return proc.returncode, proc.stdout

    def test_bytes_pass_through_exactly_when_nothing_is_trimmed(self):
        code, out = self.run_wrap("sh", "-c", r"printf 'caf\351 \377\376 ok\n'")
        self.assertEqual((code, out), (0, b"caf\xe9 \xff\xfe ok\n"))

    def test_a_signal_death_keeps_the_shell_convention(self):
        code, _ = self.run_wrap("sh", "-c", "kill -TERM $$")
        self.assertEqual(code, 128 + 15)

    def test_large_routine_output_is_trimmed(self):
        self.server.shutdown()  # swap in a sure model that calls everything disposable
        self.server.server_close()

        class Sure:
            name = "jev"

            def predict(self, state, questions):
                from subcortex.verdicts import canned_answers

                return canned_answers(questions)
        self.server = daemon.create_server(0, config=copy.deepcopy(DEFAULT_CONFIG),
                                           backend_factory=lambda c, name=None: Sure())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        os.environ["SUBCORTEX_PORT"] = str(self.server.server_address[1])
        code, out = self.run_wrap("sh", "-c", "yes 'compiling module ok' | head -1500")
        self.assertEqual(code, 0)
        self.assertIn(b"[subcortex: truncated", out)
        self.assertLess(len(out), 1500 * 22 * 0.8)

    def test_missing_command(self):
        code, _, err = self.run_cli(["wrap", "--", "/nonexistent/binary"])
        self.assertEqual(code, 127)
        code, _, err = self.run_cli(["wrap"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
