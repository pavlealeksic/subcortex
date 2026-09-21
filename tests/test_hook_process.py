"""The hook *process* contract, exercised through real subprocesses.

This is the layer that failed in 0.1.0: `subcortex hook claude …` exited 2
(an argparse usage error), which Claude Code reads as "block this prompt".
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import pathsetup  # noqa: F401

SRC = str(Path(__file__).resolve().parents[1] / "src")
PROMPT = {"session_id": "s1", "transcript_path": "/tmp/t.jsonl", "cwd": "/tmp",
          "hook_event_name": "UserPromptSubmit", "prompt": "what is 2+2?"}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HookProcess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = Path(self.tmp.name, "config.json")
        config.write_text("{}")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("SUBCORTEX_")}
        self.env.update(PYTHONPATH=SRC, SUBCORTEX_CONFIG=str(config), SUBCORTEX_AUTOSTART="0",
                        SUBCORTEX_DATA_DIR=self.tmp.name, SUBCORTEX_PORT=str(free_port()))

    def tearDown(self):
        self.tmp.cleanup()

    def run_argv(self, argv, stdin, env=None, timeout=20):
        started = time.perf_counter()
        proc = subprocess.run([sys.executable, *argv], input=stdin, capture_output=True,
                              text=True, env=env or self.env, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr, time.perf_counter() - started

    def assert_silent_zero(self, argv, stdin, env=None):
        code, out, err, _ = self.run_argv(argv, stdin, env)
        self.assertEqual((code, out, err), (0, "", ""), f"argv={argv} stdin={stdin[:60]!r}")

    def test_the_0_1_0_claude_command_now_exits_zero(self):
        # Exactly what the 0.1.0 installer wrote into ~/.claude/settings.json.
        self.assert_silent_zero(["-m", "subcortex", "hook", "claude", "UserPromptSubmit"], json.dumps(PROMPT))

    def test_every_entry_point_and_garbage_is_silent_and_zero(self):
        entries = [["-m", "subcortex", "hook"], ["-m", "subcortex.hook"]]
        argvs = [[], ["claude-code"], ["nope", "x"], ["claude-code", "NoSuchEvent"],
                 ["claude-code", "UserPromptSubmit", "extra", "--help"], ["--help"], ["-h"]]
        stdins = ["", "{", "[]", "null", "\x00\xff garbage", json.dumps(PROMPT)]
        for entry in entries:
            for argv in argvs:
                for stdin in stdins:
                    self.assert_silent_zero(entry + argv, stdin)

    def test_hung_daemon_is_cut_off_by_the_watchdog(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)  # accepts connections, never answers
        accepted = []
        threading.Thread(target=lambda: accepted.append(listener.accept()), daemon=True).start()
        env = dict(self.env, SUBCORTEX_PORT=str(listener.getsockname()[1]), SUBCORTEX_HOOK_BUDGET="1")
        try:
            code, out, err, secs = self.run_argv(["-m", "subcortex.hook", "claude-code", "UserPromptSubmit"],
                                                 json.dumps(PROMPT), env)
        finally:
            for conn, _ in accepted:
                conn.close()
            listener.close()
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertLess(secs, 3.0)

    def test_stray_prints_inside_the_hook_never_reach_stdout(self):
        import io
        from unittest import mock

        from subcortex import hook as hook_mod

        def chatty_run(*args, **kwargs):
            print("debug noise from somewhere deep")
            print("more noise", file=sys.stderr)
            return '{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "x"}}'

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(hook_mod, "run", chatty_run), \
                mock.patch.object(sys, "stdin", io.StringIO("{}")), \
                mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", stderr), \
                mock.patch.dict(os.environ, {"SUBCORTEX_CONFIG": self.env["SUBCORTEX_CONFIG"]}):
            self.assertEqual(hook_mod.main(["claude-code", "UserPromptSubmit"]), 0)
        self.assertEqual(stdout.getvalue().count("\n"), 1)
        json.loads(stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_errors_are_logged_not_printed(self):
        env = dict(self.env, SUBCORTEX_CONFIG="/nonexistent/dir/config.json")
        self.assert_silent_zero(["-m", "subcortex.hook", "kimi-code", "PreCompact"],
                                json.dumps({"session_id": "x", "hook_event_name": "PreCompact"}), env)


if __name__ == "__main__":
    unittest.main()
