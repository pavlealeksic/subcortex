"""End-to-end: TUIs whose model traffic normally goes to a vendor backend.

Each class drives the real TUI binary against tests/e2e/mock_llm.py (or a
mock of the vendor's own wire format) with NO vendor login: a BYOK / custom
endpoint / local backend where the TUI has one. Same contract as
test_e2e.py: opt-in (SUBCORTEX_E2E=1), skipped when the binary is absent
(SUBCORTEX_<TUI>_BIN, else PATH), subcortex installed with its real installer
into a throwaway HOME, assertions on what the TUI actually sent to the model.

Sandboxing: HOME, XDG_* and each TUI's own config-dir variables point into
the test's temp dir, vendor credentials are stripped from the environment, and
every proxy variable points at a dead port with only 127.0.0.1 exempt, so
nothing but the local mock is reachable for runtimes that honor proxies.

    cd tests && SUBCORTEX_E2E=1 SUBCORTEX_DROID_BIN=... ../.venv/bin/python -m unittest test_e2e_vendor -v
"""

import fcntl
import json
import os
import pty
import queue
import re
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from test_e2e import HINT, TRIMMED, E2ECase  # also puts tests/e2e on sys.path

from mock_amp import MockAmpServer  # noqa: E402
from mock_augment import MockAugment  # noqa: E402
from mock_connect import MockConnect  # noqa: E402
from mock_cursor import MockCursorBackend  # noqa: E402
from mock_llm import MockLLM  # noqa: E402
from mock_usage import UsageMockLLM  # noqa: E402
from subcortex import policy  # noqa: E402

RESTORED = "[subcortex] Recent conversation from before context compaction"
DEAD_PROXY = "http://127.0.0.1:9"
NO_NETWORK = {"HTTPS_PROXY": DEAD_PROXY, "HTTP_PROXY": DEAD_PROXY, "ALL_PROXY": DEAD_PROXY,
              "https_proxy": DEAD_PROXY, "http_proxy": DEAD_PROXY, "all_proxy": DEAD_PROXY,
              "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
              "NODE_USE_ENV_PROXY": "1"}


def _bin(env_var, *names):
    explicit = os.environ.get(env_var, "").strip()
    if explicit:
        return explicit
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


# -- kernel fence (macOS) ------------------------------------------------------------------

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def fence_profile(root, private_dirs):
    """A macOS sandbox-exec profile: loopback-only network, no keychain, no writes to
    the real home outside ``root``, no reads of the TUI's real state (``private_dirs``,
    relative to the real home) — whatever the TUI's own code does."""
    home = str(Path.home())
    q = lambda p: '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'  # noqa: E731
    return "\n".join([
        "(version 1)", "(allow default)",
        "(deny network-outbound)", '(allow network-outbound (remote ip "localhost:*"))',
        "(allow network-outbound (remote unix-socket))",
        f"(deny file-write* (subpath {q(home)}))", f"(allow file-write* (subpath {q(root)}))",
        f"(deny file-read* (subpath {q(home + '/Library/Keychains')}))",
        '(deny file-read* (subpath "/Library/Keychains"))',
        '(deny mach-lookup (global-name "com.apple.SecurityServer"))',
        '(deny mach-lookup (global-name "com.apple.securityd.xpc"))',
        '(deny process-exec (literal "/usr/bin/security"))',
        *[f"(deny file-read* (subpath {q(home + '/' + d)}))" for d in private_dirs],
    ]) + "\n"


def fenced(argv, root, private_dirs):
    if not os.path.exists(SANDBOX_EXEC):
        return argv
    profile = Path(root) / "fence.sb"
    profile.write_text(fence_profile(os.path.realpath(root), private_dirs))
    return [SANDBOX_EXEC, "-f", str(profile), *argv]


class VendorCase(E2ECase):
    """E2ECase whose TUI runs inside the kernel fence. Vendor TUIs have escaped a
    temp HOME before (a JVM resolves the OS user.home; some read the macOS keychain
    whatever HOME is), so the fence, not the TUI's good behavior, keeps the real
    home, keychain and network untouched. ``PRIVATE``: the TUI's real state dirs
    (relative to the real home), unreadable inside the fence."""

    PRIVATE = ()

    def fence(self, argv):
        return fenced(argv, self.root, self.PRIVATE)

    def run_tui(self, argv, env, timeout=120):
        return super().run_tui(self.fence(argv), env, timeout)


# -- Factory Droid ------------------------------------------------------------------------

DROID = _bin("SUBCORTEX_DROID_BIN", "droid")
DROID_MODEL = "custom:Mock-0"  # "custom:<displayName>-<index>"


