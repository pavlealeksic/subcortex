"""End-to-end: Docker Agent, Mistral Vibe (hooks) and Crush, Goose (MCP only).

Same pattern as test_e2e.py: opt-in (SUBCORTEX_E2E=1), skipped when a TUI
binary is absent (SUBCORTEX_<TUI>_BIN, else PATH), the real installer writes
into a throwaway config dir, the TUI talks to tests/e2e/mock_llm.py (OpenAI
Chat Completions) and to the stub daemon, and the assertions are on what the
TUI actually sent to the model. HOME and every config/data/cache dir the TUI
knows about point into the temp root.

    cd tests && SUBCORTEX_E2E=1 SUBCORTEX_DOCKER_AGENT_BIN=... ../.venv/bin/python -m unittest test_e2e_more -v
"""

import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

import pathsetup  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent / "e2e"))
import test_e2e  # noqa: E402
from mock_llm import MockLLM  # noqa: E402
from test_e2e import HINT, TRIMMED, E2ECase  # noqa: E402

from subcortex import policy  # noqa: E402

RESTORED = "[subcortex] Recent conversation from before context compaction"


def _binary(var, *names):
    found = os.environ.get(var)
    if found:
        return found
    for name in names:
        if shutil.which(name):
            return shutil.which(name)
    return None


def _build(lines):
    """A big, successful, routine shell output with numbered lines."""
    return f"seq 1 {lines} | sed 's/^/compiling module /'"


def _events(stdout):
    events = []
    for line in stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return events


def sandbox_env(root, *drop):
    """os.environ with HOME, every XDG base dir and DOCKER_CONFIG inside ``root``
    (created), and the variables starting with any of ``drop`` removed."""
    dirs = {"HOME": "home", "XDG_CONFIG_HOME": "xdg/config", "XDG_DATA_HOME": "xdg/data",
            "XDG_STATE_HOME": "xdg/state", "XDG_CACHE_HOME": "xdg/cache", "DOCKER_CONFIG": "home/.docker"}
    env = {k: v for k, v in os.environ.items() if not k.startswith(("XDG_",) + drop)}
    for var, sub in dirs.items():
        (root / sub).mkdir(parents=True, exist_ok=True)
        env[var] = str(root / sub)
    return env


class StubSpy:
    """Counts the decisions the stub daemon made (proof a call reached it)."""

    def __enter__(self):
        self.calls = []
        real = test_e2e.StubBackend.predict

        def spy(backend, state, questions):
            self.calls.append(questions)
            return real(backend, state, questions)

        self.patch = mock.patch.object(test_e2e.StubBackend, "predict", spy)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


# -- Docker Agent -------------------------------------------------------------------------------

DOCKER_AGENT = _binary("SUBCORTEX_DOCKER_AGENT_BIN", "docker-agent", "cagent")


