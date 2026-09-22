"""End-to-end: the TypeScript plugins inside real Pi and Cline CLI processes.

Opt-in like tests/test_e2e.py (SUBCORTEX_E2E=1) and skipped when a TUI isn't
found. Binaries come from SUBCORTEX_PI_BIN / SUBCORTEX_CLINE_BIN, else PATH:

    npm install --prefix <dir>/pi @earendil-works/pi-coding-agent   -> <dir>/pi/node_modules/.bin/pi
    npm install --prefix <dir>/cline cline                          -> <dir>/cline/node_modules/.bin/cline

(cline 3.0.62's darwin-arm64 binary ships with a broken ad-hoc signature and is
SIGKILLed on start; `codesign --force --sign - <copy>` of
node_modules/@cline/cli-darwin-arm64/bin/cline makes a runnable copy.)

Each test installs the plugin with its real installer into a throwaway config
dir, runs the TUI with HOME and its config/data dirs inside the temp root,
points it at tests/e2e/mock_llm.py (OpenAI Chat Completions) and at a stub
daemon that records every call, then asserts on what the model received.
"""

import copy
import io
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

import pathsetup  # noqa: F401
from test_e2e import HINT, TRIMMED, E2ECase, StubBackend

sys.path.insert(0, str(Path(__file__).resolve().parent / "e2e"))
from mock_llm import MockLLM  # noqa: E402

from subcortex.config import DEFAULT_CONFIG  # noqa: E402
from subcortex.daemon import create_server  # noqa: E402

PI = os.environ.get("SUBCORTEX_PI_BIN") or shutil.which("pi")
CLINE = os.environ.get("SUBCORTEX_CLINE_BIN") or shutil.which("cline")

RESTORED = "[subcortex] Recent conversation from before context compaction"
# ~38 KB of successful output: under Pi's 2000-line / 50 KB and Cline's 48 KB
# command caps, over the daemon's 6000-char threshold. Numbered, so the tail
# and the middle can be told apart.
BUILD = "seq -f 'compiling module %g ok' 1 1500"
DEAD_PROXY = "http://127.0.0.1:9"


class SilentPort:
    """A daemon that accepts connections and never answers."""

    def __enter__(self) -> int:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.sock.settimeout(0.2)
        self.held, self.stop = [], False
        threading.Thread(target=self._accept, daemon=True).start()
        return self.sock.getsockname()[1]

    def _accept(self):
        while not self.stop:
            try:
                self.held.append(self.sock.accept()[0])
            except OSError:
                continue

    def __exit__(self, *exc):
        self.stop = True
        time.sleep(0.3)
        for conn in [*self.held, self.sock]:
            conn.close()