class DroidRpc:
    """Minimal client for ``droid exec --input-format stream-jsonrpc`` (the protocol
    the interactive TUI and Factory's SDKs use; one-shot ``droid exec "<prompt>"``
    does not run UserPromptSubmit hooks as of 0.224.0)."""

    def __init__(self, argv, env, cwd):
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
        self.inbox = queue.Queue()
        self.stderr = []
        self.pumps = [threading.Thread(target=self._pump, args=(self.proc.stdout, self.inbox.put), daemon=True),
                      threading.Thread(target=self._pump, args=(self.proc.stderr, self.stderr.append), daemon=True)]
        for pump in self.pumps:
            pump.start()
        self.seen = []

    @staticmethod
    def _pump(stream, sink):
        for line in stream:
            sink(line)

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(dict(obj, jsonrpc="2.0", factoryApiVersion="1.0.0")) + "\n")
        self.proc.stdin.flush()

    def _next(self, deadline):
        while time.time() < deadline:
            try:
                line = self.inbox.get(timeout=0.25)
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise AssertionError("droid exited early: " + "".join(self.stderr)[-2000:])
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            self.seen.append(msg)
            if msg.get("method") == "droid.request_permission":  # not expected at autonomy high
                raise AssertionError("unexpected permission request: " + json.dumps(msg)[:500])
            return msg
        raise AssertionError("timed out waiting for droid; last messages: " + json.dumps(self.seen[-3:])[:1500])

    def request(self, method, params, timeout=60):
        rid = uuid.uuid4().hex
        self._send({"type": "request", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while True:
            msg = self._next(deadline)
            if msg.get("id") == rid:
                if "error" in msg:
                    raise AssertionError(f"{method} failed: {msg['error']}")
                return msg.get("result")

    def turn(self, text, timeout=90):
        """Send a user message and wait until the agent turn completes."""
        self.request("droid.add_user_message", {"text": text})
        deadline = time.time() + timeout
        while True:
            note = (self._next(deadline).get("params") or {}).get("notification") or {}
            if note.get("type") == "agent_turn_completed":
                return note

    def close(self, timeout=30):
        self.proc.stdin.close()
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise
        finally:
            self.release()

    def release(self):
        """Kill the process if still running and close its pipes once the pumps hit EOF."""
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        for pump in self.pumps:
            pump.join(5)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


@unittest.skipUnless(DROID, "Factory Droid is not installed (or set SUBCORTEX_DROID_BIN)")
class TestFactoryDroid(VendorCase):
    """BYOK custom model (settings.json ``customModels``, provider
    ``generic-chat-completion-api``): no Factory login. Droid reads stored
    credentials (and only then the macOS keychain) when an ``auth.v2.*`` file
    exists under ``$FACTORY_HOME_OVERRIDE/.factory`` — a fresh temp dir here."""

    PRIVATE = (".factory",)

    def env(self, mock_url):
        home = self.root / "home"
        (home / ".factory").mkdir(parents=True, exist_ok=True)
        (home / ".factory" / "settings.json").write_text(json.dumps({
            "customModels": [{"model": "mock-model", "displayName": "Mock", "baseUrl": mock_url + "/v1",
                              "apiKey": "x", "provider": "generic-chat-completion-api",
                              "maxOutputTokens": 1024}]}))
        xdg = {k: self.root / "xdg" / k for k in ("config", "data", "state", "cache")}
        for d in xdg.values():
            d.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("FACTORY_", "DROID_", "CLAUDE")) and "PROXY" not in k.upper()}
        env.update(self.subcortex_env, **NO_NETWORK, HOME=str(home), FACTORY_HOME_OVERRIDE=str(home),
                   # A droid found on PATH must never replace itself (its binary lives outside the sandbox).
                   FACTORY_DROID_AUTO_UPDATE_ENABLED="false",
                   XDG_CONFIG_HOME=str(xdg["config"]), XDG_DATA_HOME=str(xdg["data"]),
                   XDG_STATE_HOME=str(xdg["state"]), XDG_CACHE_HOME=str(xdg["cache"]),
                   PWD=str(self.root / "work"))
        return env

    def exec_once(self, env, prompt):
        proc = self.run_tui([DROID, "exec", "-m", DROID_MODEL, "--cwd", str(self.root / "work"), "-o", "json",
                             prompt], env)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertFalse(result.get("is_error"), proc.stdout[-1000:])
        return result

    def session(self, env, fence=True):
        argv = [DROID, "exec", "--input-format", "stream-jsonrpc", "--output-format", "stream-jsonrpc",
                "-m", DROID_MODEL, "--cwd", str(self.root / "work")]
        rpc = DroidRpc(self.fence(argv) if fence else argv, env, self.root / "work")
        self.addCleanup(rpc.release)
        rpc.request("droid.initialize_session", {"machineId": "e2e", "cwd": str(self.root / "work"),
                                                 "modelId": DROID_MODEL, "interactionMode": "auto",
                                                 "autonomyLevel": "high"})
        return rpc

    def assert_no_hook_failures(self):
        log = self.root / "data" / "hooks.log"
        self.assertFalse(log.exists(), log.read_text() if log.exists() else "")

    def test_installed_hooks_file_matches_what_droid_loads(self):
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("droid", env)
            hooks = json.loads((self.root / "home" / ".factory" / "hooks.json").read_text())
            # Top-level event map (no "hooks" wrapper); no PostToolUse (Droid can't replace output).
            self.assertEqual(set(hooks), {"UserPromptSubmit", "PreCompact", "SessionStart"})

    def test_hint_reaches_the_model(self):
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("droid", env)
            rpc = self.session(env)
            self.assertEqual(rpc.turn("what is 2+2?").get("reason"), "completed")
            self.assertEqual(rpc.close(), 0, "".join(rpc.stderr)[-2000:])
            first = json.dumps(llm.agent_requests()[0])
            self.assertIn(HINT, first, "the UserPromptSubmit hint did not reach the model")
            self.assertEqual(first.count(HINT), 1)
            self.assert_no_hook_failures()

    def test_shell_output_reaches_the_model_untouched(self):
        # Droid's PostToolUse can only block or append (0.224.0 parses no
        # output-replacement field), so subcortex registers none.
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("droid", env)
            llm.script = [{"tool": "Execute", "input": {
                "command": "yes 'compiling module ok' | head -1500; echo BUILD-TAIL-OK",
                "riskLevel": "low", "riskLevelReason": "prints text"}}, {"text": "Done."}]
            # Unfenced: Droid 0.224.0's Execute is SIGKILLed under any sandbox-exec profile (even
            # an allow-all one). Isolation here rests on HOME/FACTORY_HOME_OVERRIDE, which keep
            # all of Droid's state in the temp dir (and no auth.v2 file = no keychain read).
            rpc = self.session(env, fence=False)
            self.assertEqual(rpc.turn("run the build").get("reason"), "completed")
            self.assertEqual(rpc.close(), 0, "".join(rpc.stderr)[-2000:])
            requests = llm.agent_requests()
            self.assertIn(HINT, json.dumps(requests[0]))
            tool_turn = json.dumps(requests[1])
            # Droid 0.224.0 cuts big Execute output itself (head + tail, "[... N lines skipped ...]").
            self.assertIn("BUILD-TAIL-OK", tool_turn)
            self.assertIn("[Process exited with code 0]", tool_turn)
            self.assertNotIn(TRIMMED, tool_turn)
            self.assert_no_hook_failures()

    def test_compaction_snapshot_survives_compact(self):
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("droid", env)
            rpc = self.session(env)
            rpc.turn("remember the codeword PELICAN-42")
            compacted = rpc.request("droid.compact_session", {}, timeout=90)
            self.assertTrue(compacted.get("newSessionId"), compacted)
            # Switching to the successor session (what the TUI does after /compact) fires
            # SessionStart source=compact with previous_session_id = the compacted session.
            rpc.request("droid.load_session", {"sessionId": compacted["newSessionId"]})
            before = len(llm.requests)
            rpc.turn("what was the codeword?")
            self.assertEqual(rpc.close(), 0, "".join(rpc.stderr)[-2000:])
            after = json.dumps([r["body"] for r in llm.requests[before:]])
            self.assertIn(RESTORED, after, "the SessionStart(compact) restore did not reach the model")
            self.assertIn("PELICAN-42", after)
            self.assert_no_hook_failures()

    def test_daemon_down_does_not_block(self):
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("droid", env)
            env["SUBCORTEX_PORT"] = "1"  # nothing listens there
            self.assertEqual(self.exec_once(env, "say hi")["result"], "OK")  # one-shot path
            rpc = self.session(env)  # the path where UserPromptSubmit runs
            self.assertEqual(rpc.turn("say hi").get("reason"), "completed")
            self.assertEqual(rpc.close(), 0, "".join(rpc.stderr)[-2000:])
            self.assertEqual(len(llm.agent_requests()), 2)
            self.assertNotIn("[subcortex]", llm.all_text())
            self.assert_no_hook_failures()


# -- GitHub Copilot CLI -------------------------------------------------------------------

COPILOT = _bin("SUBCORTEX_COPILOT_BIN", "copilot")
COPILOT_TAIL = "build finished: 0 errors"
COPILOT_BIG = f"yes 'compiling module ok' | head -900; echo '{COPILOT_TAIL}'"    # ~18 KB: under Copilot's spill
COPILOT_HUGE = f"yes 'compiling module ok' | head -1500; echo '{COPILOT_TAIL}'"  # ~30 KB: Copilot spills it
_GITHUB_SECRETS = ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                   "GITHUB_ENTERPRISE_TOKEN", "GH_HOST")