@unittest.skipUnless(DOCKER_AGENT, "Docker Agent is not installed (or set SUBCORTEX_DOCKER_AGENT_BIN)")
class TestDockerAgent(E2ECase):
    def setup_agent(self, mock_url, context_size=200000, threshold=None):
        agent = self.root / "agent.yaml"
        agent.write_text(
            "providers:\n  mock:\n"
            f"    base_url: {mock_url}/v1\n    token_key: MOCK_API_KEY\n    api_type: openai_chatcompletions\n"
            "models:\n  m:\n    provider: mock\n    model: mock-model\n"
            f"    provider_opts:\n      context_size: {context_size}\n"
            "agents:\n  root:\n    model: m\n    instruction: You are a test agent.\n"
            + (f"    compaction_threshold: {threshold}\n" if threshold else "")
            + "    toolsets:\n      - type: shell\n")
        # HOME alone relocates the config dir, the cache and ~/.cagent/session.db
        # (which the before_compaction hook reads); the config dir is pinned too.
        env = sandbox_env(self.root, "DOCKER_AGENT_", "CAGENT_")
        home = Path(env["HOME"])
        env.update(self.subcortex_env, DOCKER_AGENT_CONFIG_DIR=str(home / ".config" / "cagent"), MOCK_API_KEY="x",
                   TELEMETRY_ENABLED="false", DOCKER_AGENT_HIDE_TELEMETRY_BANNER="1",
                   DOCKER_AGENT_AUTO_INSTALL="false")
        self.install("docker-agent", env)
        self.assertTrue((home / ".config" / "cagent" / "hooks.d" / "50-subcortex.yaml").is_file())
        return agent, env

    def run_agent(self, agent, env, *messages):
        proc = self.run_tui([DOCKER_AGENT, "run", "--exec", "--yolo", "--json",
                             "--working-dir", str(self.root / "work"), str(agent), *messages], env)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertEqual(proc.stderr.strip(), "")  # --exec prints hook warnings on stderr
        events = _events(proc.stdout)
        self.assertEqual([e for e in events if e.get("type") in ("error", "warning")], [])
        for event in events:
            if event.get("type") == "hook_finished":
                self.assertTrue(event.get("allowed"), event)
        return events

    def test_hint_and_trim_reach_the_model(self):
        with MockLLM() as llm:
            agent, env = self.setup_agent(llm.url)
            llm.script = [{"tool": "shell", "input": {"cmd": _build(1500)}}, {"text": "Done."}]
            events = self.run_agent(agent, env, "run the build")
            self.assertIn("Done.", [e.get("content") for e in events if e.get("type") == "agent_choice"])
            first, after_tool = (json.dumps(r) for r in llm.agent_requests()[:2])
            self.assertIn(HINT, first, "the user_prompt_submit hint did not reach the model")
            tool = [m for m in llm.agent_requests()[1]["messages"] if m["role"] == "tool"][0]["content"]
            self.assertIn(TRIMMED, tool, "the tool_response_transform trim did not reach the model")
            self.assertIn("compiling module 1500", tool)            # the tail survived
            self.assertNotIn("compiling module 750\n", tool)        # the middle did not
            self.assertIn(HINT, after_tool)                          # the hint lasts the whole turn

    def test_compaction_snapshot_restored_with_the_next_prompt(self):
        with MockLLM() as llm:
            # A tiny window and threshold: the build output triggers compaction mid-turn.
            agent, env = self.setup_agent(llm.url, context_size=4000, threshold=0.05)
            llm.script = [{"text": "Noted."}, {"tool": "shell", "input": {"cmd": _build(1500)}},
                          {"text": "Done."}]
            events = self.run_agent(agent, env, "remember the codeword PELICAN-42", "run the build",
                                    "what was the codeword?")
            hooks = [e.get("hook_event") for e in events if e.get("type") == "hook_finished"]
            self.assertIn("before_compaction", hooks)
            self.assertIn("after_compaction", hooks)
            self.assertIn({"type": "session_compaction", "outcome": "applied"},
                          [{"type": e.get("type"), "outcome": e.get("outcome")} for e in events])
            last = llm.agent_requests()[-1]
            self.assertEqual(last["messages"][-1]["content"], "what was the codeword?")
            system = "\n".join(m["content"] for m in last["messages"] if m["role"] == "system")
            self.assertIn(RESTORED, system)
            self.assertIn("user: remember the codeword PELICAN-42", system)

    def test_hooks_never_block_even_with_the_daemon_down(self):
        with MockLLM() as llm:
            agent, env = self.setup_agent(llm.url)
            env["SUBCORTEX_PORT"] = "1"  # nothing listens there
            llm.script = [{"tool": "shell", "input": {"cmd": _build(1500)}}, {"text": "Done."}]
            events = self.run_agent(agent, env, "run the build")
            self.assertIn("Done.", [e.get("content") for e in events if e.get("type") == "agent_choice"])
            self.assertNotIn("[subcortex", llm.all_text())
            self.assertIn("compiling module 750", llm.all_text())  # output untouched


# -- Mistral Vibe -------------------------------------------------------------------------------

VIBE = _binary("SUBCORTEX_VIBE_BIN", "vibe")


