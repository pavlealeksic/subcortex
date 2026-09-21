import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pathsetup  # noqa: F401

from subcortex.adapters import claude_code as adapter
from subcortex.installers import claude_code as installer


def daemon_response(verdict):
    """The real daemon wraps verdicts in {success, verdict}."""
    return {"success": True, "verdict": verdict}


@contextlib.contextmanager
def stub_post(response=None, calls=None):
    """Monkeypatch the adapter's HTTP layer; no daemon, no network."""
    def fake_post(path, payload, cfg):
        if calls is not None:
            calls.append((path, payload))
        if isinstance(response, Exception):
            raise response
        return response
    with mock.patch.object(adapter, "_post", fake_post):
        yield


@contextlib.contextmanager
def temp_home():
    old = os.environ.get("HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HOME"] = tmp
        try:
            yield Path(tmp)
        finally:
            if old is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old


BIG_OUTPUT = "all tests passed\n" + ("ok\n" * 4000)  # ~12k chars, no error markers


def bash_payload(output):
    return {
        "session_id": "sess-1",
        "transcript_path": "/nonexistent/transcript.jsonl",
        "cwd": "/tmp",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "make test"},
        "tool_response": {
            "stdout": output,
            "stderr": "",
            "interrupted": False,
            "isImage": False,
        },
        "tool_use_id": "toolu_1",
    }


class TestUserPromptSubmit(unittest.TestCase):
    PAYLOAD = {
        "session_id": "sess-1",
        "transcript_path": "/nonexistent.jsonl",
        "cwd": "/tmp",
        "permission_mode": "default",
        "hook_event_name": "UserPromptSubmit",
        "prompt": "what is 2+2?",
    }

    def test_simple_prompt_emits_additional_context(self):
        with stub_post(daemon_response({"label": "simple", "confidence": 0.95})):
            out = adapter.handle("UserPromptSubmit", self.PAYLOAD)
        self.assertIsNotNone(out)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "UserPromptSubmit")
        self.assertIn("simple", hso["additionalContext"])
        self.assertIn("0.95", hso["additionalContext"])

    def test_complex_prompt_no_output(self):
        with stub_post(daemon_response({"label": "complex", "confidence": 0.9})):
            self.assertIsNone(adapter.handle("UserPromptSubmit", self.PAYLOAD))

    def test_simple_below_threshold_no_output(self):
        with stub_post(daemon_response({"label": "simple", "confidence": 0.5})):
            self.assertIsNone(adapter.handle("UserPromptSubmit", self.PAYLOAD))

    def test_daemon_error_envelope_no_output(self):
        with stub_post({"success": False, "error": "verdict failed"}):
            self.assertIsNone(adapter.handle("UserPromptSubmit", self.PAYLOAD))

    def test_missing_prompt_no_output(self):
        with stub_post(daemon_response({"label": "simple", "confidence": 0.99})):
            self.assertIsNone(adapter.handle("UserPromptSubmit", {"session_id": "s"}))