@unittest.skipUnless(COPILOT, "GitHub Copilot CLI is not installed (or set SUBCORTEX_COPILOT_BIN)")
class TestCopilotCli(VendorCase):
    """BYOK: with COPILOT_PROVIDER_BASE_URL set no GitHub login is needed, and
    COPILOT_OFFLINE=true skips every other network call (auth, telemetry, GitHub
    MCP, auto-update). Model traffic is OpenAI Chat Completions to the mock.
    Copilot spills tool output > 20 KiB to a file *before* postToolUse runs (the
    hook sees a ~1 KB preview), so the trim case stays under that."""

    PRIVATE = (".copilot", ".config/gh", ".config/github-copilot")

    def env(self, mock_url):
        dirs = {k: self.root / k for k in ("home", "copilot", "cache", "cfg", "xdata", "state", "gh")}
        for d in dirs.values():
            d.mkdir(exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k not in _GITHUB_SECRETS and not k.startswith("COPILOT_")}
        env.update(self.subcortex_env, **NO_NETWORK, HOME=str(dirs["home"]), COPILOT_HOME=str(dirs["copilot"]),
                   COPILOT_CACHE_HOME=str(dirs["cache"]), XDG_CACHE_HOME=str(dirs["cache"]),
                   XDG_CONFIG_HOME=str(dirs["cfg"]), XDG_DATA_HOME=str(dirs["xdata"]),
                   XDG_STATE_HOME=str(dirs["state"]), GH_CONFIG_DIR=str(dirs["gh"]),
                   COPILOT_AUTO_UPDATE="false", COPILOT_OFFLINE="true",
                   COPILOT_PROVIDER_BASE_URL=mock_url + "/v1", COPILOT_PROVIDER_TYPE="openai",
                   COPILOT_PROVIDER_API_KEY="x", COPILOT_MODEL="mock-model")
        return env

    def copilot(self, env, prompt, *extra):
        proc = self.run_tui([COPILOT, "-p", prompt, "--allow-all-tools", "--no-auto-update", "-s", *extra],
                            env, timeout=110)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        return proc

    def setup_copilot(self, llm):
        env = self.env(llm.url)
        self.install("copilot", env)
        self.assertTrue((self.root / "copilot" / "hooks" / "subcortex.json").is_file())
        return env

    def test_hint_and_trim_reach_the_model(self):
        with MockLLM() as llm:
            env = self.setup_copilot(llm)
            llm.script = [{"tool": "bash", "input": {"command": COPILOT_BIG, "description": "build"}},
                          {"text": "Done."}]
            proc = self.copilot(env, "run the build")
            self.assertIn("Done.", proc.stdout)
            requests = llm.agent_requests()
            self.assertGreaterEqual(len(requests), 2, llm.all_text()[-2000:])
            self.assertIn(HINT, json.dumps(requests[0]), "the userPromptSubmitted hint did not reach the model")
            tool_turn = json.dumps(requests[1]["messages"][-1])
            self.assertIn(TRIMMED, tool_turn, "the postToolUse modifiedResult trim did not reach the model")
            self.assertIn(COPILOT_TAIL, tool_turn)                         # the tail survived
            self.assertLess(tool_turn.count("compiling module ok"), 200)  # the middle did not

    def test_copilots_own_spill_preview_is_left_alone(self):
        with MockLLM() as llm:
            env = self.setup_copilot(llm)
            llm.script = [{"tool": "bash", "input": {"command": COPILOT_HUGE, "description": "build"}},
                          {"text": "Done."}]
            self.copilot(env, "run the build")
            tool_turn = json.dumps(llm.agent_requests()[1]["messages"][-1])
            self.assertIn("Output too large to read at once", tool_turn)
            self.assertNotIn(TRIMMED, tool_turn)

    def test_daemon_down_does_not_block(self):
        with MockLLM() as llm:
            env = self.setup_copilot(llm)
            env["SUBCORTEX_PORT"] = "1"  # nothing listens there
            llm.script = [{"tool": "bash", "input": {"command": COPILOT_BIG, "description": "build"}},
                          {"text": "Done."}]
            proc = self.copilot(env, "run the build")
            self.assertIn("Done.", proc.stdout)
            self.assertEqual(proc.stderr.strip(), "")
            self.assertNotIn("[subcortex", llm.all_text())
            tool_turn = json.dumps(llm.agent_requests()[1]["messages"][-1])
            self.assertIn(COPILOT_TAIL, tool_turn)                        # untouched output reached the model
            self.assertEqual(tool_turn.count("compiling module ok"), 900)

    def test_compaction_snapshot_rides_the_next_prompt(self):
        # `-p /compact` on a resumed session fires preCompact (trigger "manual", with transcriptPath).
        with MockLLM() as llm:
            env = self.setup_copilot(llm)
            session = str(uuid.uuid4())
            self.copilot(env, "remember the codeword PELICAN-42", "--session-id", session)
            proc = self.copilot(env, "/compact", "--resume", session)
            self.assertIn("Compacted conversation", proc.stdout)
            before = len(llm.requests)
            self.copilot(env, "what was the codeword?", "--resume", session)
            after = json.dumps([r["body"] for r in llm.requests[before:]])
            self.assertIn(RESTORED, after)
            self.assertIn("PELICAN-42", after)


# -- OpenHands CLI ------------------------------------------------------------------------

OPENHANDS = _bin("SUBCORTEX_OPENHANDS_BIN", "openhands")


def _terminal(command):
    # OpenHands' terminal tool rejects calls without security_risk.
    return {"tool": "terminal", "input": {"command": command, "security_risk": "LOW", "summary": "run"}}


@unittest.skipUnless(OPENHANDS, "OpenHands CLI is not installed (or set SUBCORTEX_OPENHANDS_BIN)")
class TestOpenHands(VendorCase):
    """Headless, LLM settings from env (--override-with-envs, LiteLLM base URL).
    Settings, conversations and the user hooks file live under $HOME/.openhands."""

    PRIVATE = (".openhands",)

    def env(self, mock_url):
        home = self.root / "home"
        home.mkdir(exist_ok=True)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("LLM_", "OPENAI", "ANTHROPIC", "OPENHANDS", "LITELLM"))}
        env.update(self.subcortex_env, **NO_NETWORK, HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
                   XDG_DATA_HOME=str(home / ".local" / "share"), XDG_CACHE_HOME=str(home / ".cache"),
                   LLM_MODEL="openai/mock-model", LLM_BASE_URL=mock_url + "/v1", LLM_API_KEY="x",
                   LITELLM_LOCAL_MODEL_COST_MAP="True", OPENHANDS_SUPPRESS_BANNER="1",
                   PWD=str(self.root / "work"))
        return env

    def openhands(self, env, task, *extra):
        return self.run_tui([OPENHANDS, "--headless", "--json", "--override-with-envs", *extra, "-t", task],
                            env, timeout=150)

    @staticmethod
    def hook_runs(stdout):
        return [json.loads(line) for line in stdout.splitlines()
                if line.startswith("{") and '"HookExecutionEvent"' in line]

    def test_hint_reaches_the_model_and_tool_output_is_untouched(self):
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("openhands", env)
            hooks = json.loads((self.root / "home" / ".openhands" / "hooks.json").read_text())
            self.assertEqual(list(hooks), ["user_prompt_submit"])
            # 33 KB: over OpenHands' own 30,000-char cut. subcortex registers no
            # PostToolUse here (its output is ignored), so the output is OpenHands' alone.
            llm.script = [_terminal("yes 'compiling module ok' | head -1500; echo build-finished"),
                          {"text": "Done."}]
            proc = self.openhands(env, "run the build")
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            requests = llm.agent_requests()
            self.assertIn(HINT, json.dumps(requests[0]), "the UserPromptSubmit hint did not reach the model")
            tool_turn = json.dumps(requests[1]["messages"][-1])
            self.assertIn("build-finished", tool_turn)
            self.assertNotIn(TRIMMED, tool_turn)
            runs = self.hook_runs(proc.stdout)
            self.assertEqual([r["hook_event_type"] for r in runs], ["UserPromptSubmit"])
            self.assertTrue(runs[0]["success"] and not runs[0]["blocked"], runs[0])
            self.assertIn(HINT, runs[0]["additional_context"])

    def test_condensed_messages_come_back_with_the_next_prompt(self):
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("openhands", env)
            # 1: an opening turn. keep_first=4 keeps its events out of any condensation.
            llm.script = [_terminal("echo hello"), {"text": "OK"}]
            proc = self.openhands(env, "hello")
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            conversation = re.search(r"Conversation ID: ([0-9a-f]{32})", proc.stdout).group(1)
            # 2: the codeword, then enough tool calls to pass the default condenser's
            # max_size (80 events): the codeword prompt is summarized away.
            llm.script = [_terminal(f"echo step {i}") for i in range(42)] + [{"text": "Done."}]
            proc = self.openhands(env, "the codeword is PELICAN-42. now run the checks", "--resume", conversation)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            events = self.root / "home" / ".openhands" / "conversations" / conversation / "events"
            kinds = [json.loads(p.read_text()).get("kind") for p in sorted(events.glob("event-*.json"))]
            self.assertIn("Condensation", kinds, "OpenHands did not condense; the test can't check the restore")
            # 3: the next prompt carries the hidden messages back.
            before = len(llm.requests)
            proc = self.openhands(env, "what was the codeword?", "--resume", conversation)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            first = next(r["body"] for r in llm.requests[before:] if r["body"].get("tools"))
            restored = [json.dumps(m) for m in first["messages"] if RESTORED in json.dumps(m)]
            self.assertEqual(len(restored), 1, "the restore did not reach the model")
            self.assertIn("PELICAN-42", restored[0])
            # ...and nothing else the model saw still had it (it really was condensed away).
            others = [json.dumps(m) for m in first["messages"] if RESTORED not in json.dumps(m)]
            self.assertFalse(any("PELICAN-42" in m for m in others))

    def test_daemon_down_does_not_block(self):
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("openhands", env)
            env["SUBCORTEX_PORT"] = "1"  # nothing listens there
            proc = self.openhands(env, "say hi")
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertIn('"text": "OK"', proc.stdout)  # the agent's reply event
            self.assertNotIn("[subcortex]", llm.all_text())
            runs = self.hook_runs(proc.stdout)
            self.assertEqual(len(runs), 1)
            self.assertEqual((runs[0]["exit_code"], runs[0]["blocked"], runs[0]["stdout"], runs[0]["stderr"]),
                             (0, False, "", ""))


