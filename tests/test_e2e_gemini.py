"""End-to-end: Gemini CLI and Qwen Code (real binaries, sandboxed, fake models).

Same contract as test_e2e.py: opt-in (SUBCORTEX_E2E=1), skipped when a binary
is missing (SUBCORTEX_GEMINI_BIN / SUBCORTEX_QWEN_BIN, else PATH). HOME, the XDG
dirs, npm's cache, TMPDIR and each TUI's config/runtime dirs point into the
test's temp root; Gemini CLI talks to tests/e2e/mock_gemini.py
(GOOGLE_GEMINI_BASE_URL + a fake key), Qwen Code to the Chat Completions mock
(OPENAI_BASE_URL). Every test checks afterwards that the real ~/.gemini and
~/.qwen (and gemini/qwen-named entries in the shared dirs) are untouched.

Neither TUI can *replace* a tool result from a hook (Gemini's AfterTool and
Qwen's PostToolUse only append, block or stop), so subcortex registers no
after-tool hook there: the shell tests assert the tool turn passes through
unblocked and untrimmed by us instead of asserting a trim.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import unittest
import uuid
from pathlib import Path

from test_e2e import HINT, TRIMMED, E2ECase  # also puts tests/e2e on sys.path

from mock_gemini import MockGemini  # noqa: E402
from mock_llm import MockLLM  # noqa: E402

RESTORED = "[subcortex] Recent conversation from before context compaction"
BUILD = "yes 'compiling module ok' | head -1500; echo build-finished"  # ~30 KB, exit 0
GEMINI = os.environ.get("SUBCORTEX_GEMINI_BIN") or shutil.which("gemini")
QWEN = os.environ.get("SUBCORTEX_QWEN_BIN") or shutil.which("qwen")
_VENDOR_ENV = ("GEMINI", "GOOGLE", "QWEN", "OPENAI", "DASHSCOPE", "ANTHROPIC", "VERTEX", "CLOUDSDK",
               "XDG_", "npm_config_", "NPM_CONFIG_")
# The real user's config dirs (whole listing) and shared dirs (entries named like these TUIs).
_REAL_OWN = ("~/.gemini", "~/.qwen")
_REAL_SHARED = ("~", "~/.config", "~/.cache", "~/.local/state", "~/.local/share", "~/Library/Caches",
                "~/Library/Application Support", "~/Library/Preferences")
_REAL_SETTINGS = ("~/.gemini/settings.json", "~/.qwen/settings.json")


def _fingerprint():
    """What a sandbox escape would change in the real HOME (other programs' churn ignored)."""
    out = {}
    for d in _REAL_OWN + _REAL_SHARED:
        try:
            names = sorted(os.listdir(os.path.expanduser(d)))
        except OSError:
            names = None
        if names is not None and d in _REAL_SHARED:
            names = [n for n in names if re.search(r"gemini|qwen", n, re.I)]
        out[d] = names
    for f in _REAL_SETTINGS:
        try:
            st = os.stat(os.path.expanduser(f))
            out[f] = (st.st_mtime_ns, st.st_size)
        except OSError:
            out[f] = None
    return out


def _clean_env():
    return {k: v for k, v in os.environ.items() if not k.startswith(_VENDOR_ENV)}


class _SandboxCase(E2ECase):
    settings_file: Path

    def setUp(self):
        super().setUp()
        self.real_before = _fingerprint()
        (self.root / "home").mkdir()

    def tearDown(self):
        super().tearDown()
        self.assertEqual(_fingerprint(), self.real_before, "the TUI wrote outside its sandbox")

    def sandbox_env(self):
        """Environment with HOME, XDG dirs, npm cache and TMPDIR all inside the temp root."""
        env = _clean_env()
        env.update(self.subcortex_env, HOME=str(self.root / "home"), PWD=str(self.root / "work"),
                   npm_config_cache=str(self.root / "npm-cache"), TMPDIR=str(self.root / "tmp"))
        for name in ("CONFIG", "CACHE", "STATE", "DATA"):
            env[f"XDG_{name}_HOME"] = str(self.root / "xdg" / name.lower())
        for key in ("npm_config_cache", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                    "XDG_STATE_HOME", "XDG_DATA_HOME"):
            Path(env[key]).mkdir(parents=True, exist_ok=True)
        return env

    def break_interpreter(self):
        """Point our installed hook commands at an interpreter that no longer exists
        (an uninstalled venv): bash exits 127 with "No such file" unless guarded."""
        text = self.settings_file.read_text()
        commands = [h["command"] for groups in json.loads(text)["hooks"].values()
                    for g in groups for h in g["hooks"] if h.get("name", "").startswith("subcortex:")]
        self.assertTrue(commands)
        interpreter = shlex.split(commands[0])[0]
        self.settings_file.write_text(text.replace(interpreter, str(self.root / "gone" / "python")))


@unittest.skipUnless(GEMINI, "Gemini CLI is not installed (or set SUBCORTEX_GEMINI_BIN)")
class TestGeminiCli(_SandboxCase):
    def env(self, mock_url):
        home = self.root / "home"
        self.settings_file = home / ".gemini" / "settings.json"
        self.settings_file.parent.mkdir(exist_ok=True)
        self.settings_file.write_text(json.dumps({
            "security": {"auth": {"selectedType": "gemini-api-key"}},  # "gateway" fails -p validation
            "privacy": {"usageStatisticsEnabled": False}, "telemetry": {"enabled": False},
            "general": {"enableAutoUpdate": False, "enableAutoUpdateNotification": False}}))
        env = self.sandbox_env()
        env.update(GEMINI_CLI_HOME=str(home), GEMINI_API_KEY="e2e-fake-key", GOOGLE_GEMINI_BASE_URL=mock_url,
                   GEMINI_CLI_SYSTEM_SETTINGS_PATH=str(self.root / "gemini-system.json"),
                   GEMINI_CLI_SYSTEM_DEFAULTS_PATH=str(self.root / "gemini-system-defaults.json"),
                   # Folder trust is on by default and gates even user-level hooks.
                   GEMINI_CLI_TRUST_WORKSPACE="true", GEMINI_CLI_NO_RELAUNCH="true")
        return env

    def gemini(self, env, *args):
        proc = self.run_tui([GEMINI, *args, "--output-format", "json"], env)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:] or proc.stdout[-2000:])
        self.assertNotIn("Hook", proc.stderr)  # "Hook(s) [...] failed" / "Hook blocked" warnings
        return proc

    def test_hint_reaches_the_model(self):
        with MockGemini() as llm:
            env = self.env(llm.url)
            self.install("gemini-cli", env)
            proc = self.gemini(env, "-p", "what is 2+2?")
            self.assertEqual(json.loads(proc.stdout)["response"], "OK")
            prompt_turn = llm.agent_requests()[0]["contents"][-1]
            self.assertIn(f"<hook_context>{HINT}", json.dumps(prompt_turn),
                          "the BeforeAgent hint did not reach the model")

    def test_shell_turn_passes_through_unblocked(self):
        with MockGemini() as llm:
            env = self.env(llm.url)
            self.install("gemini-cli", env)
            llm.script = [{"tool": "run_shell_command", "input": {"command": BUILD, "description": "build"}},
                          {"text": "Done."}]
            proc = self.gemini(env, "-p", "run the build", "--yolo")
            self.assertEqual(json.loads(proc.stdout)["response"], "Done.")
            tool_turn = json.dumps(llm.agent_requests()[1]["contents"][-1])
            self.assertIn("functionResponse", tool_turn)
            self.assertIn("build-finished", tool_turn)
            self.assertIn("compiling module ok\\ncompiling module ok", tool_turn)  # full output, below Gemini's 40k cut
            self.assertNotIn("Tool result blocked", tool_turn)
            self.assertNotIn(TRIMMED, tool_turn)  # append-only AfterTool: we don't register one

    def _compress_flow(self, llm, env):
        session = str(uuid.uuid4())
        self.gemini(env, "--session-id", session, "-p", "remember the codeword PELICAN-42")
        before = len(llm.requests)
        # -p "/compress" compresses, then Gemini also forwards "/compress" to the model as a prompt.
        self.gemini(env, "--resume", session, "-p", "/compress")
        self.gemini(env, "--resume", session, "-p", "what was the codeword?")
        return [r["body"] for r in llm.requests[before:] if r["body"].get("tools")]

    def test_compress_snapshot_reaches_the_model(self):
        with MockGemini() as llm:
            env = self.env(llm.url)
            self.install("gemini-cli", env)
            after = self._compress_flow(llm, env)
            restored = [json.dumps(b["contents"][-1]) for b in after if RESTORED in json.dumps(b["contents"][-1])]
            self.assertTrue(restored, "the PreCompress snapshot never came back on a BeforeAgent")
            self.assertIn("PELICAN-42", restored[0])
            # One-shot: later prompts don't repeat it.
            self.assertNotIn(RESTORED, json.dumps(after[-1]["contents"][-1]))

    def test_snapshot_does_not_echo_injected_context(self):
        with MockGemini() as llm:
            env = self.env(llm.url)
            self.install("gemini-cli", env)
            after = self._compress_flow(llm, env)
            restored = next(json.dumps(b["contents"][-1]) for b in after if RESTORED in json.dumps(b["contents"][-1]))
            self.assertNotIn("hook_context&gt;", restored)

    def test_daemon_down_never_blocks(self):
        with MockGemini() as llm:
            env = self.env(llm.url)
            self.install("gemini-cli", env)
            env["SUBCORTEX_PORT"] = "1"  # nothing listens there
            proc = self.gemini(env, "-p", "say hi")
            self.assertEqual(json.loads(proc.stdout)["response"], "OK")
            self.assertNotIn("[subcortex]", llm.all_text())

    def test_missing_interpreter_never_blocks(self):
        # Unguarded, bash's exit 127 + "No such file" on stderr is a deny in Gemini: the prompt is dropped.
        with MockGemini() as llm:
            env = self.env(llm.url)
            self.install("gemini-cli", env)
            self.break_interpreter()
            proc = self.gemini(env, "-p", "say hi")
            self.assertEqual(json.loads(proc.stdout)["response"], "OK")
            self.assertTrue(llm.agent_requests(), "the prompt never reached the model")
            self.assertNotIn("[subcortex]", llm.all_text())


