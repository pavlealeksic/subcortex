"""Adapter contract tests: every TUI's recorded payload shapes in, the exact
response shape its spec requires out — and never a blocking response."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pathsetup  # noqa: F401

from subcortex import adapters, hook, installers, policy
from subcortex.adapters.base import BLOCKING_VALUES, KINDS, _walk
from subcortex.config import DEFAULT_CONFIG

BIG = "compiling module\n" * 800
TRANSCRIPT = [
    {"type": "user", "message": {"role": "user", "content": "rename getUser to fetchUser"}},
    {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Renamed it."}]}},
]


class FakeClient:
    def __init__(self, simple=True, disposable=True):
        self.simple, self.disposable = simple, disposable
        self.calls = []

    def classify(self, prompt):
        self.calls.append(("classify", prompt))
        return {"label": "simple" if self.simple else "complex", "confidence": 0.97}

    def judge(self, output, context):
        self.calls.append(("judge", context))
        return {"needed": not self.disposable, "p_needed": 0.02 if self.disposable else 0.9}


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.transcript = Path(self.tmp.name, "t.jsonl")
        self.transcript.write_text("\n".join(json.dumps(e) for e in TRANSCRIPT))
        self.env = mock.patch.dict(os.environ, {"SUBCORTEX_DATA_DIR": self.tmp.name})
        self.env.start()
        for var in ("CURSOR_VERSION", "DROID_PROJECT_DIR", "FACTORY_PROJECT_DIR"):
            os.environ.pop(var, None)
        self.cfg = copy.deepcopy(DEFAULT_CONFIG)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_hook(self, tui, event, payload, client=None):
        out = hook.run(tui, event, json.dumps(payload), self.cfg, client or FakeClient())
        return json.loads(out) if out and out.lstrip().startswith("{") else out

    def sample(self, tui, event):
        from subcortex.installers.base import _fill
        return _fill(installers.get_installer(tui).sample_payload(event), str(self.transcript))


class TestEveryAdapter(AdapterCase):
    def test_registry_is_consistent(self):
        for name in adapters.names():
            adapter = adapters.get_adapter(name)
            self.assertEqual(adapter.name, name)
            self.assertTrue(set(adapter.events.values()) <= set(KINDS), name)
            self.assertIsNotNone(installers.get_installer(name), f"{name} has no installer")

    def test_installers_register_exactly_the_adapter_events(self):
        for name in adapters.names():
            events = installers.get_installer(name).hook_events()
            self.assertEqual(set(events), set(adapters.get_adapter(name).events), name)

    def test_sample_payloads_never_produce_blocking_responses(self):
        for name in adapters.names():
            for event in installers.get_installer(name).hook_events():
                out = hook.run(name, event, json.dumps(self.sample(name, event)), self.cfg, FakeClient())
                if not out or not out.startswith("{"):
                    continue
                kind = adapters.get_adapter(name).resolve(event)[1]
                allowed = adapters.get_adapter(name).allowed_blocking
                for key, value in _walk(json.loads(out)):
                    for bad_key, bad_value in BLOCKING_VALUES:
                        if key == bad_key and value == bad_value:
                            self.assertIn((kind, key, value), allowed, f"{name} {event}: {out[:200]}")

    def test_garbage_payloads_never_raise_or_respond(self):
        garbage = ["", "null", "[]", '"str"', "{not json", json.dumps({"prompt": 5}),
                   json.dumps({"tool_response": [1, 2]}), json.dumps({"session_id": "../../x"})]
        for name in adapters.names():
            for event in list(adapters.get_adapter(name).events) + ["NoSuchEvent"]:
                for raw in garbage:
                    out = hook.run(name, event, raw, self.cfg, FakeClient())
                    self.assertIn(out, (None, ""), f"{name} {event} {raw!r} -> {out!r}")

    def test_unknown_tui_and_event(self):
        self.assertIsNone(hook.run("no-such-tui", "UserPromptSubmit", "{}", self.cfg, FakeClient()))
        self.assertIsNone(hook.run("claude-code", "NoSuchEvent", "{}", self.cfg, FakeClient()))

    def test_aliases(self):
        self.assertEqual(adapters.canonical_name("claude"), "claude-code")  # the 0.1.0 spelling
        self.assertEqual(adapters.canonical_name("gemini"), "gemini-cli")
        self.assertEqual(installers.canonical_name("cursor-agent"), "cursor")


class TestClaudeCode(AdapterCase):
    def test_prompt_hint(self):
        out = self.run_hook("claude-code", "UserPromptSubmit", self.sample("claude-code", "UserPromptSubmit"))
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("simple", out["hookSpecificOutput"]["additionalContext"])

    def test_prompt_id_is_claude_code_not_devin(self):
        payload = dict(self.sample("claude-code", "UserPromptSubmit"), prompt_id="550e8400")
        self.assertIsNotNone(self.run_hook("claude-code", "UserPromptSubmit", payload))

    def test_tool_output_replacement_keeps_the_tool_shape(self):
        payload = self.sample("claude-code", "PostToolUse")
        payload["tool_response"]["noOutputExpected"] = False
        out = self.run_hook("claude-code", "PostToolUse", payload)
        updated = out["hookSpecificOutput"]["updatedToolOutput"]
        self.assertEqual(set(updated), {"stdout", "stderr", "interrupted", "isImage", "noOutputExpected"})
        self.assertIn("[subcortex: truncated", updated["stdout"])

    def test_no_replacement_when_claude_already_spilled_or_failed(self):
        for extra in ({"persistedOutputPath": "/tmp/x"}, {"interrupted": True}, {"isImage": True}):
            payload = self.sample("claude-code", "PostToolUse")
            payload["tool_response"].update(extra)
            self.assertIsNone(self.run_hook("claude-code", "PostToolUse", payload), extra)
        payload = self.sample("claude-code", "PostToolUse")
        payload["tool_response"]["stdout"] = BIG + "Traceback (most recent call last):\n" + BIG
        self.assertIsNone(self.run_hook("claude-code", "PostToolUse", payload))

    def test_compaction_round_trip(self):
        self.assertIsNone(self.run_hook("claude-code", "PreCompact", self.sample("claude-code", "PreCompact")))
        out = self.run_hook("claude-code", "SessionStart", self.sample("claude-code", "SessionStart"))
        self.assertIn("user: rename getUser to fetchUser", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.run_hook("claude-code", "SessionStart", self.sample("claude-code", "SessionStart")))

    def test_session_start_other_sources_ignored(self):
        self.run_hook("claude-code", "PreCompact", self.sample("claude-code", "PreCompact"))
        payload = dict(self.sample("claude-code", "SessionStart"), source="startup")
        self.assertIsNone(self.run_hook("claude-code", "SessionStart", payload))

    def test_foreign_hosts_running_claude_hooks_get_silence(self):
        base = self.sample("claude-code", "UserPromptSubmit")
        foreign = [
            dict(base, cursor_version="2026.09.18-9a7762b"),          # Cursor CLI
            dict(base, hookEventName="user_prompt_submit"),           # Grok Build
            {k: v for k, v in base.items() if k != "transcript_path"},  # Devin CLI
            dict(base, transcript_path=""),                           # Continue cn
        ]
        for payload in foreign:
            self.assertIsNone(self.run_hook("claude-code", "UserPromptSubmit", payload), payload)
        with mock.patch.dict(os.environ, {"DROID_PROJECT_DIR": "/tmp"}):
            self.assertIsNone(self.run_hook("claude-code", "UserPromptSubmit", base))

    def test_complex_prompt_and_daemon_down(self):
        payload = self.sample("claude-code", "UserPromptSubmit")
        self.assertIsNone(self.run_hook("claude-code", "UserPromptSubmit", payload, FakeClient(simple=False)))

        class Down:
            def classify(self, prompt):
                return None

            def judge(self, output, context):
                return None
        self.assertIsNone(self.run_hook("claude-code", "UserPromptSubmit", payload, Down()))


class TestClaudeClones(AdapterCase):
    def test_qoder_and_codebuddy_replace_with_a_string(self):
        for tui in ("qoder", "codebuddy"):
            out = self.run_hook(tui, "PostToolUse", self.sample(tui, "PostToolUse"))
            self.assertIsInstance(out["hookSpecificOutput"]["updatedToolOutput"], str, tui)

    def test_droid_restores_by_previous_session_id(self):
        self.run_hook("droid", "PreCompact", self.sample("droid", "PreCompact"))
        payload = self.sample("droid", "SessionStart")
        self.assertNotEqual(payload["session_id"], payload["previous_session_id"])
        out = self.run_hook("droid", "SessionStart", payload)
        self.assertIn("rename getUser", out["hookSpecificOutput"]["additionalContext"])

    def test_droid_junie_devin_register_no_tool_output(self):
        for tui in ("droid", "junie", "devin"):
            self.assertNotIn("PostToolUse", adapters.get_adapter(tui).events, tui)


class TestCodexEngine(AdapterCase):
    def test_replacement_is_continue_false_with_reason(self):
        for tui in ("codex", "open-interpreter"):
            out = self.run_hook(tui, "PostToolUse", self.sample(tui, "PostToolUse"))
            self.assertEqual(set(out), {"continue", "stopReason", "reason"}, tui)
            self.assertIs(out["continue"], False)
            self.assertIn("[subcortex: truncated", out["reason"])

    def test_codex_truncated_output_is_left_alone(self):
        payload = self.sample("codex", "PostToolUse")
        payload["tool_response"] = "Warning: truncated output (original token count: 90000)\n" + BIG
        self.assertIsNone(self.run_hook("codex", "PostToolUse", payload))

    def test_prompt_and_compaction_use_only_schema_keys(self):
        out = self.run_hook("codex", "UserPromptSubmit", self.sample("codex", "UserPromptSubmit"))
        self.assertEqual(set(out["hookSpecificOutput"]), {"hookEventName", "additionalContext"})
        self.run_hook("codex", "PreCompact", self.sample("codex", "PreCompact"))
        out = self.run_hook("codex", "SessionStart", self.sample("codex", "SessionStart"))
        self.assertEqual(set(out["hookSpecificOutput"]), {"hookEventName", "additionalContext"})

    def test_continue_false_is_not_allowed_anywhere_else(self):
        adapter = adapters.get_adapter("codex")
        self.assertIsNone(adapter.guard("prompt", {"continue": False}))
        self.assertIsNotNone(adapter.guard("tool_output", {"continue": False, "reason": "x"}))


class TestGeminiFamily(AdapterCase):
    def test_gemini_strips_hook_context_and_restores_after_manual_compress(self):
        client = FakeClient(simple=False)
        payload = dict(self.sample("gemini-cli", "BeforeAgent"),
                       prompt="<hook_context>old</hook_context>\nwhat is ls?")
        self.run_hook("gemini-cli", "BeforeAgent", payload, client)
        self.assertEqual(client.calls[0], ("classify", "what is ls?"))
        self.run_hook("gemini-cli", "PreCompress", self.sample("gemini-cli", "PreCompress"))
        out = self.run_hook("gemini-cli", "BeforeAgent", self.sample("gemini-cli", "BeforeAgent"), client)
        self.assertIn("rename getUser", out["hookSpecificOutput"]["additionalContext"])

    def test_gemini_auto_precompress_does_not_mark_compaction(self):
        payload = dict(self.sample("gemini-cli", "PreCompress"), trigger="auto")
        self.run_hook("gemini-cli", "PreCompress", payload)
        self.assertIsNone(self.run_hook("gemini-cli", "BeforeAgent", self.sample("gemini-cli", "BeforeAgent"),
                                        FakeClient(simple=False)))

    def test_qwen_classifies_only_real_user_prompts(self):
        payload = self.sample("qwen-code", "UserPromptSubmit")
        self.assertIsNotNone(self.run_hook("qwen-code", "UserPromptSubmit", payload))
        continuation = {k: v for k, v in payload.items() if k != "submitted_prompt"}
        client = FakeClient()
        self.assertIsNone(self.run_hook("qwen-code", "UserPromptSubmit", continuation, client))
        self.assertEqual(client.calls, [])


class TestCursorCopilot(AdapterCase):
    def test_cursor_prompt_and_restore_on_next_prompt(self):
        out = self.run_hook("cursor", "beforeSubmitPrompt", self.sample("cursor", "beforeSubmitPrompt"))
        self.assertEqual(set(out), {"additional_context"})
        self.run_hook("cursor", "preCompact", self.sample("cursor", "preCompact"))
        out = self.run_hook("cursor", "beforeSubmitPrompt", self.sample("cursor", "beforeSubmitPrompt"),
                            FakeClient(simple=False))
        self.assertIn("rename getUser", out["additional_context"])
        self.assertLessEqual(len(out["additional_context"]), 9000)

    def test_copilot_modified_result_keeps_success(self):
        out = self.run_hook("copilot", "postToolUse", self.sample("copilot", "postToolUse"))
        self.assertEqual(out["modifiedResult"]["resultType"], "success")
        self.assertIn("[subcortex: truncated", out["modifiedResult"]["textResultForLlm"])

    def test_copilot_failed_results_untouched_and_string_args(self):
        payload = self.sample("copilot", "postToolUse")
        payload["toolResult"]["resultType"] = "failure"
        self.assertIsNone(self.run_hook("copilot", "postToolUse", payload))
        payload = self.sample("copilot", "postToolUse")
        payload["toolArgs"] = json.dumps({"command": "make"})
        client = FakeClient()
        self.run_hook("copilot", "postToolUse", payload, client)
        self.assertEqual(client.calls[0], ("judge", 'bash: {"command": "make"}'))


class TestKimi(AdapterCase):
    def test_prompt_parts_and_message_shape(self):
        client = FakeClient()
        out = self.run_hook("kimi-code", "UserPromptSubmit", self.sample("kimi-code", "UserPromptSubmit"), client)
        self.assertEqual(set(out), {"message"})
        self.assertEqual(client.calls[0], ("classify", "what does ls -la do?"))

    def test_compaction_via_wire_log_and_next_prompt(self):
        home = Path(self.tmp.name, "kimi")
        session = "session_4f0c2a8e-9b1d-4c3e-8a77-2f5d0e6b1c90"
        sdir = home / "sessions" / "wd_x" / session
        (sdir / "agents" / "main").mkdir(parents=True)
        wire = [
            {"type": "metadata", "protocol_version": "1.5"},
            {"type": "context.append_message", "message": {"role": "user", "origin": {"kind": "user"},
                                                           "content": [{"type": "text", "text": "fix login"}]}},
            {"type": "context.append_message", "message": {"role": "user", "origin": {"kind": "injection"},
                                                           "content": [{"type": "text", "text": "<system-reminder>x"}]}},
            {"type": "context.append_loop_event", "event": {"type": "content.part", "stepUuid": "s1",
                                                            "part": {"type": "think", "think": "hmm"}}},
            {"type": "context.append_loop_event", "event": {"type": "content.part", "stepUuid": "s1",
                                                            "part": {"type": "text", "text": "Fixed "}}},
            {"type": "context.append_loop_event", "event": {"type": "content.part", "stepUuid": "s1",
                                                            "part": {"type": "text", "text": "it."}}},
        ]
        (sdir / "agents" / "main" / "wire.jsonl").write_text("\n".join(json.dumps(w) for w in wire))
        (home / "session_index.jsonl").write_text(json.dumps(
            {"sessionId": session, "sessionDir": str(sdir), "workDir": "/tmp"}) + "\n")
        with mock.patch.dict(os.environ, {"KIMI_CODE_HOME": str(home)}):
            self.run_hook("kimi-code", "PreCompact", self.sample("kimi-code", "PreCompact"))
            # PreCompact alone (compaction could still be skipped) restores nothing:
            self.assertIsNone(self.run_hook("kimi-code", "UserPromptSubmit",
                                            self.sample("kimi-code", "UserPromptSubmit"), FakeClient(simple=False)))
            self.run_hook("kimi-code", "PostCompact", self.sample("kimi-code", "PostCompact"))
            out = self.run_hook("kimi-code", "UserPromptSubmit", self.sample("kimi-code", "UserPromptSubmit"),
                                FakeClient(simple=False))
        self.assertIn("user: fix login", out["message"])
        self.assertIn("assistant: Fixed it.", out["message"])
        self.assertNotIn("system-reminder", out["message"])


class TestOpenHands(AdapterCase):
    def test_hint_and_condensation_detection(self):
        session = "4f1c2a9e-8b7d-4e21-9c3a-0d5e6f7a8b9c"
        conv = Path(self.tmp.name, "oh") / "conversations" / session.replace("-", "") / "events"
        conv.mkdir(parents=True)

        def event(i, body):
            (conv / f"event-{i:05d}-e{i}.json").write_text(json.dumps({"id": f"e{i}", **body}))

        event(0, {"kind": "MessageEvent", "source": "user",
                  "llm_message": {"role": "user", "content": [{"type": "text", "text": "add tests"}]}})
        event(1, {"kind": "MessageEvent", "source": "agent",
                  "llm_message": {"role": "assistant", "content": [{"type": "text", "text": "Added 3 tests."}]}})
        payload = self.sample("openhands", "UserPromptSubmit")
        complex_client = FakeClient(simple=False)
        with mock.patch.dict(os.environ, {"OPENHANDS_PERSISTENCE_DIR": str(Path(self.tmp.name, "oh"))}):
            self.assertIsNone(self.run_hook("openhands", "UserPromptSubmit", payload, complex_client))  # baseline
            event(2, {"kind": "Condensation", "forgotten_event_ids": ["e0", "e1"], "summary": "..."})
            out = self.run_hook("openhands", "UserPromptSubmit", payload, complex_client)
            self.assertIn("user: add tests", out["additionalContext"])
            self.assertIsNone(self.run_hook("openhands", "UserPromptSubmit", payload, complex_client))  # once
        out = self.run_hook("openhands", "UserPromptSubmit", payload)
        self.assertEqual(set(out), {"additionalContext"})

    def test_any_continue_key_is_dropped(self):
        adapter = adapters.get_adapter("openhands")
        self.assertIsNone(adapter.guard("prompt", {"continue": True, "additionalContext": "x"}))


class TestWaveTwo(AdapterCase):
    def test_grok_replaces_with_the_full_tagged_object(self):
        payload = self.sample("grok-build", "PostToolUse")
        payload["toolResult"]["output_for_prompt"] = "exit: 0\n" + BIG
        out = self.run_hook("grok-build", "PostToolUse", payload)
        updated = out["hookSpecificOutput"]["updatedToolOutput"]
        self.assertEqual(updated["type"], "Bash")
        self.assertEqual(updated["output"], [])
        self.assertTrue(updated["output_for_prompt"].startswith("exit: 0\n"))
        self.assertIn("[subcortex: truncated", updated["output_for_prompt"])
        for key in ("exit_code", "command", "truncated", "timed_out", "current_dir", "output_file", "total_bytes"):
            self.assertIn(key, updated)
        self.assertEqual(set(out), {"hookSpecificOutput"})

    def test_grok_leaves_failed_truncated_and_background_results_alone(self):
        for change in ({"toolResultTruncated": True}, {"toolResult": {"type": "BackgroundTaskStarted"}}):
            payload = dict(self.sample("grok-build", "PostToolUse"), **change)
            self.assertIsNone(self.run_hook("grok-build", "PostToolUse", payload), change)
        payload = self.sample("grok-build", "PostToolUse")
        payload["toolResult"]["exit_code"] = 1
        self.assertIsNone(self.run_hook("grok-build", "PostToolUse", payload))

    def test_grok_restores_after_compaction_on_the_next_shell_call(self):
        self.run_hook("grok-build", "PreCompact", self.sample("grok-build", "PreCompact"))
        self.run_hook("grok-build", "PostCompact", self.sample("grok-build", "PostCompact"))
        payload = self.sample("grok-build", "PostToolUse")
        payload["toolResult"]["output_for_prompt"] = "exit: 0\nsmall output"
        out = self.run_hook("grok-build", "PostToolUse", payload)
        self.assertIn("rename getUser", out["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("updatedToolOutput", out["hookSpecificOutput"])

    def test_claude_adapter_is_silent_under_grok(self):
        with mock.patch.dict(os.environ, {"GROK_HOOK_EVENT": "user_prompt_submit"}):
            self.assertIsNone(self.run_hook("claude-code", "UserPromptSubmit",
                                            self.sample("claude-code", "UserPromptSubmit")))

    def test_docker_agent_snake_case_responses(self):
        for event in ("user_prompt_submit", "user_steering_messages_submit", "user_followup_submit"):
            out = self.run_hook("docker-agent", event, self.sample("docker-agent", event))
            self.assertEqual(set(out["hook_specific_output"]), {"additional_context"}, event)
        out = self.run_hook("docker-agent", "tool_response_transform",
                            self.sample("docker-agent", "tool_response_transform"))
        self.assertIn("[subcortex: truncated", out["hook_specific_output"]["updated_tool_response"])
        failed = dict(self.sample("docker-agent", "tool_response_transform"), tool_error=True)
        self.assertIsNone(self.run_hook("docker-agent", "tool_response_transform", failed))

    def test_docker_agent_snapshot_from_the_session_db(self):
        import sqlite3

        db = Path(self.tmp.name, "home", ".cagent", "session.db")
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE session_items (session_id TEXT, item_type TEXT, position INT, message_json TEXT)")
        session = self.sample("docker-agent", "before_compaction")["session_id"]
        for i, (role, text) in enumerate([("user", "deploy the api"), ("assistant", "Deployed."),
                                          ("tool", "ignored tool output")]):
            conn.execute("INSERT INTO session_items VALUES (?, 'message', ?, ?)",
                         (session, i, json.dumps({"role": role, "content": text})))
        conn.commit()
        conn.close()
        with mock.patch.dict(os.environ, {"HOME": str(Path(self.tmp.name, "home"))}):
            self.run_hook("docker-agent", "before_compaction", self.sample("docker-agent", "before_compaction"))
            self.run_hook("docker-agent", "after_compaction", self.sample("docker-agent", "after_compaction"))
            out = self.run_hook("docker-agent", "user_prompt_submit", self.sample("docker-agent", "user_prompt_submit"),
                                FakeClient(simple=False))
        context = out["hook_specific_output"]["additional_context"]
        self.assertIn("user: deploy the api", context)
        self.assertNotIn("ignored tool output", context)

    def test_letta_plain_text_and_slash_commands(self):
        out = hook.run("letta", "UserPromptSubmit", json.dumps(self.sample("letta", "UserPromptSubmit")),
                       self.cfg, FakeClient())
        self.assertTrue(out.startswith("[subcortex]"))
        payload = dict(self.sample("letta", "UserPromptSubmit"), is_command=True)
        self.assertIsNone(hook.run("letta", "UserPromptSubmit", json.dumps(payload), self.cfg, FakeClient()))

    def test_vibe_deny_reason_replaces_only_successful_output(self):
        out = self.run_hook("vibe", "post_tool", self.sample("vibe", "post_tool"))
        self.assertEqual(out["decision"], "deny")
        self.assertIn("[subcortex: truncated", out["reason"])
        failed = dict(self.sample("vibe", "post_tool"), tool_status="failure")
        self.assertIsNone(self.run_hook("vibe", "post_tool", failed))
        adapter = adapters.get_adapter("vibe")
        self.assertIsNone(adapter.guard("prompt", {"decision": "deny"}))  # only on tool output


class TestPolicySwitches(AdapterCase):
    def test_disabled_features_silence_every_adapter(self):
        self.cfg["features"] = {"prompt_hint": False, "trim_output": False, "compaction_snapshot": False}
        for name in adapters.names():
            for event in installers.get_installer(name).hook_events():
                self.assertIsNone(hook.run(name, event, json.dumps(self.sample(name, event)),
                                           self.cfg, FakeClient()), f"{name} {event}")
        self.assertFalse(Path(self.tmp.name, "compact").exists())


if __name__ == "__main__":
    unittest.main()
