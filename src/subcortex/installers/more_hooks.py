"""Installers for Grok Build, Docker Agent, Letta Code and Mistral Vibe."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..adapters.docker_agent import config_dir as docker_config_dir
from ..adapters.grok import grok_home
from .base import Installer, Target, owned_file_target, toml_block_target, toml_str
from .claude_family import SESSION, ClaudeStyleInstaller


class GrokBuildInstaller(Installer):
    """``$GROK_HOME/hooks/subcortex.json`` — a Claude-format hooks file we own."""

    name = "grok-build"
    display_name = "Grok Build"
    seam = "hooks"
    binaries = ("grok",)
    docs = "https://x.ai/cli"
    supports_mcp = True
    post_install = "start a new grok session (or /hooks → r) to load the hooks"
    # UserPromptSubmit output is discarded by Grok; the hook only records the
    # request, which the output judge needs as evidence.
    EVENTS = {"UserPromptSubmit": (None, 5), "PostToolUse": ("Bash|bash", 10),
              "PreCompact": (None, 10), "PostCompact": (None, 5)}

    def _content(self) -> str:
        hooks: Dict[str, Any] = {}
        for event, (matcher, timeout) in self.EVENTS.items():
            group: Dict[str, Any] = {"matcher": matcher} if matcher else {}
            group["hooks"] = [{"type": "command", "command": self.command(event), "timeout": timeout}]
            hooks[event] = [group]
        return json.dumps({"_subcortex": {"managed": True, "schema": 1}, "hooks": hooks}, indent=2) + "\n"

    def targets(self) -> List[Target]:
        targets = [owned_file_target(grok_home() / "hooks" / "subcortex.json", self._content())]
        if self.mcp:
            argv = self.mcp_command()
            body = "\n".join(["[mcp_servers.subcortex]", f"command = {toml_str(argv[0])}",
                              f"args = [{', '.join(toml_str(a) for a in argv[1:])}]", "enabled = true"]) + "\n"
            targets.append(toml_block_target(grok_home() / "config.toml", body, self.name))
        return targets

    def hook_events(self) -> List[str]:
        return list(self.EVENTS)

    def sample_payload(self, event: str) -> Dict[str, Any]:
        common = {"hookEventName": event.lower(), "sessionId": SESSION, "cwd": "/tmp", "workspaceRoot": "/tmp",
                  "transcriptPath": "{transcript}", "permissionMode": "default", "hook_event_name": event}
        if event == "UserPromptSubmit":
            return {**common, "hookEventName": "user_prompt_submit", "promptId": "p1",
                    "prompt": "why does make build take so long?"}
        if event == "PostToolUse":
            return {**common, "toolName": "run_terminal_command", "toolUseId": "call_1",
                    "toolInput": {"command": "make build"}, "toolInputTruncated": False,
                    "toolResultTruncated": False,
                    "toolResult": {"type": "Bash", "output": [], "output_for_prompt": "{big_output}",
                                   "exit_code": 0, "command": "make build", "truncated": False, "signal": None,
                                   "timed_out": False, "current_dir": "/tmp", "output_file": "", "total_bytes": 0}}
        return {**common, "source": "manual"}

    def expects_output(self, event: str) -> bool:
        return event == "PostToolUse"


class DockerAgentInstaller(Installer):
    """``<config dir>/hooks.d/50-subcortex.yaml`` — a drop-in we own."""

    name = "docker-agent"
    display_name = "Docker Agent"
    seam = "hooks"
    binaries = ("docker-agent", "cagent")
    docs = "https://github.com/docker/docker-agent/blob/main/docs/configuration/hooks/index.md"
    min_version = "1.137.0"
    post_install = "applies to new `docker-agent run` sessions"
    EVENTS = {"user_prompt_submit": 5, "user_steering_messages_submit": 5, "user_followup_submit": 5,
              "tool_response_transform": 10, "before_compaction": 10, "after_compaction": 5}

    def _content(self) -> str:
        q = json.dumps  # JSON strings are valid YAML double-quoted scalars
        lines = ["# Managed by subcortex (subcortex install docker-agent). Do not edit.",
                 "# Uninstall: subcortex uninstall docker-agent"]
        for event, timeout in self.EVENTS.items():
            hook = [f"name: {q('subcortex:' + event)}", "type: command",
                    f"command: {q(self.command(event))}", f"timeout: {timeout}", "on_error: ignore"]
            lines.append(f"{event}:")
            if event == "tool_response_transform":
                lines += ['  - matcher: "shell"', "    hooks:", f"      - {hook[0]}"]
                lines += [f"        {h}" for h in hook[1:]]
            else:
                lines.append(f"  - {hook[0]}")
                lines += [f"    {h}" for h in hook[1:]]
        return "\n".join(lines) + "\n"

    def targets(self) -> List[Target]:
        return [owned_file_target(docker_config_dir() / "hooks.d" / "50-subcortex.yaml", self._content())]

    def hook_events(self) -> List[str]:
        return list(self.EVENTS)

    def sample_payload(self, event: str) -> Dict[str, Any]:
        common = {"session_id": SESSION, "cwd": "/tmp", "hook_event_name": event, "agent_name": "root"}
        if event == "user_prompt_submit":
            return {**common, "prompt": "what does ls -la do?"}
        if event == "user_steering_messages_submit":
            return {**common, "steering_messages": ["also, what does ls -a do?"]}
        if event == "user_followup_submit":
            return {**common, "prompt": "and ls -l?"}
        if event == "tool_response_transform":
            return {**common, "tool_category": "shell", "tool_name": "shell", "tool_use_id": "call_1",
                    "tool_input": {"cmd": "make build"}, "tool_response": "{big_output}"}
        return {**common, "compaction_reason": "manual", "input_tokens": 90000}

    def expects_output(self, event: str) -> bool:
        return event in ("user_prompt_submit", "user_steering_messages_submit", "user_followup_submit",
                         "tool_response_transform")


class LettaInstaller(ClaudeStyleInstaller):
    """``~/.letta/settings.json`` → ``hooks`` (timeouts in ms, ``quiet`` so hints aren't echoed)."""

    name = "letta"
    display_name = "Letta Code"
    binaries = ("letta",)
    docs = "https://docs.letta.com/letta-code/hooks"
    post_install = "restart letta (and don't edit hooks via /hooks in a session started before this install)"

    def settings_path(self) -> Path:
        return Path.home() / ".letta" / "settings.json"

    def entry(self, event: str) -> Dict[str, Any]:
        return {"type": "command", "command": self.command(event), "timeout": 5000, "quiet": True}

    def matcher(self, event: str) -> Optional[str]:
        return None

    def sample_payload(self, event: str) -> Dict[str, Any]:
        return {"event_type": "UserPromptSubmit", "working_directory": "/tmp", "prompt": "what does ls -la do?",
                "is_command": False, "agent_id": "agent-1", "conversation_id": "conv-1"}


class VibeInstaller(Installer):
    """A marked ``[[hooks]]`` block in ``$VIBE_HOME/hooks.toml`` (Vibe never rewrites that file)."""

    name = "vibe"
    display_name = "Mistral Vibe"
    seam = "hooks"
    binaries = ("vibe",)
    docs = "https://github.com/mistralai/mistral-vibe#hooks"
    min_version = "2.25.5"
    post_install = "restart vibe to load the hook"

    def vibe_home(self) -> Path:
        override = os.environ.get("VIBE_HOME", "").strip()
        return Path(override).expanduser() if override else Path.home() / ".vibe"

    def targets(self) -> List[Target]:
        body = "\n".join([
            "[[hooks]]",
            'name = "subcortex-post-tool"',
            'type = "post_tool"',
            'match = "bash"',
            f"command = {toml_str(self.command('post_tool'))}",
            "timeout = 10.0",
            'description = "subcortex: trim large, disposable shell output"',
        ]) + "\n"
        return [toml_block_target(self.vibe_home() / "hooks.toml", body, self.name)]

    def hook_events(self) -> List[str]:
        return ["post_tool"]

    def sample_payload(self, event: str) -> Dict[str, Any]:
        return {"session_id": SESSION, "transcript_path": "{transcript}", "cwd": "/tmp",
                "hook_event_name": "post_tool", "tool_name": "bash", "tool_call_id": "call_1",
                "tool_input": {"command": "make build"}, "tool_status": "success",
                "tool_output_text": "{big_output}", "tool_error": None}

    def expects_output(self, event: str) -> bool:
        return True