class PluginCase(E2ECase):
    def setUp(self):
        super().setUp()
        self.calls = []  # (path, body) of every request the daemon got
        (self.root / "home").mkdir()
        (self.root / "tmp").mkdir()
        self.use_daemon()

    def use_daemon(self, **features):
        """Swap the base stub daemon for one that records calls. Trimming is
        opted in the way a user would (thresholds.output_needed_threshold), so
        these tests don't depend on which backend profiles trim by default."""
        self.daemon.shutdown()
        self.daemon.server_close()
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["thresholds"]["output_needed_threshold"] = 0.5
        cfg["features"].update(features)
        token = (self.root / "data" / "token").read_text()
        self.daemon = create_server(0, cfg, backend_factory=lambda c, name=None: StubBackend(), token=token)
        calls = self.calls

        class Recording(self.daemon.RequestHandlerClass):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.rfile = io.BytesIO(raw)
                try:
                    calls.append((self.path, json.loads(raw)))
                except ValueError:
                    calls.append((self.path, None))
                return super().do_POST()

        self.daemon.RequestHandlerClass = Recording
        threading.Thread(target=self.daemon.serve_forever, daemon=True).start()
        self.subcortex_env["SUBCORTEX_PORT"] = str(self.daemon.server_address[1])

    def daemon_paths(self):
        return [path for path, _ in self.calls]

    def base_env(self, **extra):
        # Nothing of the developer's: no provider keys, no proxies, no SUBCORTEX_URL
        # (the plugin prefers it over the installed URL), no TUI config dirs.
        drop = ("ANTHROPIC_", "OPENAI_", "PI_", "CLINE_", "SUBCORTEX_", "NODE_OPTIONS", "XDG_", "NPM_CONFIG_")
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(drop) and "PROXY" not in k.upper()}
        home = self.root / "home"
        env.update(self.subcortex_env, HOME=str(home), TMPDIR=str(self.root / "tmp"),
                   XDG_CONFIG_HOME=str(home / ".config"), XDG_CACHE_HOME=str(home / ".cache"),
                   XDG_STATE_HOME=str(home / ".local" / "state"), XDG_DATA_HOME=str(home / ".local" / "share"),
                   npm_config_cache=str(home / ".npm"), PWD=str(self.root / "work"), **extra)
        return env

    @staticmethod
    def proxied(env, llm):
        """A dead HTTP proxy for everything but the mock model's exact host:port, so
        the TUI's own traffic still flows while a proxied daemon call would fail."""
        exempt = llm.url.split("//", 1)[1]
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env[key] = env[key.lower()] = DEAD_PROXY
        env["NO_PROXY"] = env["no_proxy"] = exempt
        return env

    @staticmethod
    def messages(request):
        return request.get("messages") or []

    def tool_message(self, request):
        tools = [m for m in self.messages(request) if m.get("role") == "tool"]
        self.assertTrue(tools, "no tool result in the request")
        content = tools[-1].get("content")
        return content if isinstance(content, str) else json.dumps(content)

    def assert_trimmed(self, tool_text):
        self.assertIn(TRIMMED, tool_text, "the trimmed output did not reach the model")
        self.assertIn("compiling module 1500 ok", tool_text)   # the tail survived
        self.assertNotIn("compiling module 750 ok", tool_text)  # the middle did not

    def assert_same_session(self):
        sessions = {body.get("session_id") for path, body in self.calls
                    if path in ("/v1/prompt-hint", "/v1/tool-output") and body}
        self.assertEqual(len(sessions), 1, f"prompt and output went to different sessions: {sessions}")
        self.assertTrue(sessions.pop())