@unittest.skipUnless(VIBE, "Mistral Vibe is not installed (or set SUBCORTEX_VIBE_BIN)")
class TestMistralVibe(E2ECase):
    """Vibe has no prompt or compaction event: only (b) and (c) apply."""

    def setup_vibe(self, mock_url):
        env = sandbox_env(self.root, "VIBE_", "MISTRAL_")
        vibe_home = self.root / "vibe"
        vibe_home.mkdir()
        (vibe_home / "config.toml").write_text(
            'active_model = "mock"\nenable_telemetry = false\nenable_update_checks = false\n'
            'enable_auto_update = false\n\n[experiments]\nenable = false\n\n'
            f'[[providers]]\nname = "mock"\napi_base = "{mock_url}/v1"\napi_key_env_var = "MOCK_API_KEY"\n'
            'api_style = "openai"\nbackend = "generic"\n\n'
            '[[models]]\nname = "mock-model"\nprovider = "mock"\nalias = "mock"\n')
        env.update(self.subcortex_env, VIBE_HOME=str(vibe_home), MOCK_API_KEY="x")
        self.install("vibe", env)
        return env

    def run_vibe(self, env, prompt, *extra):
        """(entries of --output json, [(status, content)] of our post_tool hook runs)."""
        # The Unified Harness needs an internal install; the legacy one is what users run.
        proc = self.run_tui([VIBE, "-p", prompt, "--auto-approve", "--output", "json", "--legacy-harness",
                             "--workdir", str(self.root / "work"), *extra], env)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertEqual(proc.stderr.strip(), "")
        entries = json.loads(proc.stdout)
        self.assertEqual([e for e in entries if e.get("level") in ("warning", "error")], [])
        runs = [(e["detail"]["status"], e["detail"]["content"]) for e in entries
                if (e.get("detail") or {}).get("kind") == "hook_completed"
                and e["detail"].get("hookName") == "subcortex-post-tool"]
        for status, content in runs:
            # Vibe shows every replacement with a warning icon; anything else is a hook failure.
            self.assertIn((status, (content or "")[:19]), [("ok", ""), ("warning", "subcortex: trimmed ")])
        return entries, runs

    def tool_results(self, llm):
        return [m["content"] for m in llm.agent_requests()[-1]["messages"] if m["role"] == "tool"]

    def test_deny_reason_replaces_the_output_the_model_sees(self):
        # The hook needs the user's request as evidence. Vibe never sends it
        # (see the next test), so record it for the session first, as a
        # prompt hook would, then continue that session.
        with MockLLM() as llm:
            env = self.setup_vibe(llm.url)
            entries, runs = self.run_vibe(env, "hello")
            session = entries[0]["sessionId"]
            with mock.patch.dict(os.environ, {"SUBCORTEX_DATA_DIR": self.subcortex_env["SUBCORTEX_DATA_DIR"]}):
                policy.remember_prompt(session, "run the build", tui="vibe")
            # Under Vibe's own 16k-char stdout cut, so the tail reaches the hook.
            llm.script = [{"tool": "bash", "input": {"command": _build(700)}}, {"text": "Done."}]
            _, runs = self.run_vibe(env, "run the build", "--resume", session)
            self.assertEqual([status for status, _ in runs], ["warning"])  # replaced
            tool = self.tool_results(llm)[-1]
            self.assertIn(TRIMMED, tool, "the post_tool deny/reason replacement did not reach the model")
            self.assertIn("compiling module 700", tool)
            self.assertNotIn("compiling module 350\n", tool)
            self.assertIn("returncode: 0", tool)  # still a successful result, not an error

    def test_trim_without_a_prompt_hook(self):
        with MockLLM() as llm:
            env = self.setup_vibe(llm.url)
            # Two steps in one turn: Vibe writes messages.jsonl (transcript_path)
            # after each step, so from the second step on it holds the request.
            llm.script = [{"tool": "bash", "input": {"command": "echo warming up"}},
                          {"tool": "bash", "input": {"command": _build(700)}}, {"text": "Done."}]
            self.run_vibe(env, "run the build")
            self.assertIn(TRIMMED, self.tool_results(llm)[-1])

    def test_hooks_never_block_even_with_the_daemon_down(self):
        with MockLLM() as llm:
            env = self.setup_vibe(llm.url)
            env["SUBCORTEX_PORT"] = "1"
            llm.script = [{"tool": "bash", "input": {"command": _build(700)}}, {"text": "Done."}]
            entries, runs = self.run_vibe(env, "run the build")
            self.assertEqual(runs, [("ok", None)])
            self.assertEqual(entries[-1]["content"], [{"type": "text", "text": "Done."}])
            self.assertNotIn("[subcortex", llm.all_text())
            self.assertIn("compiling module 350", self.tool_results(llm)[-1])


# -- MCP-only TUIs: Crush, Goose ---------------------------------------------------------------