class _UsageSizedLLM(MockLLM):
    """Chat Completions mock that reports prompt_tokens ~ request size / 4.

    Qwen's /compress compares its own estimate of the compacted history with the
    API-reported count and refuses ("not beneficial") when the fixed 10 tokens
    of MockLLM make the original look smaller.
    """

    def _handler(self):
        base = super()._handler()

        class Handler(base):
            def _openai(self, body):
                self.prompt_tokens = max(10, len(json.dumps(body)) // 4)
                super()._openai(body)

            def _patch(self, obj):
                usage = obj.get("usage") if isinstance(obj, dict) else None
                if usage and getattr(self, "prompt_tokens", None):
                    usage["prompt_tokens"] = self.prompt_tokens
                    usage["total_tokens"] = self.prompt_tokens + usage.get("completion_tokens", 0)

            def _json(self, code, body):
                self._patch(body)
                super()._json(code, body)

            def _sse(self, events, anthropic):
                for event in events:
                    self._patch(event)
                super()._sse(events, anthropic)

        return Handler


@unittest.skipUnless(QWEN, "Qwen Code is not installed (or set SUBCORTEX_QWEN_BIN)")
class TestQwenCode(_SandboxCase):
    def env(self, mock_url):
        home = self.root / "home"
        self.settings_file = home / ".qwen" / "settings.json"
        self.settings_file.parent.mkdir(exist_ok=True)
        self.settings_file.write_text(json.dumps({
            "security": {"auth": {"selectedType": "openai"}},
            "privacy": {"usageStatisticsEnabled": False}, "telemetry": {"enabled": False},
            "general": {"enableAutoUpdate": False},
            # Background memory extraction sends its own tool-carrying requests.
            "memory": {"enableManagedAutoMemory": False}}))
        env = self.sandbox_env()
        env.update(QWEN_HOME=str(self.settings_file.parent), QWEN_RUNTIME_DIR=str(self.root / "qwen-runtime"),
                   QWEN_CODE_SYSTEM_SETTINGS_PATH=str(self.root / "qwen-system.json"),
                   QWEN_CODE_SYSTEM_DEFAULTS_PATH=str(self.root / "qwen-system-defaults.json"),
                   OPENAI_API_KEY="e2e-fake-key", OPENAI_BASE_URL=mock_url + "/v1", OPENAI_MODEL="mock-model",
                   QWEN_USAGE_STATISTICS_ENABLED="0", QWEN_TELEMETRY_ENABLED="0",
                   QWEN_CODE_SUPPRESS_YOLO_WARNING="1")
        return env

    def qwen(self, env, *args):
        proc = self.run_tui([QWEN, *args, "--output-format", "json"], env)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:] or proc.stdout[-2000:])
        self.assertNotIn("hook", proc.stderr.lower())
        result = json.loads(proc.stdout)[-1]
        self.assertFalse(result.get("is_error"), result)
        return result

    def test_hint_reaches_the_model(self):
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("qwen-code", env)
            self.assertEqual(self.qwen(env, "what is 2+2?")["result"], "OK")
            prompt_turn = json.dumps(llm.agent_requests()[0]["messages"][-1])
            self.assertIn("<qwen:user-prompt-submit-context>\\n" + HINT, prompt_turn,
                          "the UserPromptSubmit hint did not reach the model")

    def test_shell_turn_passes_through_and_continuations_get_no_hint(self):
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("qwen-code", env)
            llm.script = [{"tool": "run_shell_command",
                           "input": {"command": BUILD, "description": "build", "is_background": False}},
                          {"text": "Done."}]
            self.assertEqual(self.qwen(env, "run the build", "--yolo")["result"], "Done.")
            tool_request = llm.agent_requests()[1]
            tool_turn = json.dumps(tool_request["messages"][-1])
            self.assertIn('"role": "tool"', tool_turn)
            self.assertIn("build-finished", tool_turn)
            self.assertIn("Exit Code: 0", tool_turn)
            self.assertNotIn(TRIMMED, tool_turn)  # Qwen truncates natively; PostToolUse is append-only
            # UserPromptSubmit re-fires on the tool-result continuation (without submitted_prompt):
            # the hint must not be injected a second time.
            self.assertEqual(json.dumps(tool_request).count(HINT), 1)

    def _compress_flow(self):
        """System prompt of the first model request after a /compress.

        SessionStart(compact) context lives in the in-memory system instruction, so the
        prompt after /compress must come from the same process: drive it over stream-json.
        """
        with _UsageSizedLLM() as llm:
            env = self.env(llm.url)
            self.install("qwen-code", env)
            proc = subprocess.Popen([QWEN, "--input-format", "stream-json", "--output-format", "stream-json"],
                                    cwd=self.root / "work", env=env, text=True, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            watchdog = threading.Timer(120, proc.kill)  # a hung turn fails the test instead of the run
            watchdog.start()
            try:
                results = [self._turn(proc, text) for text in
                           ("remember the codeword PELICAN-42", "/compress", "what was the codeword?")]
                proc.stdin.close()
                self.assertEqual(proc.wait(timeout=60), 0, proc.stderr.read()[-2000:])
            finally:
                watchdog.cancel()
                if proc.poll() is None:
                    proc.kill()
                proc.stdout.close()
                proc.stderr.close()
            self.assertTrue(results[1]["result"].startswith("Context compressed"), results[1])
            return llm.agent_requests()[-1]["messages"][0]["content"]

    def test_compress_snapshot_lands_in_the_system_prompt(self):
        system = self._compress_flow()
        self.assertIn(RESTORED, system, "the SessionStart(compact) snapshot is not in the system prompt")
        self.assertIn("PELICAN-42", system[system.index(RESTORED):])

    def test_snapshot_does_not_echo_injected_context(self):
        system = self._compress_flow()
        self.assertNotIn("user-prompt-submit-context&gt;", system[system.index(RESTORED):])

    def _turn(self, proc, text):
        message = {"type": "user", "session_id": "e2e", "parent_tool_use_id": None,
                   "message": {"role": "user", "content": text}}
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result":
                self.assertFalse(event.get("is_error"), event)
                return event
        self.fail(f"qwen exited before answering {text!r}: {proc.stderr.read()[-2000:]}")

    def test_daemon_down_never_blocks(self):
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("qwen-code", env)
            env["SUBCORTEX_PORT"] = "1"
            self.assertEqual(self.qwen(env, "say hi")["result"], "OK")
            self.assertNotIn("[subcortex]", llm.all_text())

    def test_missing_interpreter_never_blocks(self):
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("qwen-code", env)
            self.break_interpreter()
            self.assertEqual(self.qwen(env, "say hi")["result"], "OK")
            self.assertNotIn("[subcortex]", llm.all_text())
            self.assertNotIn("No such file", llm.all_text())  # plain stdout would become model context


if __name__ == "__main__":
    unittest.main()