@unittest.skipUnless(PI, "Pi is not installed (or set SUBCORTEX_PI_BIN)")
class TestPi(PluginCase):
    def env(self, llm, settings=None, **extra):
        agent = self.root / "pi"
        agent.mkdir(exist_ok=True)
        (agent / "models.json").write_text(json.dumps({"providers": {"mock": {
            "baseUrl": llm.url + "/v1", "api": "openai-completions", "apiKey": "sk-e2e",
            "compat": {"supportsDeveloperRole": False, "supportsReasoningEffort": False},
            "models": [{"id": "mock-model", "reasoning": False, "input": ["text"],
                        "contextWindow": 32000, "maxTokens": 4096}]}}}))
        if settings:
            (agent / "settings.json").write_text(json.dumps(settings))
        return self.base_env(PI_CODING_AGENT_DIR=str(agent), PI_OFFLINE="1", PI_SKIP_VERSION_CHECK="1",
                             PI_TELEMETRY="0", **extra)

    ARGS = ["--no-session", "--no-context-files", "--no-skills", "--model", "mock/mock-model"]

    def run_pi(self, env, prompt):
        proc = self.run_tui([PI, "-p", *self.ARGS, prompt], env)
        # A plugin that fails to load makes every pi invocation exit 1.
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertNotIn("Extension error", proc.stderr)
        return proc

    def test_plugin_loads_and_the_hint_reaches_the_model(self):
        with MockLLM() as llm:
            env = self.env(llm)
            self.install("pi", env)
            proc = self.run_pi(env, "what is 2+2?")
            self.assertEqual(proc.stdout.strip(), "OK")
            messages = self.messages(llm.agent_requests()[0])
            prompt = next(i for i, m in enumerate(messages) if "what is 2+2?" in json.dumps(m))
            # before_agent_start: a hidden custom message, sent as the next user message.
            self.assertEqual(messages[prompt + 1]["role"], "user")
            self.assertIn(HINT, json.dumps(messages[prompt + 1]))
            self.assertEqual(self.calls[0][1]["tui"], "pi")

    def test_bash_output_reaches_the_model_trimmed(self):
        with MockLLM() as llm:
            env = self.env(llm)
            self.install("pi", env)
            llm.script = [{"tool": "bash", "input": {"command": BUILD}}, {"text": "Done."}]
            proc = self.run_pi(env, "run the build")
            self.assertEqual(proc.stdout.strip(), "Done.")
            self.assertEqual(self.daemon_paths(), ["/v1/prompt-hint", "/v1/tool-output"])
            self.assert_same_session()  # the trim needs the request the prompt hook remembered
            self.assert_trimmed(self.tool_message(llm.agent_requests()[1]))

    def test_spill_file_pointer_survives_the_trim(self):
        # Over 2000 lines Pi keeps the tail and appends "[Showing ... Full output: <file>]".
        with MockLLM() as llm:
            env = self.env(llm)
            self.install("pi", env)
            llm.script = [{"tool": "bash", "input": {"command": BUILD.replace("1500", "3000")}},
                          {"text": "Done."}]
            self.run_pi(env, "run the build")
            text = self.tool_message(llm.agent_requests()[1])
            self.assertIn(TRIMMED, text)
            self.assertIn("compiling module 3000 ok", text)
            self.assertRegex(text, r"\[Showing lines [^\]]*Full output: [^\]]+\]$")

    def test_compaction_restore_reaches_the_model(self):
        with MockLLM() as llm:
            # Keep almost nothing verbatim, so the codeword survives only through the restore.
            env = self.env(llm, settings={"compaction": {"keepRecentTokens": 10}})
            self.install("pi", env)
            events, compact = self.run_rpc(env, [
                {"id": "1", "type": "prompt", "message": "remember the codeword PELICAN-42"},
                {"id": "2", "type": "prompt", "message": "also note the color teal"},
                {"id": "c", "type": "compact"},
                {"id": "3", "type": "prompt", "message": "what was the codeword?"}], llm)
            self.assertTrue(compact.get("success"), compact)
            self.assertIn("/v1/snapshot", self.daemon_paths())
            self.assertIn("/v1/restore", self.daemon_paths())
            messages = self.messages(llm.agent_requests()[-1])
            restored = [i for i, m in enumerate(messages) if RESTORED in json.dumps(m)]
            self.assertEqual(len(restored), 1, "the restored context did not reach the model")
            self.assertIn("PELICAN-42", json.dumps(messages[restored[0]]))
            self.assertIn("summary", json.dumps(messages[restored[0] - 1]))  # right after Pi's summary

    def run_rpc(self, env, commands, llm, timeout=90):
        """Drive `pi --mode rpc`: each prompt waits for agent_settled, compact for its response."""
        stderr = open(self.root / "pi-rpc.err", "w+")
        proc = subprocess.Popen([PI, "--mode", "rpc", *self.ARGS], cwd=self.root / "work", env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr)
        lines = queue.Queue()
        threading.Thread(target=lambda: [*map(lines.put, proc.stdout), lines.put(None)], daemon=True).start()
        deadline = time.monotonic() + timeout
        events, compact = [], {}

        def until(done):
            while True:
                try:
                    line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
                except queue.Empty:
                    self.fail(f"pi --mode rpc stalled; last events: {[e.get('type') for e in events[-5:]]}")
                if line is None:
                    stderr.seek(0)
                    self.fail(f"pi exited early: {stderr.read()[-2000:]}")
                event = json.loads(line)
                events.append(event)
                if event.get("type") == "extension_error":
                    self.fail(f"extension error: {event}")
                if done(event):
                    return event

        try:
            for command in commands:
                proc.stdin.write((json.dumps(command) + "\n").encode())
                proc.stdin.flush()
                if command["type"] == "compact":
                    compact = until(lambda e: e.get("type") == "response" and e.get("id") == "c")
                else:
                    until(lambda e: e.get("type") == "agent_settled")
            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=30), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            stderr.close()
        return events, compact

    def test_daemon_down_runs_normally(self):
        with MockLLM() as llm:
            env = self.env(llm, SUBCORTEX_PORT="1")  # nothing listens there
            self.install("pi", env)
            llm.script = [{"tool": "bash", "input": {"command": BUILD}}, {"text": "Done."}]
            proc = self.run_pi(env, "run the build")
            self.assertEqual(proc.stdout.strip(), "Done.")
            self.assertEqual(proc.stderr.strip(), "")
            self.assertNotIn("[subcortex", llm.all_text())
            self.assertIn("compiling module 750 ok", self.tool_message(llm.agent_requests()[1]))

    def test_hung_daemon_runs_normally(self):
        with MockLLM() as llm, SilentPort() as port:
            env = self.env(llm, SUBCORTEX_PORT=str(port))
            self.install("pi", env)
            llm.script = [{"tool": "bash", "input": {"command": BUILD}}, {"text": "Done."}]
            started = time.monotonic()
            proc = self.run_pi(env, "run the build")
            self.assertEqual(proc.stdout.strip(), "Done.")
            self.assertLess(time.monotonic() - started, 30)  # per-request timeouts: 1.5 s + 3 s
            self.assertNotIn("[subcortex", llm.all_text())

    def test_http_proxy_does_not_divert_daemon_calls(self):
        # Pi routes its own HTTP through HTTP_PROXY; the plugin's raw socket must not.
        with MockLLM() as llm:
            env = self.proxied(self.env(llm), llm)
            self.install("pi", env)
            llm.script = [{"tool": "bash", "input": {"command": BUILD}}, {"text": "Done."}]
            self.run_pi(env, "run the build")
            self.assertEqual(self.daemon_paths(), ["/v1/prompt-hint", "/v1/tool-output"])
            self.assertIn(HINT, json.dumps(llm.agent_requests()[0]))
            self.assert_trimmed(self.tool_message(llm.agent_requests()[1]))