class TestPostToolUse(unittest.TestCase):
    def test_big_disposable_output_is_truncated(self):
        with stub_post(daemon_response({"needed": False, "p_needed": 0.1})):
            out = adapter.handle("PostToolUse", bash_payload(BIG_OUTPUT))
        self.assertIsNotNone(out)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PostToolUse")
        updated = hso["updatedToolOutput"]
        # Bash schema preserved
        self.assertEqual(set(updated), {"stdout", "stderr", "interrupted", "isImage"})
        self.assertFalse(updated["interrupted"])
        stdout = updated["stdout"]
        self.assertIn("[subcortex: truncated", stdout)
        self.assertIn("re-run the command", stdout)
        self.assertLess(len(stdout), len(BIG_OUTPUT))
        self.assertTrue(stdout.startswith(BIG_OUTPUT[:100]))  # head kept
        self.assertTrue(stdout.endswith(BIG_OUTPUT[-100:]))   # tail kept

    def test_needed_output_untouched(self):
        with stub_post(daemon_response({"needed": True, "p_needed": 0.9})):
            self.assertIsNone(adapter.handle("PostToolUse", bash_payload(BIG_OUTPUT)))

    def test_traceback_output_untouched(self):
        payload = bash_payload(BIG_OUTPUT + "\nTraceback (most recent call last):\n boom")
        calls = []
        with stub_post(daemon_response({"needed": False, "p_needed": 0.0}), calls=calls):
            self.assertIsNone(adapter.handle("PostToolUse", payload))
        self.assertEqual(calls, [])  # never even judged

    def test_interrupted_untouched(self):
        payload = bash_payload(BIG_OUTPUT)
        payload["tool_response"]["interrupted"] = True
        with stub_post(daemon_response({"needed": False, "p_needed": 0.0})):
            self.assertIsNone(adapter.handle("PostToolUse", payload))

    def test_short_output_untouched(self):
        calls = []
        with stub_post(daemon_response({"needed": False, "p_needed": 0.0}), calls=calls):
            self.assertIsNone(adapter.handle("PostToolUse", bash_payload("small")))
        self.assertEqual(calls, [])

    def test_string_tool_response_shape_preserved(self):
        payload = bash_payload(BIG_OUTPUT)
        payload["tool_response"] = BIG_OUTPUT
        with stub_post(daemon_response({"needed": False, "p_needed": 0.1})):
            out = adapter.handle("PostToolUse", payload)
        updated = out["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIsInstance(updated, str)
        self.assertIn("[subcortex: truncated", updated)


class TestPreCompactSessionStart(unittest.TestCase):
    def _write_transcript(self, home: Path) -> str:
        transcript = home / "transcript.jsonl"
        lines = [
            {"type": "user", "message": {"role": "user", "content": "first question"}},
            {"type": "assistant", "message": {"role": "assistant",
                                              "content": [{"type": "text", "text": "first answer"}]}},
            {"type": "system", "content": "noise"},
            {"type": "user", "message": {"role": "user", "content": "second question"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "second answer"}},
        ]
        transcript.write_text("\n".join(json.dumps(l) for l in lines))
        return str(transcript)

    def test_precompact_writes_snapshot_and_sessionstart_reinjects(self):
        with temp_home() as home:
            transcript = self._write_transcript(home)
            payload = {
                "session_id": "sess-42",
                "transcript_path": transcript,
                "cwd": str(home),
                "hook_event_name": "PreCompact",
                "trigger": "auto",
                "custom_instructions": None,
            }
            self.assertIsNone(adapter.handle("PreCompact", payload))
            snapshot_path = home / ".local/share/subcortex/compact/sess-42.json"
            self.assertTrue(snapshot_path.is_file())
            snapshot = json.loads(snapshot_path.read_text())
            self.assertEqual(snapshot["trigger"], "auto")
            self.assertEqual(len(snapshot["messages"]), 4)  # system line skipped
            self.assertEqual(snapshot["messages"][0]["text"], "first question")

            start_payload = {
                "session_id": "sess-42",
                "transcript_path": transcript,
                "cwd": str(home),
                "hook_event_name": "SessionStart",
                "source": "compact",
            }
            out = adapter.handle("SessionStart", start_payload)
            self.assertIsNotNone(out)
            hso = out["hookSpecificOutput"]
            self.assertEqual(hso["hookEventName"], "SessionStart")
            self.assertIn("Pre-compaction context", hso["additionalContext"])
            self.assertIn("second answer", hso["additionalContext"])
            self.assertFalse(snapshot_path.exists())  # consumed

            # second start: nothing left to inject
            self.assertIsNone(adapter.handle("SessionStart", start_payload))

    def test_precompact_tolerates_missing_transcript(self):
        with temp_home() as home:
            payload = {
                "session_id": "sess-x",
                "transcript_path": str(home / "nope.jsonl"),
                "hook_event_name": "PreCompact",
                "trigger": "manual",
                "custom_instructions": None,
            }
            self.assertIsNone(adapter.handle("PreCompact", payload))
            snapshot = json.loads(
                (home / ".local/share/subcortex/compact/sess-x.json").read_text())
            self.assertEqual(snapshot["messages"], [])


class TestFailOpen(unittest.TestCase):
    EVENTS = {
        "UserPromptSubmit": TestUserPromptSubmit.PAYLOAD,
        "PostToolUse": bash_payload(BIG_OUTPUT),
        "PreCompact": {"session_id": "s", "trigger": "auto",
                       "transcript_path": "/nope.jsonl", "custom_instructions": None},
        "SessionStart": {"session_id": "s", "hook_event_name": "SessionStart"},
        "SomeUnknownEvent": {"session_id": "s"},
    }

    def test_daemon_down_returns_none_for_every_event(self):
        with temp_home(), stub_post(ConnectionRefusedError("down")):
            for event, payload in self.EVENTS.items():
                self.assertIsNone(adapter.handle(event, payload), event)

    def test_handle_never_raises_on_garbage(self):
        with temp_home():
            self.assertIsNone(adapter.handle("UserPromptSubmit", "not a dict"))
            self.assertIsNone(adapter.handle(None, None))

    def test_main_exit_0_no_output_when_daemon_down(self):
        stdin = io.StringIO(json.dumps(TestUserPromptSubmit.PAYLOAD))
        stdout = io.StringIO()
        with temp_home(), stub_post(None), \
                mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", stdout):
            rc = adapter.main(["UserPromptSubmit"])
        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue(), "")

    def test_main_writes_response_json(self):
        stdin = io.StringIO(json.dumps(TestUserPromptSubmit.PAYLOAD))
        stdout = io.StringIO()
        verdict = daemon_response({"label": "simple", "confidence": 0.99})
        with temp_home(), stub_post(verdict), \
                mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", stdout):
            rc = adapter.main(["UserPromptSubmit"])
        self.assertEqual(rc, 0)
        out = json.loads(stdout.getvalue())
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")

    def test_main_tolerates_empty_stdin(self):
        stdout = io.StringIO()
        with temp_home(), mock.patch("sys.stdin", io.StringIO("")), \
                mock.patch("sys.stdout", stdout):
            rc = adapter.main(["UserPromptSubmit"])
        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue(), "")


