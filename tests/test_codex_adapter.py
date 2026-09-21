import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pathsetup  # noqa: F401

from subcortex.adapters import codex
from subcortex.installers import codex as codex_installer

LONG_OUTPUT = "x" * 7000


def post_tool_payload(output, **overrides):
    payload = {
        "session_id": "s1",
        "cwd": "/tmp",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "make test"},
        "tool_response": output,
        "tool_use_id": "t1",
    }
    payload.update(overrides)
    return payload


class TestUserPromptSubmit(unittest.TestCase):
    def _handle(self, verdict, prompt="what is 2+2?"):
        with mock.patch.object(codex, "_post", return_value=verdict) as post:
            result = codex.handle("UserPromptSubmit", {"session_id": "s1", "prompt": prompt})
        return result, post

    def test_simple_prompt_gets_hint(self):
        result, post = self._handle({"label": "simple", "confidence": 0.93})
        self.assertIsNotNone(result)
        ctx = result["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "UserPromptSubmit")
        self.assertIn("simple", ctx["additionalContext"])
        self.assertIn("0.93", ctx["additionalContext"])
        self.assertIn("direct, minimal path", ctx["additionalContext"])
        self.assertEqual(post.call_args[0][0], "/verdict/prompt")
        self.assertEqual(post.call_args[0][1], {"prompt": "what is 2+2?"})

    def test_simple_at_threshold_gets_hint(self):
        result, _ = self._handle({"label": "simple", "confidence": 0.8})
        self.assertIsNotNone(result)

    def test_simple_below_threshold_no_hint(self):
        result, _ = self._handle({"label": "simple", "confidence": 0.7})
        self.assertIsNone(result)

    def test_complex_prompt_no_hint(self):
        result, _ = self._handle({"label": "complex", "confidence": 0.9})
        self.assertIsNone(result)

    def test_empty_prompt_skips_daemon(self):
        result, post = self._handle(None, prompt="   ")
        self.assertIsNone(result)
        post.assert_not_called()

    def test_daemon_failure_is_silent(self):
        result, _ = self._handle(None)
        self.assertIsNone(result)


class TestPostToolUse(unittest.TestCase):
    def _handle(self, verdict, payload):
        with mock.patch.object(codex, "_post", return_value=verdict) as post:
            result = codex.handle("PostToolUse", payload)
        return result, post

    def test_disposable_output_is_replaced(self):
        output = "H" * 1000 + "M" * 5000 + "T" * 500  # 6500 chars, head/tail distinct
        result, post = self._handle({"needed": False, "p_needed": 0.1},
                                    post_tool_payload(output))
        self.assertIsNotNone(result)
        self.assertEqual(result["decision"], "block")
        reason = result["reason"]
        self.assertTrue(reason.startswith("H" * 1000))
        self.assertTrue(reason.endswith("T" * 500))
        self.assertIn("truncated 5000 chars", reason)
        self.assertIn("re-run the command", reason)
        self.assertEqual(len(reason), 6500 - 5000 + len(
            "\n\n[subcortex: truncated 5000 chars of low-value output; "
            "re-run the command if you need the rest]\n\n"))
        self.assertEqual(post.call_args[0][0], "/verdict/output")
        self.assertEqual(post.call_args[0][1]["output"], output)
        self.assertEqual(post.call_args[0][1]["context"], "Bash")

    def test_needed_output_passes(self):
        result, _ = self._handle({"needed": True, "p_needed": 0.9},
                                 post_tool_payload(LONG_OUTPUT))
        self.assertIsNone(result)

    def test_threshold_edge_passes(self):
        result, _ = self._handle({"needed": False, "p_needed": 0.3},
                                 post_tool_payload(LONG_OUTPUT))
        self.assertIsNone(result)

    def test_short_output_skips_daemon(self):
        result, post = self._handle(None, post_tool_payload("tiny"))
        self.assertIsNone(result)
        post.assert_not_called()

    def test_traceback_passes_untouched(self):
        output = "y" * 6000 + "\nTraceback (most recent call last):\n..."
        result, post = self._handle(None, post_tool_payload(output))
        self.assertIsNone(result)
        post.assert_not_called()

    def test_failed_exit_code_passes_untouched(self):
        response = {"output": LONG_OUTPUT, "exit_code": 1}
        result, post = self._handle(None, post_tool_payload(response))
        self.assertIsNone(result)
        post.assert_not_called()

    def test_dict_response_output_field_is_judged(self):
        response = {"output": LONG_OUTPUT, "exit_code": 0}
        result, post = self._handle({"needed": False, "p_needed": 0.2},
                                    post_tool_payload(response))
        self.assertIsNotNone(result)
        self.assertEqual(post.call_args[0][1]["output"], LONG_OUTPUT)

    def test_daemon_failure_is_silent(self):
        result, _ = self._handle(None, post_tool_payload(LONG_OUTPUT))
        self.assertIsNone(result)


class TestFailOpen(unittest.TestCase):
    def test_handle_never_raises(self):
        with mock.patch.object(codex, "load_config", side_effect=RuntimeError("boom")):
            self.assertIsNone(codex.handle("UserPromptSubmit", {"prompt": "hi"}))
        self.assertIsNone(codex.handle("Nonsense", {"prompt": "hi"}))
        self.assertIsNone(codex.handle("UserPromptSubmit", None))

    def test_post_raises_on_daemon_down(self):
        cfg = {"port": 1}  # nothing listening there
        self.assertIsNone(codex._post("/verdict/prompt", {"prompt": "x"}, cfg))

    def test_post_unwraps_daemon_envelope_and_bare_verdict(self):
        def fake_urlopen(req, timeout=0):
            self.assertEqual(timeout, codex.TIMEOUT_S)
            body = json.dumps({"success": True, "verdict": {"label": "simple"}}).encode()
            return io.BytesIO(body)
        with mock.patch.object(codex.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(codex._post("/x", {}, {"port": 7707}), {"label": "simple"})

            def bare(req, timeout=0):
                return io.BytesIO(json.dumps({"label": "simple"}).encode())
            with mock.patch.object(codex.urllib.request, "urlopen", bare):
                self.assertEqual(codex._post("/x", {}, {"port": 7707}), {"label": "simple"})

            def failed(req, timeout=0):
                return io.BytesIO(json.dumps({"success": False, "error": "x"}).encode())
            with mock.patch.object(codex.urllib.request, "urlopen", failed):
                self.assertIsNone(codex._post("/x", {}, {"port": 7707}))

    def _run_main(self, argv, stdin_text):
        stdout = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin_text)), \
                mock.patch("sys.stdout", stdout):
            code = codex.main(argv)
        return code, stdout.getvalue()

    def test_main_writes_response_json(self):
        verdict = {"label": "simple", "confidence": 0.95}
        with mock.patch.object(codex, "_post", return_value=verdict):
            code, out = self._run_main(["UserPromptSubmit"],
                                       json.dumps({"prompt": "hi"}))
        self.assertEqual(code, 0)
        response = json.loads(out)
        self.assertEqual(response["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")

    def test_main_no_verdict_no_output(self):
        with mock.patch.object(codex, "_post", return_value=None):
            code, out = self._run_main(["UserPromptSubmit"], json.dumps({"prompt": "hi"}))
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_main_garbage_stdin_still_zero(self):
        code, out = self._run_main(["UserPromptSubmit"], "not json at all")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_main_exception_still_zero_and_silent(self):
        with mock.patch.object(codex, "handle", side_effect=RuntimeError("boom")):
            code, out = self._run_main(["UserPromptSubmit"], json.dumps({"prompt": "hi"}))
        self.assertEqual(code, 0)
        self.assertEqual(out, "")


class TestCompactCycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"HOME": self.tmp.name,
                                                "SUBCORTEX_DATA_DIR": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.transcript = Path(self.tmp.name) / "transcript.jsonl"

    def _write_transcript(self, lines):
        with open(self.transcript, "w") as fh:
            for line in lines:
                fh.write(line if line.endswith("\n") else line + "\n")

    def test_pre_compact_writes_last_five_nonempty(self):
        entries = [
            json.dumps({"message": {"content": f"msg {i}"}}) for i in range(8)
        ]
        entries.append(json.dumps({"message": {"content": "   "}}))  # empty, skipped
        entries.append("garbage line")  # unparseable, skipped
        entries.append(json.dumps({"content": [{"type": "text", "text": "msg 8"}]}))
        self._write_transcript(entries)
        result = codex.handle("PreCompact", {
            "session_id": "sess 1/2", "trigger": "manual",
            "transcript_path": str(self.transcript),
        })
        self.assertIsNone(result)  # never vetoes
        path = codex._snapshot_path("sess 1/2")
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, Path(self.tmp.name) / ".local/share/subcortex/compact")
        self.assertNotIn("/", path.name)
        data = json.loads(path.read_text())
        self.assertEqual(data["session_id"], "sess 1/2")
        self.assertEqual(data["trigger"], "manual")
        self.assertEqual(data["messages"],
                         ["msg 4", "msg 5", "msg 6", "msg 7", "msg 8"])

    def test_pre_compact_tolerates_missing_transcript(self):
        result = codex.handle("PreCompact", {
            "session_id": "s9", "transcript_path": str(self.transcript)})
        self.assertIsNone(result)
        data = json.loads(codex._snapshot_path("s9").read_text())
        self.assertEqual(data["messages"], [])

    def test_session_start_reinjects_and_deletes(self):
        codex.handle("PreCompact", {"session_id": "s1",
                                    "transcript_path": str(self.transcript)})
        self._write_transcript([json.dumps({"text": "remember this"})])
        codex.handle("PreCompact", {"session_id": "s1",
                                    "transcript_path": str(self.transcript)})
        result = codex.handle("SessionStart", {"session_id": "s1", "source": "compact"})
        self.assertIsNotNone(result)
        ctx = result["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "SessionStart")
        self.assertIn("remember this", ctx["additionalContext"])
        self.assertFalse(codex._snapshot_path("s1").exists())  # consumed
        # second compact-start finds nothing
        self.assertIsNone(codex.handle("SessionStart", {"session_id": "s1",
                                                        "source": "compact"}))

    def test_session_start_non_compact_sources_ignored(self):
        for source in ("startup", "resume", "clear", ""):
            self.assertIsNone(codex.handle("SessionStart",
                                           {"session_id": "s1", "source": source}))


class TestInstaller(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"HOME": self.tmp.name,
                                                "CODEX_HOME": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = Path(self.tmp.name) / ".codex" / "config.toml"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(
            'model = "gpt-5-codex"\n'
            'approval_policy = "on-request"\n'
            '\n'
            '[sandbox_workspace_write]\n'
            'network_access = true\n'
        )
        self.foreign = self.config.read_text()

    def test_install_uninstall_roundtrip(self):
        self.assertTrue(codex_installer.install())
        text = self.config.read_text()
        parsed = codex_installer.tomllib.loads(text)
        # foreign content preserved
        self.assertTrue(text.startswith(self.foreign))
        self.assertEqual(parsed["model"], "gpt-5-codex")
        self.assertEqual(parsed["sandbox_workspace_write"]["network_access"], True)
        # our hooks landed
        hooks = parsed["hooks"]
        up_hooks = hooks["UserPromptSubmit"]["hooks"]
        self.assertEqual(up_hooks[0]["type"], "command")
        self.assertIn("hook codex UserPromptSubmit", up_hooks[0]["command"])
        self.assertEqual(up_hooks[0]["timeout"], 10)
        self.assertEqual(hooks["PostToolUse"][0]["matcher"], "Bash")
        self.assertIn("hook codex PostToolUse",
                      hooks["PostToolUse"][0]["hooks"][0]["command"])
        self.assertIn("hook codex PreCompact",
                      hooks["PreCompact"]["hooks"][0]["command"])
        self.assertEqual(hooks["SessionStart"][0]["matcher"], "compact")
        # markers + backup
        self.assertIn("# >>> subcortex", text)
        self.assertIn("# <<< subcortex", text)
        backup = Path(str(self.config) + ".subcortex.bak")
        self.assertEqual(backup.read_text(), self.foreign)
        # status reflects installation
        st = codex_installer.status()
        self.assertTrue(st["installed"])
        self.assertIn("/hooks", st["note"])

        # idempotent: second install changes nothing
        self.assertTrue(codex_installer.install())
        self.assertEqual(self.config.read_text(), text)

        # uninstall removes exactly our block, foreign content byte-identical
        self.assertTrue(codex_installer.uninstall())
        cleaned = self.config.read_text()
        self.assertNotIn("subcortex", cleaned)
        self.assertEqual(codex_installer.tomllib.loads(cleaned)["model"], "gpt-5-codex")
        self.assertEqual(cleaned, self.foreign)
        self.assertFalse(codex_installer.status()["installed"])

        # uninstall again: no-op, still fine
        self.assertTrue(codex_installer.uninstall())
        self.assertEqual(self.config.read_text(), self.foreign)

    def test_install_into_missing_config(self):
        self.config.unlink()
        self.assertTrue(codex_installer.install())
        parsed = codex_installer.tomllib.loads(self.config.read_text())
        self.assertIn("UserPromptSubmit", parsed["hooks"])
        self.assertTrue(codex_installer.uninstall())
        self.assertEqual(self.config.read_text(), "")

    def test_install_refuses_invalid_toml(self):
        self.config.write_text("this is = = not toml\n")
        self.assertFalse(codex_installer.install())
        self.assertEqual(self.config.read_text(), "this is = = not toml\n")

    def test_status_when_absent(self):
        self.config.unlink()
        st = codex_installer.status()
        self.assertFalse(st["installed"])
        self.assertFalse(st["config_exists"])
        self.assertEqual(st["commands"], {})


if __name__ == "__main__":
    unittest.main()