@unittest.skipUnless(CLINE, "Cline CLI is not installed (or set SUBCORTEX_CLINE_BIN)")
class TestCline(PluginCase):
    def env(self, llm, context_window=64000, **extra):
        self.cline_dir, self.cline_data = self.root / "cline", self.root / "cline-data"
        settings = self.cline_data / "settings"
        settings.mkdir(parents=True, exist_ok=True)
        (settings / "providers.json").write_text(json.dumps({
            "version": 1, "lastUsedProvider": "openai-compatible", "modes": {},
            "providers": {"openai-compatible": {"settings": {
                "provider": "openai-compatible", "apiKey": "sk-e2e", "model": "mock-model",
                "baseUrl": llm.url + "/v1", "contextWindow": context_window, "maxTokens": 4096},
                "updatedAt": "2026-09-21T00:00:00.000Z", "tokenSource": "manual"}}}))
        (settings / "global-settings.json").write_text(json.dumps({"telemetryOptOut": True}))
        return self.base_env(CLINE_DIR=str(self.cline_dir), CLINE_NO_AUTO_UPDATE="1",
                             CLINE_DISABLE_CLINE_PASS_NOTICE="1", CLINE_TELEMETRY_DISABLED="1",
                             CLINE_HUB_DISCOVERY_PATH=str(self.cline_data / "locks" / "hub" / "discovery.json"),
                             **extra)

    def run_cline(self, env, prompt, timeout=120):
        # --data-dir: isolated state and the local backend (no hub daemon). Not --yolo:
        # it requires a completion tool, so a plain-text reply would never end the run.
        proc = self.run_tui([CLINE, "--config", str(self.cline_dir), "--data-dir", str(self.cline_data),
                             "--json", "-P", "openai-compatible", "-m", "mock-model", prompt], env, timeout)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        results = [json.loads(line) for line in proc.stdout.splitlines()
                   if line.startswith("{") and '"run_result"' in line]
        self.assertTrue(results, proc.stdout[-2000:])
        self.assertEqual(results[-1].get("finishReason"), "completed", results[-1])
        self.assertNotIn("timed out", proc.stdout + proc.stderr)  # the sandbox kills hooks at 3 s
        return proc

    def test_plugin_loads_and_the_hint_reaches_the_model(self):
        with MockLLM() as llm:
            env = self.env(llm)
            self.install("cline", env)
            self.run_cline(env, "what is 2+2?")
            messages = self.messages(llm.agent_requests()[0])
            prompt = [m for m in messages if m.get("role") == "user" and "what is 2+2?" in json.dumps(m)]
            self.assertEqual(len(prompt), 1)
            # beforeModel: an extra text part on the prompt message itself.
            self.assertIn(HINT, json.dumps(prompt[0]))
            self.assertEqual(self.calls[0][1]["tui"], "cline")

    def test_run_commands_output_reaches_the_model_trimmed(self):
        with MockLLM() as llm:
            env = self.env(llm)
            self.install("cline", env)
            llm.script = [{"tool": "run_commands", "input": {"commands": [BUILD]}}, {"text": "Done."}]
            self.run_cline(env, "run the build")
            self.assertEqual(self.daemon_paths(), ["/v1/prompt-hint", "/v1/tool-output"])
            self.assert_same_session()
            self.assert_trimmed(self.tool_message(llm.agent_requests()[1]))
            self.assertIn(HINT, json.dumps(llm.agent_requests()[1]))  # re-applied on every request

    def compact_mid_run(self, llm):
        """One run that compacts between its two agent requests: a small context
        window, then a tool result that overflows it. Agentic compaction (the
        default) asks the mock for a summary; `--compaction basic` would not
        produce a compaction_summary message at all."""
        self.use_daemon(trim_output=False)  # the full output is what overflows the window
        env = self.env(llm, context_window=8000)
        self.install("cline", env)
        llm.script = [{"tool": "run_commands", "input": {"commands": [BUILD.replace("1500", "3000")]}},
                      {"text": "Done."}]
        self.run_cline(env, "remember the codeword PELICAN-42, then run the build")
        after = [r for r in llm.agent_requests() if "Context summary:" in json.dumps(r)]
        self.assertTrue(after, "Cline did not compact; retune the context window")
        return after[0]

    def test_compaction_is_detected(self):
        with MockLLM() as llm:
            self.compact_mid_run(llm)
            self.assertIn("/v1/snapshot", self.daemon_paths())
            self.assertIn("/v1/restore", self.daemon_paths())

    # Product bug: the plugin posts Cline's raw messages, whose prompt text is
    # <user_input mode="act">…</user_input>; the daemon's transcript normalizer
    # drops text that starts with a tag, so the snapshot is empty ("saved": false)
    # and /v1/restore returns null. Remove the decorator once the plugin sends
    # plain {role, text} messages.
    def test_compaction_restore_reaches_the_model(self):
        with MockLLM() as llm:
            request = self.compact_mid_run(llm)
            summary = [m for m in self.messages(request) if "Context summary:" in json.dumps(m)]
            self.assertIn(RESTORED, json.dumps(summary))  # appended to the summary message
            self.assertIn("PELICAN-42", json.dumps(summary))

    def test_daemon_down_runs_normally(self):
        with MockLLM() as llm:
            env = self.env(llm, SUBCORTEX_PORT="1")  # nothing listens there
            self.install("cline", env)
            llm.script = [{"tool": "run_commands", "input": {"commands": [BUILD]}}, {"text": "Done."}]
            self.run_cline(env, "run the build")
            self.assertNotIn("[subcortex", llm.all_text())
            self.assertEqual(self.calls, [])

    def test_hung_daemon_stays_under_the_sandbox_hook_timeout(self):
        with MockLLM() as llm, SilentPort() as port:
            env = self.env(llm, SUBCORTEX_PORT=str(port))
            self.install("cline", env)
            llm.script = [{"tool": "run_commands", "input": {"commands": [BUILD]}}, {"text": "Done."}]
            self.run_cline(env, "run the build")
            self.assertEqual(len(llm.agent_requests()), 2)
            self.assertNotIn("[subcortex", llm.all_text())

    def test_http_proxy_does_not_divert_daemon_calls(self):
        with MockLLM() as llm:
            env = self.proxied(self.env(llm), llm)
            self.install("cline", env)
            llm.script = [{"tool": "run_commands", "input": {"commands": [BUILD]}}, {"text": "Done."}]
            self.run_cline(env, "run the build")
            self.assertEqual(self.daemon_paths(), ["/v1/prompt-hint", "/v1/tool-output"])
            self.assertIn(HINT, json.dumps(llm.agent_requests()[0]))
            self.assert_trimmed(self.tool_message(llm.agent_requests()[1]))


if __name__ == "__main__":
    unittest.main()