class TestInstaller(unittest.TestCase):
    def test_install_merge_idempotent_uninstall(self):
        with temp_home() as home:
            settings_path = home / ".claude/settings.json"
            settings_path.parent.mkdir(parents=True)
            preexisting = {
                "model": "claude-opus-4-1",
                "hooks": {
                    "PostToolUse": [
                        {"matcher": "Bash",
                         "hooks": [{"type": "command", "command": "my-own-hook"}]},
                    ],
                },
            }
            settings_path.write_text(json.dumps(preexisting))

            result = installer.install()
            self.assertTrue((home / ".claude/settings.json.subcortex.bak").is_file())
            self.assertEqual(set(result["added"]), set(installer.HOOKS))

            settings = json.loads(settings_path.read_text())
            # existing content preserved
            self.assertEqual(settings["model"], "claude-opus-4-1")
            commands = [
                h["command"]
                for group in settings["hooks"]["PostToolUse"]
                for h in group["hooks"]
            ]
            self.assertIn("my-own-hook", commands)
            subcortex_cmds = [c for c in commands if "subcortex" in c]
            self.assertEqual(len(subcortex_cmds), 1)
            self.assertIn("hook claude PostToolUse", subcortex_cmds[0])
            for event, matcher in installer.HOOKS.items():
                self.assertIn(event, settings["hooks"])
                groups = settings["hooks"][event]
                sub_groups = [
                    g for g in groups
                    if any("subcortex" in h.get("command", "") for h in g["hooks"])
                ]
                self.assertEqual(len(sub_groups), 1)
                self.assertEqual(sub_groups[0].get("matcher"), matcher)
                sub_hooks = [
                    h for h in sub_groups[0]["hooks"] if "subcortex" in h.get("command", "")
                ]
                self.assertEqual(sub_hooks[0]["timeout"], 10)
                self.assertEqual(sub_hooks[0]["type"], "command")

            # idempotent: second install adds nothing
            before = settings_path.read_text()
            result2 = installer.install()
            self.assertEqual(result2["added"], [])
            self.assertEqual(settings_path.read_text(), before)

            st = installer.status()
            self.assertTrue(st["installed"])
            self.assertTrue(all(st["events"].values()))

            # uninstall removes exactly subcortex entries
            result3 = installer.uninstall()
            self.assertEqual(set(result3["removed"]), set(installer.HOOKS))
            settings = json.loads(settings_path.read_text())
            self.assertEqual(settings["model"], "claude-opus-4-1")
            self.assertEqual(settings["hooks"], preexisting["hooks"])
            st = installer.status()
            self.assertFalse(st["installed"])
            self.assertEqual(st["entries"], 0)

    def test_install_creates_settings_from_scratch(self):
        with temp_home() as home:
            result = installer.install()
            settings = json.loads((home / ".claude/settings.json").read_text())
            self.assertEqual(set(settings["hooks"]), set(installer.HOOKS))
            self.assertEqual(set(result["added"]), set(installer.HOOKS))

    def test_uninstall_without_settings_is_noop(self):
        with temp_home():
            result = installer.uninstall()
            self.assertEqual(result["removed"], [])


if __name__ == "__main__":
    unittest.main()
