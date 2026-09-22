"""End-to-end: OpenAI Codex CLI and Open Interpreter (a Codex fork on the same hook engine).

Opt-in (SUBCORTEX_E2E=1) like tests/test_e2e.py. Each test installs subcortex
with its real installer into a throwaway CODEX_HOME (or Open Interpreter home)
under a throwaway HOME, points the TUI at tests/e2e/mock_responses.py (a
Responses-API mock: newer Codex speaks nothing else) through a custom model
provider, and runs ``exec`` non-interactively with approvals off and the
workspace-write sandbox rooted in the temp dir. Outbound HTTP(S) is sent to a
dead proxy so nothing can reach a real provider.

Binaries: SUBCORTEX_CODEX_BIN / SUBCORTEX_INTERPRETER_BIN, else PATH. Codex
older than 0.133 has no usable hooks and is skipped (its version is read from
the installation; no binary is ever run outside a test's sandbox).

Hook trust: Codex runs a non-managed hook only once the user trusted its
exact definition (``/hooks`` in the TUI, persisted in config.toml). Most tests
pass ``--dangerously-bypass-hook-trust``; ``test_trusted_hooks_...`` instead
writes the same trust state the TUI's "Trust all and continue" writes and runs
without the bypass, as a user would.
"""

import hashlib
import json
import os
import re
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

from test_e2e import HINT, TRIMMED, E2ECase

sys.path.insert(0, str(Path(__file__).resolve().parent / "e2e"))
from mock_responses import MockResponses  # noqa: E402

from subcortex import installers  # noqa: E402
from subcortex.installers.base import installed_version, parse_version  # noqa: E402

RESTORE = "[subcortex] Recent conversation from before context compaction"
MIN_CODEX = (0, 133, 0)
# Codex's own per-call output cap: GPT-5-class models get ~10k tokens; an unknown
# model's fallback metadata gets far less, which would pre-truncate the output
# (subcortex then leaves it alone by design).
TOOL_OUTPUT_TOKENS = 12000
BUILD = "seq -f 'compiling module %g ok' 1 1200"  # ~28 KB, one distinct line each
DEAD_PROXY = "http://127.0.0.1:9"
REAL_HOME = Path.home()


def _binary(env_var, name):
    return os.environ.get(env_var) or shutil.which(name)


CODEX = _binary("SUBCORTEX_CODEX_BIN", "codex")
# Read from the installation (npm package.json, Homebrew Cellar path), never by running it.
CODEX_VERSION = parse_version(installed_version(CODEX) or "") if CODEX else None
INTERPRETER = _binary("SUBCORTEX_INTERPRETER_BIN", "interpreter")


def shell_call(command):
    """A scripted turn calling whichever shell tool this Codex version offers."""
    def turn(body):
        names = {t.get("name") for t in body.get("tools") or []}
        if "exec_command" in names:  # unified exec (current)
            return {"tool": "exec_command", "input": {"cmd": command}}
        if "shell_command" in names:
            return {"tool": "shell_command", "input": {"command": command}}
        return {"tool": "shell", "input": {"command": ["bash", "-lc", command]}}
    return turn


def hook_statuses(stderr):
    """[(event, status)] from exec's human output: ``hook: PostToolUse Stopped``."""
    return re.findall(r"^hook: (\w+) (\w+)\s*$", stderr, re.M)


def function_outputs(body):
    return [i.get("output") for i in body.get("input") or [] if i.get("type") == "function_call_output"]


def developer_texts(body):
    return [c.get("text", "") for i in body.get("input") or []
            if i.get("type") == "message" and i.get("role") == "developer"
            for c in i.get("content") or []]