# -- Letta Code ---------------------------------------------------------------------------

LETTA = _bin("SUBCORTEX_LETTA_BIN", "letta")
LETTA_MODEL = "openai-compatible/mock-model"


@unittest.skipUnless(LETTA, "Letta Code is not installed (or set SUBCORTEX_LETTA_BIN)")
class TestLettaCode(VendorCase):
    """Letta Code >= 0.32 on its local backend (``--backend local``) with a BYOK
    openai-compatible provider: no Letta Cloud account or server. UserPromptSubmit
    hooks only run in the interactive TUI (``letta -p`` never calls them), so the
    TUI is driven in a pseudo-terminal."""

    PRIVATE = (".letta",)

    def env(self):
        home = self.root / "home"
        home.mkdir(exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LETTA_", "SUBCORTEX_"))}
        env.update(self.subcortex_env, **NO_NETWORK, HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
                   XDG_DATA_HOME=str(home / ".local/share"), XDG_CACHE_HOME=str(home / ".cache"),
                   XDG_STATE_HOME=str(home / ".local/state"), PWD=str(self.root / "work"),
                   LETTA_SKIP_KEYCHAIN_CHECK="1", LETTA_TEST_SECRETS_SERVICE_PREFIX="subcortex-e2e",
                   DISABLE_AUTOUPDATER="1", LETTA_CODE_TELEM="0", DO_NOT_TRACK="1",
                   TERM="xterm-256color", COLUMNS="120", LINES="40")
        return env

    def prepare(self, llm):
        env = self.env()
        self.install("letta", env)
        settings = json.loads((self.root / "home" / ".letta" / "settings.json").read_text())
        self.assertEqual(list(settings["hooks"]), ["UserPromptSubmit"])  # nothing else is registered
        proc = self.run_tui([LETTA, "--backend", "local", "connect", "openai-compatible",
                             "--base-url", llm.url + "/v1", "--api-key", "x"], env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout[-1000:] + proc.stderr[-1000:])
        return env

    def run_interactive(self, env, prompt, llm, *extra, requests=1, timeout=90):
        """Type ``prompt`` into the TUI, wait until the model got ``requests`` agent
        requests and the reply was rendered, then /exit. Returns (exit code, raw screen)."""
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        argv = self.fence([LETTA, "--backend", "local", "-m", LETTA_MODEL, "--new-agent", *extra])
        proc = subprocess.Popen(argv, cwd=self.root / "work", env=env, stdin=slave, stdout=slave, stderr=slave,
                                start_new_session=True, close_fds=True)
        os.close(slave)
        screen = bytearray()
        deadline = time.time() + timeout

        def pump(seconds, until=None):
            end = min(time.time() + seconds, deadline)
            while time.time() < end:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        return False
                    if not chunk:
                        return False
                    screen.extend(chunk)
                if until is not None and until():
                    return True
            return until is None

        try:
            # The input placeholder (Try "...", first letter under the cursor) shows once the agent is ready.
            self.assertTrue(pump(60, lambda: b'ry "' in screen), bytes(screen[-2000:]))
            pump(1.0)
            os.write(master, prompt.encode())
            pump(1.0)
            os.write(master, b"\r")
            got = pump(60, lambda: len(llm.agent_requests()) >= requests and not llm.script)
            self.assertTrue(got, f"only {len(llm.agent_requests())} agent requests reached the model")
            pump(3.0)  # let the reply render
            self.assertIsNone(proc.poll(), "letta exited mid-turn")
            os.write(master, b"/exit")
            pump(1.0)
            os.write(master, b"\r")
            pump(15, lambda: proc.poll() is not None)  # returns early once the pty closes
            return proc.wait(timeout=15), bytes(screen)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            os.close(master)

    def test_hint_reaches_the_model(self):
        with MockLLM() as llm:
            env = self.prepare(llm)
            code, screen = self.run_interactive(env, "what is 2+2?", llm)
            self.assertEqual(code, 0, screen[-2000:])
            content = llm.agent_requests()[0]["messages"][-1]["content"]
            parts = [p.get("text", "") for p in content] if isinstance(content, list) else [content]
            self.assertEqual(parts[-1], "what is 2+2?")
            # Plain stdout, wrapped by Letta in its own reminder, just before the prompt.
            self.assertTrue(parts[-2].startswith("<system-reminder>\n" + HINT), parts[-2][:300])
            self.assertNotIn(b"[subcortex]", screen)  # quiet: true -> not echoed in the TUI

    def test_tool_output_is_left_alone(self):
        # No Letta hook can replace a tool result (PostToolUse only appends), so
        # subcortex registers none: shell output must reach the model untouched.
        with MockLLM() as llm:
            env = self.prepare(llm)
            llm.script = [{"tool": "Bash", "input": {"command": "yes 'compiling module ok' | head -1000; "
                                                                "echo BUILD-TAIL-OK", "description": "build"}},
                          {"text": "Done."}]
            code, screen = self.run_interactive(env, "run the build", llm, "--yolo", requests=2)
            self.assertEqual(code, 0, screen[-2000:])
            tool_turn = json.dumps(llm.agent_requests()[1]["messages"][-1])
            self.assertIn("BUILD-TAIL-OK", tool_turn)
            self.assertEqual(tool_turn.count("compiling module ok"), 1000)  # under Letta's own 30k cut
            self.assertNotIn(TRIMMED, tool_turn)
            self.assertNotIn("[Hook feedback]", tool_turn)

    def test_daemon_down_does_not_block(self):
        with MockLLM() as llm:
            env = self.prepare(llm)
            env.update(SUBCORTEX_PORT="1", SUBCORTEX_DEBUG="1")  # nothing listens there; log every hook run
            code, screen = self.run_interactive(env, "say hi", llm)
            self.assertEqual(code, 0, screen[-2000:])
            self.assertEqual(len(llm.agent_requests()), 1)
            self.assertIn("say hi", json.dumps(llm.agent_requests()[0]["messages"][-1]))
            self.assertNotIn("[subcortex]", llm.all_text())
            self.assertNotIn("Hook error", llm.all_text())
            log = (self.root / "data" / "hooks.log").read_text()
            self.assertIn("letta UserPromptSubmit ok", log)  # the hook did run, and exited cleanly


# -- CodeBuddy Code -----------------------------------------------------------------------

CODEBUDDY = _bin("SUBCORTEX_CODEBUDDY_BIN", "codebuddy")
# 400 distinct lines (~10 KB): over subcortex's 6000-char floor, under CodeBuddy's
# own spill-to-file limit (bigger outputs reach the model as a 2 KB preview anyway).
CODEBUDDY_BUILD = "for i in $(seq 1 400); do echo \"compiling module $i ok\"; done"


@unittest.skipUnless(CODEBUDDY, "CodeBuddy Code is not installed (or set SUBCORTEX_CODEBUDDY_BIN)")
class TestCodeBuddy(VendorCase):
    """A ``~/.codebuddy/models.json`` custom model (OpenAI Chat Completions, ``url`` =
    the mock) runs ``codebuddy -p`` end to end without a Tencent login."""

    PRIVATE = (".codebuddy",)

    def env(self, mock_url):
        home = self.root / "home"
        (home / ".codebuddy").mkdir(parents=True, exist_ok=True)
        (home / ".codebuddy" / "models.json").write_text(json.dumps({
            "models": [{"id": "mock-model", "name": "Mock", "vendor": "OpenAI", "apiKey": "x",
                        "url": mock_url + "/v1/chat/completions", "maxInputTokens": 200000,
                        "maxOutputTokens": 4096, "supportsToolCall": True}],
            "availableModels": ["mock-model"]}))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CODEBUDDY_", "OPENAI_", "ANTHROPIC_")) and "PROXY" not in k.upper()}
        env.update(self.subcortex_env, **NO_NETWORK, HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
                   XDG_CACHE_HOME=str(home / ".cache"), XDG_DATA_HOME=str(home / ".local" / "share"),
                   XDG_STATE_HOME=str(home / ".local" / "state"), DISABLE_AUTOUPDATER="1")
        return env

    def codebuddy(self, env, *args):
        proc = self.run_tui([CODEBUDDY, "-p", *args, "--model", "mock-model", "--output-format", "json", "-y"],
                            env, timeout=110)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        result = json.loads(proc.stdout)[-1]
        self.assertFalse(result.get("is_error"), result)
        return proc, result

    def test_hint_reaches_the_model(self):
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("codebuddy", env)
            _, result = self.codebuddy(env, "what is 2+2?")
            self.assertEqual(result["result"], "OK")
            self.assertIn(HINT, json.dumps(llm.agent_requests()[0]),
                          "the UserPromptSubmit hint did not reach the model")

    def test_prompt_is_remembered_for_the_output_judge(self):
        session = "5e1d2c3b-4a59-4687-9a0b-1c2d3e4f5a6b"
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("codebuddy", env)
            self.codebuddy(env, "run the build", "--session-id", session)
        with mock.patch.dict(os.environ, self.subcortex_env):
            self.assertEqual(policy.last_prompt(session, tui="codebuddy"), "run the build")

    @unittest.skip("CodeBuddy's PostToolUse carries no output, so subcortex doesn't register it")
    def test_trim_reaches_the_model(self):
        # Fails on CodeBuddy 2.156.0: its PostToolUse tool_response for Bash is the tool's
        # rawResponse — {exitCode, signal, interrupted, sandboxDenied, stderrBytesTruncated,
        # stdoutBytesTruncated, tool_error_code} — with no stdout, so there is nothing to
        # trim. (A string updatedToolOutput does replace what the model sees.) An
        # "unexpected success" here means CodeBuddy started sending the output.
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("codebuddy", env)
            llm.script = [{"tool": "Bash", "input": {"command": CODEBUDDY_BUILD, "description": "build"}},
                          {"text": "Done."}]
            _, result = self.codebuddy(env, "run the build")
            self.assertEqual(result["result"], "Done.")
            tool_turn = json.dumps(llm.agent_requests()[1])
            self.assertIn(TRIMMED, tool_turn, "the PostToolUse trim did not reach the model")
            self.assertIn("compiling module 400 ok", tool_turn)      # the tail survived
            self.assertNotIn("compiling module 200 ok", tool_turn)   # the middle did not

    def test_shell_output_is_left_intact(self):
        # What the model gets today: the untouched output (the PostToolUse hook runs and says nothing).
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("codebuddy", env)
            llm.script = [{"tool": "Bash", "input": {"command": CODEBUDDY_BUILD, "description": "build"}},
                          {"text": "Done."}]
            _, result = self.codebuddy(env, "run the build")
            self.assertEqual(result["result"], "Done.")
            tool_turn = json.dumps(llm.agent_requests()[1])
            self.assertIn("compiling module 200 ok", tool_turn)
            self.assertNotIn(TRIMMED, tool_turn)

    def test_hooks_never_block_even_with_the_daemon_down(self):
        with MockLLM() as llm:
            env = self.env(llm.url)
            self.install("codebuddy", env)
            env["SUBCORTEX_PORT"] = "1"  # nothing listens there
            llm.script = [{"tool": "Bash", "input": {"command": CODEBUDDY_BUILD, "description": "build"}},
                          {"text": "Done."}]
            proc, result = self.codebuddy(env, "run the build")
            self.assertEqual(result["result"], "Done.")
            self.assertEqual(len(llm.agent_requests()), 2)
            self.assertNotIn("[subcortex]", llm.all_text())
            self.assertNotIn("hook", proc.stderr.lower())

    def test_compaction_snapshot_survives_auto_compact(self):
        # A /compact in -p mode fires PreCompact and PostCompact but never SessionStart(compact)
        # (2.156.0), so the restore path is exercised through auto-compaction: the mock reports a
        # nearly full window, CodeBuddy compacts before the next model call, fires
        # SessionStart(compact) with the same session_id and continues the turn.
        with UsageMockLLM() as llm:
            llm.prompt_tokens = 195000  # of maxInputTokens 200000
            env = self.env(llm.url)
            self.install("codebuddy", env)
            llm.script = [{"tool": "Bash", "input": {"command": "echo hi", "description": "echo"}},
                          {"text": "Done."}]
            self.codebuddy(env, "remember the codeword PELICAN-42 and run echo hi")
            summary = next(i for i, r in enumerate(llm.requests) if not r["body"].get("tools"))  # the compaction
            after = "\n".join(json.dumps(r["body"]) for r in llm.requests[summary + 1:])
            self.assertIn(RESTORED, after)
            self.assertIn("PELICAN-42", after)


def _screen_text(raw):
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07", "", raw.decode(errors="replace"))


# -- Cursor CLI ---------------------------------------------------------------------------

def _cursor_bin():
    found = _bin("SUBCORTEX_CURSOR_BIN", "cursor-agent")
    if not found:
        return None
    try:
        bundle = (Path(os.path.realpath(found)).parent / "index.js").read_bytes()
    except OSError:
        return None
    # Only a build whose credential store can be forced to memory (no keychain) is safe to run.
    return found if b"AGENT_CLI_CREDENTIAL_STORE" in bundle and b'"memory"===' in bundle else None


CURSOR = _cursor_bin()
CURSOR_PROMPT = "what is 2+2?"
CURSOR_PRIVATE = (".cursor", "Library/Application Support/Cursor", ".local/share/cursor-agent")


@unittest.skipUnless(CURSOR, "Cursor CLI not found (set SUBCORTEX_CURSOR_BIN) or its build "
                             "can't be kept off the keychain")
class TestCursorCli(VendorCase):
    """The public Cursor CLI has no custom-model mode (``--base-url`` is refused outside
    agent-cli-local builds), so no turn completes. What is proven, with the real
    interactive CLI against tests/e2e/mock_cursor.py (``CURSOR_API_ENDPOINT``): it
    loads subcortex's ~/.cursor/hooks.json, runs beforeSubmitPrompt on a submitted
    prompt, and the hook's additional_context is in the turn request it sends.
    ``AGENT_CLI_CREDENTIAL_STORE=memory`` keeps it off the macOS keychain (the default
    store reads the real login from it regardless of HOME); on macOS it also runs
    inside a sandbox-exec fence."""

    PRIVATE = CURSOR_PRIVATE

    def env(self, backend_url):
        home = self.root / "home"
        (home / ".cursor").mkdir(parents=True, exist_ok=True)
        # HTTP/1.1 for the agent stream, so the stdlib mock can read it.
        (home / ".cursor" / "cli-config.json").write_text(
            '{"version": 1, "network": {"useHttp1ForAgent": true}, "permissions": {"allow": [], "deny": []}}')
        return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(home), "SHELL": "/bin/sh",
                "XDG_CONFIG_HOME": str(self.root / "xdg"), "XDG_DATA_HOME": str(self.root / "xdg" / "data"),
                "XDG_CACHE_HOME": str(self.root / "xdg" / "cache"), "CURSOR_CONFIG_DIR": str(home / ".cursor"),
                "AGENT_CLI_CREDENTIAL_STORE": "memory", "CURSOR_API_ENDPOINT": backend_url,
                "CURSOR_AUTH_TOKEN": "e2e-fake-token", "CURSOR_AGENT_CLI_AUTHLESS_MODE": "true",
                "TERM": "xterm-256color", "COLUMNS": "120", "LINES": "40", "SUBCORTEX_DEBUG": "1",
                **NO_NETWORK, **self.subcortex_env}

    def submit(self, backend, env, prompt=CURSOR_PROMPT, timeout=60):
        """Start the interactive CLI in a pty, submit ``prompt``; return (screen, agent messages)."""
        argv = self.fence([CURSOR, "--trust", "--workspace", str(self.root / "work")])
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(self.root / "work")
                os.execve(argv[0], argv, env)
            finally:
                os._exit(127)
        screen = b""
        deadline = time.time() + timeout

        def pump(seconds, until=None):
            nonlocal screen
            end = min(time.time() + seconds, deadline)
            while time.time() < end:
                if until and until():
                    return True
                ready, _, _ = select.select([fd], [], [], 0.2)
                if ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        return False
                    if not chunk:
                        return False
                    screen += chunk
            return bool(until and until())

        try:
            self.assertTrue(pump(40, lambda: b"Mock" in screen), "the CLI never showed the mock model:\n"
                            + _screen_text(screen)[-1500:])
            pump(5)  # the input box takes a moment to accept keys after the model shows up
            for ch in prompt:
                os.write(fd, ch.encode())
                time.sleep(0.03)
            pump(1.5)
            os.write(fd, b"\r")
            pump(30, lambda: any(prompt.encode() in m for m in backend.agent_messages()))
        finally:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
            os.waitpid(pid, 0)
            os.close(fd)
        return _screen_text(screen), backend.agent_messages()

    def hooks_log(self):
        path = self.root / "data" / "hooks.log"
        return path.read_text() if path.exists() else ""

    def test_prompt_hint_reaches_the_turn_request(self):
        with MockCursorBackend() as backend:
            env = self.env(backend.url)
            self.install("cursor", env)
            screen, messages = self.submit(backend, env)
            turn = [m for m in messages if CURSOR_PROMPT.encode() in m]
            self.assertTrue(turn, f"no turn request reached the backend; paths={backend.paths()}\n{screen[-1500:]}")
            self.assertIn("cursor beforeSubmitPrompt ok", self.hooks_log())
            self.assertIn(HINT.encode(), turn[0], "the beforeSubmitPrompt additional_context is not in the turn")
            self.assertIn(b"beforeSubmitPrompt", turn[0])  # sent as a hook additional-context carrier

    def test_daemon_down_still_submits_without_errors(self):
        with MockCursorBackend() as backend:
            env = self.env(backend.url)
            self.install("cursor", env)
            env["SUBCORTEX_PORT"] = "1"  # nothing listens there
            screen, messages = self.submit(backend, env)
            turn = [m for m in messages if CURSOR_PROMPT.encode() in m]
            self.assertTrue(turn, f"the prompt was not submitted; paths={backend.paths()}\n{screen[-1500:]}")
            self.assertNotIn(b"[subcortex]", b"".join(messages))
            self.assertNotIn("blocked", screen.lower())
            log = self.hooks_log()
            self.assertIn("cursor beforeSubmitPrompt ok", log)
            self.assertNotIn("failed", log)


