"""End-to-end: real TUI binaries, isolated config dirs, a fake LLM API.

Opt-in (SUBCORTEX_E2E=1) and skipped when a TUI isn't installed. Each test
installs subcortex with its real installer into a throwaway config dir, points
the TUI at tests/e2e/mock_llm.py (no network, no cost) and at a stub
subcortex daemon, then asserts on what the TUI actually sent to the model.
Nothing outside the temp dirs is read or written.
"""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import pathsetup  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent / "e2e"))
from mock_llm import MockLLM  # noqa: E402

from subcortex import installers  # noqa: E402
from subcortex.config import DEFAULT_CONFIG  # noqa: E402
from subcortex.daemon import create_server  # noqa: E402

E2E = os.environ.get("SUBCORTEX_E2E") == "1"
HINT = "[subcortex] A decision model rated this request as simple"
TRIMMED = "[subcortex: truncated"


class StubBackend:
    name = "stub"

    def predict(self, state, questions):
        from subcortex.verdicts import canned_answers

        return canned_answers(questions)  # every prompt simple, every output disposable

    def available(self):
        return True, "stub"


@unittest.skipUnless(E2E, "set SUBCORTEX_E2E=1 to run end-to-end tests against real TUI binaries")
class E2ECase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="subcortex-e2e-")
        self.root = Path(self.tmp.name)
        (self.root / "work").mkdir()
        config = self.root / "subcortex.json"
        config.write_text("{}")
        token = "e2e" + "0" * 61
        (self.root / "data").mkdir(mode=0o700)
        (self.root / "data" / "token").write_text(token)  # what the sandboxed hooks and plugins read
        self.daemon = create_server(0, copy.deepcopy(DEFAULT_CONFIG),
                                    backend_factory=lambda c, name=None: StubBackend(), token=token)
        threading.Thread(target=self.daemon.serve_forever, daemon=True).start()
        self.subcortex_env = {"SUBCORTEX_CONFIG": str(config), "SUBCORTEX_DATA_DIR": str(self.root / "data"),
                              "SUBCORTEX_AUTOSTART": "0",
                              "SUBCORTEX_PORT": str(self.daemon.server_address[1])}

    def tearDown(self):
        self.daemon.shutdown()
        self.daemon.server_close()
        self.tmp.cleanup()

    def install(self, tui, env):
        with mock.patch.dict(os.environ, env):
            result = installers.get_installer(tui).install(check_version=False)
        self.assertTrue(result.ok, result.messages)

    def run_tui(self, argv, env, timeout=120):
        # stdin must be closed: some TUIs (opencode run) read piped stdin into the prompt.
        proc = subprocess.run(argv, cwd=self.root / "work", env=env, capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return proc


@unittest.skipUnless(shutil.which("claude"), "Claude Code is not installed")
class TestClaudeCode(E2ECase):
    def env(self, mock_url):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE"))}
        env.update(self.subcortex_env, CLAUDE_CONFIG_DIR=str(self.root / "claude"),
                   ANTHROPIC_API_KEY="sk-ant-e2e", ANTHROPIC_BASE_URL=mock_url,
                   DISABLE_AUTOUPDATER="1", DISABLE_TELEMETRY="1",
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
        return env

    def test_hint_and_trim_reach_the_model(self):
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("claude-code", env)
            llm.script = [{"tool": "Bash", "input": {"command": "seq 1 3000", "description": "count"}},
                          {"text": "Done."}]
            proc = self.run_tui(["claude", "-p", "count to 3000 with seq", "--output-format", "json",
                                 "--allowedTools", "Bash(seq:*)"], env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertFalse(json.loads(proc.stdout).get("is_error"), proc.stdout[:500])
            first = json.dumps(llm.agent_requests()[0])
            self.assertIn(HINT, first, "the UserPromptSubmit hint did not reach the model")
            tool_turn = json.dumps(llm.agent_requests()[1])
            self.assertIn(TRIMMED, tool_turn, "the PostToolUse trim did not reach the model")
            self.assertIn("\\n3000", tool_turn)       # the tail survived
            self.assertNotIn("\\n1500\\n", tool_turn)  # the middle did not

    def test_compaction_snapshot_survives_compact(self):
        import uuid

        with MockLLM() as llm:
            env = dict(self.env(llm.url), PWD=str(self.root / "work"))
            self.install("claude-code", env)
            session = str(uuid.uuid4())
            for args in (["--session-id", session, "remember the codeword PELICAN-42"],
                         ["--resume", session, "/compact"]):
                proc = self.run_tui(["claude", "-p", *args, "--output-format", "json"], env)
                self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            before = len(llm.requests)
            proc = self.run_tui(["claude", "-p", "--resume", session, "what was the codeword?",
                                 "--output-format", "json"], env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            after = json.dumps([r["body"] for r in llm.requests[before:]])
            self.assertIn("[subcortex] Recent conversation from before context compaction", after)
            self.assertIn("PELICAN-42", after)

    def test_hooks_never_block_even_with_the_daemon_down(self):
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("claude-code", env)
            env["SUBCORTEX_PORT"] = "1"  # nothing listens there
            proc = self.run_tui(["claude", "-p", "say hi", "--output-format", "json"], env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertEqual(json.loads(proc.stdout)["result"], "OK")
            self.assertNotIn("[subcortex]", llm.all_text())

    def test_the_0_1_0_hook_command_no_longer_blocks(self):
        # Exactly the entry 0.1.0 wrote, which blocked every prompt with exit 2.
        settings = self.root / "claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        legacy = f"{sys.executable} -m subcortex hook claude UserPromptSubmit"
        settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": legacy, "timeout": 10}]}]}}))
        with MockLLM() as llm:
            env = self.env(llm.url)
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            proc = self.run_tui(["claude", "-p", "say hi", "--output-format", "json"], env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertEqual(json.loads(proc.stdout)["result"], "OK")
            self.assertIn(HINT, llm.all_text())


@unittest.skipUnless(shutil.which("kimi"), "Kimi Code CLI is not installed")
class TestKimiCode(E2ECase):
    def test_hint_reaches_the_model(self):
        with MockLLM() as llm:
            env = dict(os.environ, **self.subcortex_env, HOME=str(self.root / "home"),
                       KIMI_CODE_HOME=str(self.root / "kimi"), KIMI_CODE_NO_AUTO_UPDATE="1",
                       KIMI_DISABLE_TELEMETRY="1", KIMI_CODE_WATCH="0", KIMI_MODEL_NAME="mock-model",
                       KIMI_MODEL_API_KEY="x", KIMI_MODEL_PROVIDER_TYPE="openai",
                       KIMI_MODEL_BASE_URL=llm.url + "/v1")
            self.install("kimi-code", env)
            proc = self.run_tui(["kimi", "-p", "what is 2+2?", "--output-format", "stream-json"], env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertIn('"content":"OK"', proc.stdout)
            self.assertIn(HINT, json.dumps(llm.agent_requests()[0]))

    def test_daemon_down_does_not_block(self):
        with MockLLM() as llm:
            env = dict(os.environ, **self.subcortex_env, HOME=str(self.root / "home"),
                       KIMI_CODE_HOME=str(self.root / "kimi"), KIMI_CODE_NO_AUTO_UPDATE="1",
                       KIMI_MODEL_NAME="mock-model", KIMI_MODEL_API_KEY="x",
                       KIMI_MODEL_PROVIDER_TYPE="openai", KIMI_MODEL_BASE_URL=llm.url + "/v1")
            self.install("kimi-code", env)
            env["SUBCORTEX_PORT"] = "1"
            proc = self.run_tui(["kimi", "-p", "say hi", "--output-format", "stream-json"], env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertIn('"content":"OK"', proc.stdout)
            self.assertNotIn("[subcortex]", llm.all_text())


@unittest.skipUnless(shutil.which("opencode"), "OpenCode is not installed")
class TestOpenCode(E2ECase):
    def env(self, mock_url):
        dirs = {k: self.root / k for k in ("cfg", "data", "state", "cache", "home")}
        for d in dirs.values():
            d.mkdir(exist_ok=True)
        (dirs["cfg"] / "opencode").mkdir(exist_ok=True)
        (dirs["cfg"] / "opencode" / "opencode.json").write_text(json.dumps({
            "provider": {"mock": {"npm": "@ai-sdk/openai-compatible", "name": "Mock",
                                  "options": {"baseURL": mock_url + "/v1", "apiKey": "x"},
                                  "models": {"mock-model": {"name": "Mock"}}}},
            "model": "mock/mock-model", "autoupdate": False, "share": "disabled"}))
        return dict(os.environ, **self.subcortex_env, XDG_CONFIG_HOME=str(dirs["cfg"]),
                    XDG_DATA_HOME=str(dirs["data"]), XDG_STATE_HOME=str(dirs["state"]),
                    XDG_CACHE_HOME=str(dirs["cache"]), OPENCODE_TEST_HOME=str(dirs["home"]),
                    OPENCODE_DISABLE_PROJECT_CONFIG="1", OPENCODE_DISABLE_AUTOUPDATE="1",
                    PWD=str(self.root / "work"))  # OpenCode takes its project dir from PWD

    def test_plugin_hint_and_trim_reach_the_model(self):
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("opencode", env)
            # Big but under OpenCode's own 2000-line / 50 KB spill limit (spilled output is left alone).
            llm.script = [{"tool": "bash", "input": {"command": "yes 'compiling module ok' | head -1200",
                                                     "description": "build"}},
                          {"text": "Done."}]
            proc = self.run_tui(["opencode", "run", "run the build"], env, timeout=180)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            requests = llm.agent_requests()
            self.assertIn(HINT, json.dumps(requests[0]), "the chat.message hint did not reach the model")
            self.assertIn(TRIMMED, json.dumps(requests[1]), "the tool.execute.after trim did not reach the model")


GROK = os.environ.get("SUBCORTEX_GROK_BIN") or shutil.which("grok")


@unittest.skipUnless(GROK, "Grok Build is not installed (or set SUBCORTEX_GROK_BIN)")
class TestGrokBuild(E2ECase):
    def test_tagged_replacement_reaches_the_model(self):
        with MockLLM() as llm:
            for sub in ("home", "grok"):
                (self.root / sub).mkdir(exist_ok=True)
            (self.root / "grok" / "config.toml").write_text(
                f'[model.mock]\nmodel = "mock"\nbase_url = "{llm.url}/v1"\napi_key = "x"\n'
                'api_backend = "chat_completions"\ncontext_window = 200000\n[models]\ndefault = "mock"\n')
            env = dict(os.environ, **self.subcortex_env, HOME=str(self.root / "home"),
                       GROK_HOME=str(self.root / "grok"), GROK_DISABLE_AUTOUPDATER="1", XAI_API_KEY="dummy",
                       PWD=str(self.root / "work"))
            self.install("grok-build", env)
            # Under Grok's own 20k-char cut, so the trim is subcortex's.
            llm.script = [{"tool": "run_terminal_command",
                           "input": {"command": "yes 'compiling module ok' | head -900", "description": "build"}},
                          {"text": "Done."}]
            proc = self.run_tui([GROK, "-p", "run the build", "-m", "mock", "--yolo", "--no-auto-update",
                                 "--output-format", "json", "--cwd", str(self.root / "work")], env, timeout=180)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            after_tool = json.dumps(llm.agent_requests()[1:])
            self.assertIn(TRIMMED, after_tool)
            self.assertIn("exit: 0", after_tool)


if __name__ == "__main__":
    unittest.main()