CRUSH = _binary("SUBCORTEX_CRUSH_BIN", "crush")
GOOSE = _binary("SUBCORTEX_GOOSE_BIN", "goose")
MCP_TOOLS = ("subcortex_classify_prompt", "subcortex_decide", "subcortex_judge_output")


class MCPCase(E2ECase):
    """The installer's `subcortex mcp` entry starts, its tools reach the model,
    and a scripted classify call round-trips through the (stub) daemon. The
    MCP server autostarts a daemon when none answers, so the stub is always up."""

    prefix = ""

    def assert_round_trip(self, llm, spy):
        tools = [t["function"]["name"] for t in llm.agent_requests()[0]["tools"]]
        for name in MCP_TOOLS:
            self.assertIn(self.prefix + name, tools)
        result = [m for m in llm.agent_requests()[1]["messages"] if m["role"] == "tool"][-1]["content"]
        self.assertEqual(json.loads(result)["verdict"]["label"], "simple", result)
        self.assertTrue(spy.calls, "the stub daemon was never asked")

    def script(self, llm):
        llm.script = [{"tool": self.prefix + "subcortex_classify_prompt", "input": {"prompt": "what does ls -la do?"}},
                      {"text": "Done."}]


@unittest.skipUnless(CRUSH, "Crush is not installed (or set SUBCORTEX_CRUSH_BIN)")
class TestCrushMCP(MCPCase):
    prefix = "mcp_subcortex_"

    def test_mcp_tools_listed_and_classify_round_trips(self):
        with MockLLM() as llm, StubSpy() as spy:
            env = sandbox_env(self.root, "CRUSH_")
            dirs = {k: self.root / k for k in ("crush", "crush-data")}
            for d in dirs.values():
                d.mkdir()
            (dirs["crush"] / "crush.json").write_text(json.dumps({
                "providers": {"mock": {"type": "openai-compat", "base_url": llm.url + "/v1", "api_key": "x",
                                       "models": [{"id": "mock-model", "name": "Mock", "context_window": 200000,
                                                   "default_max_tokens": 4000}]}},
                "models": {"large": {"model": "mock-model", "provider": "mock"},
                           "small": {"model": "mock-model", "provider": "mock"}},
                "options": {"disable_provider_auto_update": True, "disable_metrics": True}}, indent=2))
            env.update(self.subcortex_env, CRUSH_GLOBAL_CONFIG=str(dirs["crush"]),
                       CRUSH_GLOBAL_DATA=str(dirs["crush-data"]), CRUSH_DISABLE_PROVIDER_AUTO_UPDATE="1",
                       CRUSH_DISABLE_METRICS="1", DO_NOT_TRACK="1")
            self.install("crush", env)
            self.assertIn("subcortex", json.loads((dirs["crush"] / "crush.json").read_text())["mcp"])
            self.script(llm)
            # `crush run` auto-approves tool calls; data dir = <cwd>/.crush (temp).
            proc = self.run_tui([CRUSH, "run", "-q", "-m", "mock/mock-model", "classify my prompt"], env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertIn("Done.", proc.stdout)
            self.assert_round_trip(llm, spy)


@unittest.skipUnless(GOOSE, "Goose is not installed (or set SUBCORTEX_GOOSE_BIN)")
class TestGooseMCP(MCPCase):
    prefix = "subcortex__"

    def test_mcp_tools_listed_and_classify_round_trips(self):
        with MockLLM() as llm, StubSpy() as spy:
            env = sandbox_env(self.root, "GOOSE_", "OPENAI_")
            goose = self.root / "goose"
            env.update(self.subcortex_env, GOOSE_PATH_ROOT=str(goose), GOOSE_DISABLE_KEYRING="1", GOOSE_TELEMETRY_OFF="1",
                       GOOSE_PROVIDER="openai", GOOSE_MODEL="mock-model", GOOSE_MODE="auto",
                       OPENAI_HOST=llm.url, OPENAI_BASE_PATH="v1/chat/completions", OPENAI_API_KEY="x")
            self.install("goose", env)
            self.assertTrue((goose / "config" / "config.yaml").is_file())
            self.script(llm)
            proc = self.run_tui([GOOSE, "run", "--text", "classify my prompt"], env)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertIn("Done.", proc.stdout)
            self.assert_round_trip(llm, spy)


if __name__ == "__main__":
    unittest.main()