# -- Amp ------------------------------------------------------------------------------------

AMP = _bin("SUBCORTEX_AMP_BIN", "amp")
AMP_THREAD = "T-5f0c8a3e-2b1d-4e7a-9c6b-0d4e8f2a1b3c"
AMP_PROMPT = "what is 2+2?"
AMP_BIG = "".join(f"compiling module {i} ok\n" for i in range(1, 1501))
AMP_PRIVATE = (".config/amp", ".local/share/amp", ".cache/amp")

# Loads subcortex's plugin through a proxied PluginAPI inside Amp's own plugin
# process and calls its handlers with the event shapes the newest Amp sends.
AMP_HARNESS = """\
import subcortex from %(plugin)s
import { appendFileSync } from "node:fs"

const OUT = %(out)s
const EVENTS: Array<[string, any]> = %(events)s
const log = (x: unknown) => appendFileSync(OUT, JSON.stringify(x) + "\\n")

export default function (amp: any) {
  const handlers: Record<string, any> = {}
  const proxy = new Proxy(amp, {
    get(target, key) {
      if (key === "on") return (event: string, handler: any) => { handlers[event] = handler; return { unsubscribe() {} } }
      const value = target[key]
      return typeof value === "function" ? value.bind(target) : value
    },
  })
  subcortex(proxy)
  amp.on("session.start", async (_event: any, ctx: any) => {
    log({ registered: Object.keys(handlers), executor: amp.system.executor.kind })
    for (const [name, payload] of EVENTS) {
      try {
        log({ event: name, result: (await handlers[name]?.(payload, ctx)) ?? null })
      } catch (error) {
        log({ event: name, error: String(error) })
      }
    }
  })
}
"""