class CodexEngineTests:
    """Shared by Codex and Open Interpreter (same exec CLI, same hook engine)."""

    binary = None
    tui = ""
    home_env = ""

    def tui_home(self):
        raise NotImplementedError

    def setUp(self):
        super().setUp()
        self.real_before = self._real_state()
        self.home = self.tui_home()
        self.home.mkdir(parents=True, exist_ok=True)
        (self.root / "home").mkdir(exist_ok=True)

    def tearDown(self):
        try:
            self.assertEqual(self._real_state(), self.real_before, "a real config dir was touched")
        finally:
            super().tearDown()

    @staticmethod
    def _real_state():
        """Every path under the real ~/.codex and ~/.openinterpreter (Codex writes
        CODEX_HOME/tmp/arg0 on every start: a run that escaped the sandbox shows up here)."""
        state = {}
        for name in (".codex", ".openinterpreter"):
            top = REAL_HOME / name
            state[name] = None if not top.exists() else sorted(
                (str(p.relative_to(top)), p.lstat().st_mtime_ns if p.name in ("config.toml", "hooks.json") else 0)
                for p in top.rglob("*") if "skills" not in p.relative_to(top).parts[:1])
        return state

    # -- sandbox --------------------------------------------------------------------------

    def env(self, mock_url, extra_config=""):
        (self.home / "config.toml").write_text(
            'model = "mock-model"\n'
            'model_provider = "mock"\n'
            'approval_policy = "never"\n'
            'sandbox_mode = "workspace-write"\n'
            'check_for_update_on_startup = false\n'
            f"tool_output_token_limit = {TOOL_OUTPUT_TOKENS}\n"
            f"{extra_config}\n"
            "[model_providers.mock]\n"
            'name = "Mock"\n'
            f'base_url = "{mock_url}/v1"\n'
            'env_key = "SUBCORTEX_E2E_MOCK_KEY"\n'
            'wire_api = "responses"\n'
            "request_max_retries = 0\n"
            "stream_max_retries = 0\n")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("OPENAI_", "CODEX_", "INTERPRETER_", "OPEN_INTERPRETER_", "ZDOTDIR"))
               and k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
        env.update(self.subcortex_env, HOME=str(self.root.resolve() / "home"), SUBCORTEX_E2E_MOCK_KEY="x")
        env[self.home_env] = str(self.home)
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env[var] = env[var.lower()] = DEAD_PROXY
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
        return env

    def run_exec(self, env, prompt, *extra, resume=False, bypass=True):
        argv = [self.binary, "exec", *(["resume", "--last"] if resume else []), "--skip-git-repo-check",
                "--strict-config",
                *(["--dangerously-bypass-hook-trust"] if bypass else []), *extra, prompt]
        proc = self.run_tui(argv, env, timeout=90)
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        statuses = hook_statuses(proc.stderr)
        self.assertFalse([s for s in statuses if s[1] in ("Failed", "Blocked")], proc.stderr[-3000:])
        return proc, statuses

    def trust_all(self):
        """Persist what the TUI's startup review ("Trust all and continue") writes to
        config.toml: per handler, ``[hooks.state."<hooks.json>:<event>:<group>:<handler>"]``
        with the sha256 of the canonical JSON of its normalized definition."""
        hooks_json = self.home / "hooks.json"
        lines = ["", "[hooks.state]"]
        for event, groups in json.loads(hooks_json.read_text())["hooks"].items():
            label = re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()  # PostToolUse -> post_tool_use
            for g, group in enumerate(groups):
                for h, handler in enumerate(group["hooks"]):
                    identity = {"event_name": label, "hooks": [{
                        "type": handler["type"], "command": handler["command"],
                        "timeout": handler["timeout"], "async": handler.get("async", False)}]}
                    if group.get("matcher") is not None:
                        identity["matcher"] = group["matcher"]
                    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"),
                                                       ensure_ascii=False).encode()).hexdigest()
                    lines += [f"[hooks.state.{json.dumps(f'{hooks_json}:{label}:{g}:{h}')}]",
                              f'trusted_hash = "sha256:{digest}"']
        with open(self.home / "config.toml", "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    # -- tests ------------------------------------------------------------------------------

    def test_hint_and_trim_reach_the_model(self):
        with MockResponses() as llm:
            env = self.env(llm.url)
            self.install(self.tui, env)
            llm.script = [shell_call(BUILD), {"text": "Done."}]
            proc, statuses = self.run_exec(env, "run the build")
            requests = llm.agent_requests()
            self.assertEqual(len(requests), 2, llm.all_text()[-2000:])
            self.assertTrue(any(HINT in t for t in developer_texts(requests[0])),
                            "the UserPromptSubmit hint did not reach the model as developer context")
            output = "\n".join(function_outputs(requests[1]))
            self.assertIn(TRIMMED, output, "the PostToolUse replacement did not reach the model")
            self.assertIn("compiling module 1200 ok", output)     # the tail survived
            self.assertNotIn("compiling module 600 ok", output)   # the middle did not
            # continue:false replaced the result without ending the turn.
            self.assertEqual(proc.stdout.strip(), "Done.")
            self.assertIn(("PostToolUse", "Stopped"), statuses)
            self.assertIn(("UserPromptSubmit", "Completed"), statuses)

    def test_hooks_never_block_even_with_the_daemon_down(self):
        with MockResponses() as llm:
            env = self.env(llm.url)
            self.install(self.tui, env)
            env["SUBCORTEX_PORT"] = "1"  # nothing listens there
            llm.script = [shell_call(BUILD), {"text": "Done."}]
            proc, statuses = self.run_exec(env, "run the build")
            self.assertEqual(proc.stdout.strip(), "Done.")
            self.assertEqual(sorted(set(statuses)), [("PostToolUse", "Completed"),
                                                     ("UserPromptSubmit", "Completed")])
            self.assertNotIn("[subcortex", llm.all_text())
            output = "\n".join(function_outputs(llm.agent_requests()[1]))
            self.assertIn("compiling module 600 ok", output)  # untouched

    def test_compaction_snapshot_survives_auto_compaction(self):
        with MockResponses() as llm:
            env = self.env(llm.url, "model_auto_compact_token_limit = 20000")
            self.install(self.tui, env)
            llm.input_tokens = 100_000  # every reply reports a context over the limit
            self.run_exec(env, "remember the codeword PELICAN-42")
            before = len(llm.requests)
            proc, statuses = self.run_exec(env, "what was the codeword?", resume=True)
            after = [r["body"] for r in llm.requests[before:]]
            self.assertTrue(any(not b.get("tools") for b in after), "Codex did not compact")
            restored = [t for b in after if b.get("tools") for t in developer_texts(b) if RESTORE in t]
            self.assertTrue(restored, "the SessionStart(compact) restore did not reach the model")
            self.assertIn("PELICAN-42", restored[0])
            self.assertIn(("SessionStart", "Completed"), statuses)
            self.assertIn(("PreCompact", "Completed"), statuses)

    def test_a_failed_command_is_not_trimmed(self):
        # Codex's PostToolUse payload carries no exit status and the continue:false
        # replacement drops Codex's "Process exited with code N" header, so routine-looking
        # output of a FAILED command is trimmed and the model never learns it failed.
        # (The rollout at transcript_path already records the exit code when the hook runs.)
        with MockResponses() as llm:
            env = self.env(llm.url)
            self.install(self.tui, env)
            llm.script = [shell_call(BUILD + "; exit 3"), {"text": "Done."}]
            self.run_exec(env, "run the build")
            output = "\n".join(function_outputs(llm.agent_requests()[1]))
            self.assertNotIn(TRIMMED, output)
            self.assertIn("code 3", output)

    def test_untrusted_hooks_are_skipped_silently(self):
        with MockResponses() as llm:
            env = self.env(llm.url)
            self.install(self.tui, env)
            proc, statuses = self.run_exec(env, "say hi", bypass=False)
            self.assertEqual(proc.stdout.strip(), "OK")
            self.assertEqual(statuses, [])
            self.assertNotIn("[subcortex", llm.all_text())

    def test_trusted_hooks_run_without_bypass_and_survive_reinstall(self):
        with MockResponses() as llm:
            env = self.env(llm.url)
            self.install(self.tui, env)
            self.trust_all()
            self.install(self.tui, env)  # idempotent: the definitions (and their trust) are unchanged
            proc, statuses = self.run_exec(env, "say hi", bypass=False)
            self.assertIn(("UserPromptSubmit", "Completed"), statuses)
            self.assertIn(HINT, "\n".join(developer_texts(llm.agent_requests()[0])))
            # Uninstall leaves the trust entries behind; Codex must still start and run.
            with mock.patch.dict(os.environ, env):
                self.assertTrue(installers.get_installer(self.tui).uninstall().ok)
            proc, statuses = self.run_exec(env, "say hi", bypass=False)
            self.assertEqual((proc.stdout.strip(), statuses), ("OK", []))


@unittest.skipUnless(CODEX, "Codex CLI is not installed (or set SUBCORTEX_CODEX_BIN)")
class TestCodex(CodexEngineTests, E2ECase):
    binary = CODEX
    tui = "codex"
    home_env = "CODEX_HOME"

    def setUp(self):
        if CODEX_VERSION is not None and CODEX_VERSION < MIN_CODEX:
            self.skipTest(f"Codex {'.'.join(map(str, CODEX_VERSION))} predates hooks (need >= 0.133.0; "
                          "set SUBCORTEX_CODEX_BIN)")
        super().setUp()

    def tui_home(self):
        return self.root.resolve() / "codex"


@unittest.skipUnless(INTERPRETER, "Open Interpreter is not installed (or set SUBCORTEX_INTERPRETER_BIN)")
class TestOpenInterpreter(CodexEngineTests, E2ECase):
    binary = INTERPRETER
    tui = "open-interpreter"
    home_env = "INTERPRETER_HOME"

    def tui_home(self):
        # The installer writes ~/.openinterpreter; keep INTERPRETER_HOME pointing at the same place.
        return self.root.resolve() / "home" / ".openinterpreter"

    def test_install_follows_interpreter_home(self):
        # Open Interpreter reads its config and hooks from $INTERPRETER_HOME (default
        # ~/.openinterpreter); the installer always writes ~/.openinterpreter/hooks.json.
        self.home = self.root.resolve() / "oi-home"
        self.home.mkdir()
        with MockResponses() as llm:
            env = self.env(llm.url)
            self.install(self.tui, env)
            proc, statuses = self.run_exec(env, "say hi")
            self.assertIn(HINT, "\n".join(developer_texts(llm.agent_requests()[0])))


if __name__ == "__main__":
    unittest.main()