@unittest.skipUnless(AMP, "Amp is not installed (or set SUBCORTEX_AMP_BIN)")
class TestAmpPlugin(VendorCase):
    """Amp can't run a turn without an Amp account: the agent loop runs on the Amp
    server and the CLI only executes tools and plugins for it. What runs locally is
    the plugin host, so these tests use the real Amp binary to load the plugin the
    installer wrote (``amp plugins list``, with tests/e2e/mock_amp.py standing in for
    the server's loadPlugins call) and to run it in Amp's plugin process (``amp
    plugins exec``) with the newest event shapes (Bash input ``{cmd, cwd}``, output
    ``{output, exitCode}``; a non-zero exit is still ``status: "done"``)."""

    PRIVATE = AMP_PRIVATE

    def env(self, server_url):
        return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(self.root / "home"),
                "XDG_CONFIG_HOME": str(self.root / "xdg"), "XDG_DATA_HOME": str(self.root / "xdg" / "data"),
                "XDG_CACHE_HOME": str(self.root / "xdg" / "cache"), "AMP_URL": server_url,
                "AMP_SKIP_UPDATE_CHECK": "1", "TERM": "dumb", "NO_COLOR": "1", **NO_NETWORK, **self.subcortex_env}

    def amp(self, args, env):
        return subprocess.run(self.fence([AMP, *args]), cwd=self.root / "work", env=env,
                              capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL)

    def plugin_path(self):
        return self.root / "xdg" / "amp" / "plugins" / "subcortex.ts"

    def run_handlers(self, env, events):
        out = self.root / "harness.jsonl"
        harness = self.root / "harness" / "harness.ts"
        harness.parent.mkdir(exist_ok=True)
        harness.write_text(AMP_HARNESS % {"plugin": json.dumps(str(self.plugin_path())),
                                          "out": json.dumps(str(out)), "events": json.dumps(events)})
        proc = self.amp(["plugins", "exec", str(harness), "session.start",
                         "--data", json.dumps({"thread": {"id": AMP_THREAD}})], env)
        self.assertEqual(proc.returncode, 0, proc.stdout[-1500:] + proc.stderr[-1500:])
        lines = [json.loads(line) for line in out.read_text().splitlines()] if out.exists() else []
        self.assertTrue(lines, "the harness never ran:\n" + proc.stdout[-1500:] + proc.stderr[-1500:])
        self.assertEqual(sorted(lines[0]["registered"]), ["agent.end", "agent.start", "session.start", "tool.result"])
        self.assertEqual(lines[0]["executor"], "local")
        return {r["event"]: r for r in lines[1:]}, proc

    @staticmethod
    def shell_result(exit_code, output=AMP_BIG):
        return {"thread": {"id": AMP_THREAD}, "toolUseID": "toolu_01", "tool": "Bash",
                "input": {"cmd": "./build.sh", "cwd": "/tmp"}, "status": "done",
                "output": {"output": output, "exitCode": exit_code}}

    def test_plugin_loads_in_amp(self):
        with MockAmpServer() as server:
            env = self.env(server.url)
            self.install("amp", env)
            proc = self.amp(["plugins", "list"], env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
            self.assertIn(f"{self.plugin_path()} active", proc.stdout)
            self.assertIn("events: session.start, agent.start, tool.result, agent.end", proc.stdout)

    def test_hint_and_trim_from_the_amp_plugin_runtime(self):
        with MockAmpServer() as server:
            env = self.env(server.url)
            self.install("amp", env)
            results, _ = self.run_handlers(env, [
                ["agent.start", {"thread": {"id": AMP_THREAD}, "message": AMP_PROMPT, "id": 1}],
                ["tool.result", self.shell_result(0)],
            ])
            start = results["agent.start"]["result"]
            self.assertIn(HINT, start["message"]["content"])
            self.assertIs(start["message"]["display"], False)
            done = results["tool.result"]["result"]
            self.assertEqual(done["status"], "done")
            self.assertEqual(done["output"]["exitCode"], 0)  # the result's other fields are kept
            text = done["output"]["output"]
            self.assertIn(TRIMMED, text)
            self.assertIn("compiling module 1500 ok", text)     # the tail survived
            self.assertNotIn("compiling module 750 ok", text)   # the middle did not

    def test_failed_command_output_is_left_alone(self):
        with MockAmpServer() as server:
            env = self.env(server.url)
            self.install("amp", env)
            results, _ = self.run_handlers(env, [
                ["agent.start", {"thread": {"id": AMP_THREAD}, "message": AMP_PROMPT, "id": 1}],
                ["tool.result", self.shell_result(2)],
            ])
            self.assertIsNone(results["tool.result"]["result"])

    def test_daemon_down_changes_nothing(self):
        with MockAmpServer() as server:
            env = dict(self.env(server.url), SUBCORTEX_PORT="1")  # templated into the plugin: nothing listens
            self.install("amp", env)
            results, proc = self.run_handlers(env, [
                ["agent.start", {"thread": {"id": AMP_THREAD}, "message": AMP_PROMPT, "id": 1}],
                ["tool.result", self.shell_result(0)],
                ["agent.end", {"thread": {"id": AMP_THREAD}, "message": AMP_PROMPT, "id": 1, "status": "done",
                               "messages": []}],
            ])
            self.assertEqual(results["agent.start"], {"event": "agent.start", "result": {}})
            self.assertEqual(results["tool.result"], {"event": "tool.result", "result": None})
            self.assertEqual(results["agent.end"], {"event": "agent.end", "result": None})
            self.assertNotIn("Error", proc.stdout + proc.stderr)


# -- JetBrains Junie CLI --------------------------------------------------------------------

JUNIE = _bin("SUBCORTEX_JUNIE_BIN", "junie")
JUNIE_PRIVATE = (".junie", "Library/Caches/JNA", "Library/Caches/JetBrains", "Library/Application Support/JetBrains")
_ANSI = re.compile(r"\x1b\[[0-9;?<>=]*[A-Za-z~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>]")


def drive_pty(argv, env, cwd, steps, timeout=90):
    """Run ``argv`` in a pty; ``steps`` is [(text to wait for, keys to send)].
    Returns (screen text, exit code or None if it had to be killed)."""
    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.chdir(cwd)
            os.execve(argv[0], argv, env)
        finally:
            os._exit(127)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
    out = b""
    deadline = time.time() + timeout

    def text():
        return _ANSI.sub("", out.decode("utf-8", "replace")).replace("\r", "")

    def pump(until, secs):
        nonlocal out
        end = min(deadline, time.time() + secs)
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return False
                if not chunk:
                    return False
                out += chunk
            if until and until in text():
                return True
        return False

    try:
        for wait_for, keys in steps:
            if wait_for and not pump(wait_for, 60):
                raise AssertionError(f"never saw {wait_for!r}; screen:\n{text()[-3000:]}")
            time.sleep(0.5)
            os.write(fd, keys.encode())
        pump(None, 10)  # let it exit
    finally:
        code = None
        for _ in range(50):
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                code = os.waitstatus_to_exitcode(status)
                break
            time.sleep(0.2)
        else:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(fd)
    return text(), code


@unittest.skipUnless(JUNIE, "Junie CLI is not installed (or set SUBCORTEX_JUNIE_BIN)")
class TestJunie(VendorCase):
    """A custom model profile (``$JUNIE_HOME/models/*.json``, ``--model custom:<file>``)
    runs Junie without a JetBrains login. UserPromptSubmit fires only in the
    interactive TUI (not ``junie "task"`` batch runs), so the TUI runs in a pty.

    Junie is a JVM app and IGNORES $HOME: without ``-Duser.home`` + JUNIE_HOME +
    ``-c`` it writes the real ~/.junie. It also probes the macOS keychain by
    *writing* an item (``security add-generic-password``): a fake ``security`` first
    on PATH refuses, so it falls back to file storage in the temp dir. On macOS the
    run is also fenced (no writes to the real home, no keychain, loopback only)."""

    PRIVATE = JUNIE_PRIVATE
    PROMPT = "what is 2+2?"

    def env(self):
        home = self.root / "home"
        junie_home = home / ".junie"
        bin_dir = self.root / "bin"
        for d in (junie_home / "models", bin_dir):
            d.mkdir(parents=True, exist_ok=True)
        fake = bin_dir / "security"  # never let Junie near the real keychain
        fake.write_text("#!/bin/sh\nexit 44\n")
        fake.chmod(0o755)
        jvm = [f"-Duser.home={home}", "-Dhttp.proxyHost=127.0.0.1", "-Dhttp.proxyPort=9",
               "-Dhttps.proxyHost=127.0.0.1", "-Dhttps.proxyPort=9", "-Dhttp.nonProxyHosts=127.0.0.1|localhost"]
        env = {k: v for k, v in os.environ.items() if not k.startswith(("JUNIE_", "JAVA_", "JDK_"))}
        env.update(self.subcortex_env, **NO_NETWORK, HOME=str(home), JUNIE_HOME=str(junie_home),
                   JAVA_TOOL_OPTIONS=" ".join(jvm), PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                   TERM="xterm-256color", COLUMNS="140", LINES="40")
        return env

    def run_junie(self, llm, env):
        (Path(env["JUNIE_HOME"]) / "models" / "mock.json").write_text(json.dumps({
            "baseUrl": llm.url + "/v1/chat/completions", "id": "mock-model",
            "apiType": "OpenAICompletion", "apiKey": "x"}))
        llm.script = [{"tool": "answer", "input": {"full_answer": "ANSWER-FOUR", "is_terminal": True}}]
        argv = self.fence([os.path.abspath(JUNIE), "-c", str(self.root / "cache"), "--skip-update-check",
                           "--share-anonymous-statistics=false", "--model", "custom:mock"])
        steps = [("Trust this project", "\r"),   # the trust dialog of a fresh project
                 ("What can I do?", self.PROMPT),
                 (self.PROMPT, "\r"),
                 ("ANSWER-FOUR", "/exit"),
                 ("/quit", "\r")]
        return drive_pty(argv, env, self.root / "work", steps)

    def junie_log(self, env):
        return "\n".join(p.read_text(errors="replace") for p in (Path(env["JUNIE_HOME"]) / "logs").glob("log-*.log"))

    def test_hint_reaches_the_model(self):
        with MockLLM() as llm:
            env = self.env()
            self.install("junie", env)
            config = json.loads((Path(env["JUNIE_HOME"]) / "config.json").read_text())
            self.assertEqual(list(config["hooks"]), ["UserPromptSubmit"])
            screen, code = self.run_junie(llm, env)
            self.assertEqual(code, 0, screen[-2000:])
            first = json.dumps(llm.agent_requests()[0])
            self.assertIn(self.PROMPT, first)
            self.assertIn(HINT, first, "the UserPromptSubmit additionalContext did not reach the model")
            self.assertIn("Hook command completed (exit=0", self.junie_log(env))

    def test_daemon_down_does_not_block(self):
        with MockLLM() as llm:
            env = self.env()
            self.install("junie", env)
            env["SUBCORTEX_PORT"] = "1"  # nothing listens there
            screen, code = self.run_junie(llm, env)
            self.assertEqual(code, 0, screen[-2000:])
            self.assertIn("ANSWER-FOUR", screen)
            self.assertIn(self.PROMPT, json.dumps(llm.agent_requests()[0]))
            self.assertNotIn("[subcortex]", llm.all_text())
            log = self.junie_log(env)
            self.assertIn("Hook command completed (exit=0", log)
            for bad in ("Hook command failed", "Hook command timed out", "Hook command blocked",
                        "requested block", "hard halt"):
                self.assertNotIn(bad, log)


# -- Devin CLI ------------------------------------------------------------------------------

DEVIN = _bin("SUBCORTEX_DEVIN_BIN", "devin")
DEVIN_PRIVATE = (".config/devin", ".local/share/devin")


@unittest.skipUnless(DEVIN, "Devin CLI is not installed (or set SUBCORTEX_DEVIN_BIN)")
class TestDevin(VendorCase):
    """Devin has no BYOK: every model turn is a protobuf Connect RPC to Cognition's
    API. Its credentials file (``$XDG_DATA_HOME/devin/credentials.toml``) names the
    API URL, so a throwaway one points it at tests/e2e/mock_connect.py, which answers
    every call with an empty message: no reply is ever generated, but the chat
    request is recorded and protobuf carries strings verbatim."""

    PRIVATE = DEVIN_PRIVATE
    PROMPT = "what is 2+2?"
    CHAT = "GetChatMessage"  # exa.api_server_pb.ApiServerService/GetChatMessage

    def env(self, api_url):
        home = self.root / "home"
        data = home / ".local" / "share"
        (data / "devin").mkdir(parents=True)
        (data / "devin" / "credentials.toml").write_text(  # local-only fake login
            f'windsurf_api_key = "e2e-local-only"\napi_server_url = "{api_url}"\n'
            f'devin_webapp_host = "{api_url}"\ndevin_api_url = "{api_url}"\n')
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("DEVIN_", "WINDSURF_", "XDG_", "CLAUDE"))}
        env.update(self.subcortex_env, **NO_NETWORK, HOME=str(home),
                   XDG_CONFIG_HOME=str(home / ".config"),  # Devin honors it; the installer writes ~/.config
                   XDG_DATA_HOME=str(data), XDG_CACHE_HOME=str(home / ".cache"),
                   XDG_STATE_HOME=str(home / ".local" / "state"))
        return env

    def run_devin(self, env):
        argv = [os.path.abspath(DEVIN), "-p", self.PROMPT, "--respect-workspace-trust", "false"]
        return self.run_tui(argv, env, timeout=90)

    def agent_request(self, api):
        turns = [b for b in api.bodies(self.CHAT) if self.PROMPT.encode() in b]
        self.assertTrue(turns, f"no model turn recorded; calls: {[c[0] for c in api.calls]}")
        return max(turns, key=len)  # the agent turn (the other is the title generator)

    def test_hint_reaches_the_model(self):
        with MockConnect() as api:
            env = self.env(api.url)
            self.install("devin", env)
            proc = self.run_devin(env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertIn(HINT.encode(), self.agent_request(api),
                          "the UserPromptSubmit additionalContext did not reach the model")

    def test_daemon_down_does_not_block(self):
        with MockConnect() as api:
            env = self.env(api.url)
            self.install("devin", env)
            env["SUBCORTEX_PORT"] = "1"  # nothing listens there
            proc = self.run_devin(env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertNotIn(b"[subcortex]", self.agent_request(api))
            self.assertNotIn("hook", (proc.stdout + proc.stderr).lower())


# -- Augment Auggie (MCP only) --------------------------------------------------------------

AUGGIE = _bin("SUBCORTEX_AUGGIE_BIN", "auggie")
AUGGIE_CLASSIFY = "subcortex_classify_prompt_subcortex"  # Auggie's name: <tool>_<server>


@unittest.skipUnless(AUGGIE, "Auggie CLI is not installed (or set SUBCORTEX_AUGGIE_BIN)")
class TestAuggie(VendorCase):
    """subcortex reaches Auggie through MCP only (``~/.augment/settings.json`` →
    ``mcpServers.subcortex``). Auggie has no BYOK, but ``AUGMENT_SESSION_AUTH`` names
    the backend ("tenant") URL, so it points at tests/e2e/mock_augment.py. Auggie
    keeps no secrets in the keychain; on macOS the run is fenced anyway."""

    PRIVATE = (".augment",)

    def env(self, backend):
        home = self.root / "home"
        home.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AUGMENT_", "XDG_"))}
        env.update(self.subcortex_env, **NO_NETWORK, HOME=str(home), AUGMENT_SESSION_AUTH=backend.session_auth(),
                   AUGMENT_DISABLE_AUTO_UPDATE="1")
        return env

    def install_mcp(self, env):
        self.install("auggie", env)
        settings = Path(env["HOME"]) / ".augment" / "settings.json"
        data = json.loads(settings.read_text())
        entry = data["mcpServers"]["subcortex"]
        self.assertEqual(entry["args"][-1], "mcp")
        # Auggie starts stdio MCP servers with only HOME/LOGNAME/PATH/SHELL/TERM/USER, so
        # the test daemon's port, token dir and "no autostart" must travel in the entry's
        # own env — or `subcortex mcp` falls back to port 7707 and spawns a real daemon.
        entry["env"] = dict(self.subcortex_env)
        settings.write_text(json.dumps(data, indent=2))

    def run_auggie(self, env, prompt):
        argv = [os.path.abspath(AUGGIE), "-p", "--dont-save-session", "--max-turns", "4",
                "--permission", f"{AUGGIE_CLASSIFY}:allow", "-w", str(self.root / "work"), "-i", prompt]
        return self.run_tui(argv, env, timeout=90)

    def test_mcp_tools_are_offered_and_callable(self):
        with MockAugment() as backend:
            env = self.env(backend)
            self.install_mcp(env)
            backend.script = [{"tool": AUGGIE_CLASSIFY, "input": {"prompt": "what does ls -la do?"}},
                              {"text": "Done."}]
            proc = self.run_auggie(env, "classify this prompt with subcortex")
            self.assertEqual(proc.returncode, 0, proc.stdout[-1500:] + proc.stderr[-1500:])
            chats = backend.chat_requests()
            offered = [t["name"] for t in chats[0].get("tool_definitions", [])]
            self.assertIn(AUGGIE_CLASSIFY, offered, "the subcortex MCP tools were not offered to the model")
            self.assertEqual(len(chats), 2, [c.get("nodes") for c in chats])
            result = json.dumps(chats[1].get("nodes"))  # the tool result travels back to the model
            self.assertIn("simple", result, result[:2000])
            self.assertIn("Done.", proc.stdout)

    def test_daemon_down_does_not_break_the_turn(self):
        with MockAugment() as backend:
            env = self.env(backend)
            self.subcortex_env["SUBCORTEX_PORT"] = "1"  # the MCP server's env: nothing listens, autostart off
            self.install_mcp(env)
            backend.script = [{"tool": AUGGIE_CLASSIFY, "input": {"prompt": "what does ls -la do?"}},
                              {"text": "Done."}]
            proc = self.run_auggie(env, "classify this prompt with subcortex")
            self.assertEqual(proc.returncode, 0, proc.stdout[-1500:] + proc.stderr[-1500:])
            self.assertIn("Done.", proc.stdout)
            chats = backend.chat_requests()
            self.assertEqual(len(chats), 2)
            results = [n["tool_result_node"] for n in chats[1].get("nodes", []) if "tool_result_node" in n]
            self.assertEqual(len(results), 1, chats[1].get("nodes"))
            self.assertNotIn("simple", results[0].get("content", ""))  # an error result, not a verdict


if __name__ == "__main__":
    unittest.main()
