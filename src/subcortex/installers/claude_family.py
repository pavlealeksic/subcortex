"""Installers for Claude-Code-style hook files.

All of them write ``{"<Event>": [{"matcher": ..., "hooks": [{"type": "command",
"command": ..., "timeout": N}]}]}`` groups — under a top-level ``hooks`` key or
(Droid's hooks.json) at the top level — for exactly the events the TUI's
adapter handles.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..adapters import get_adapter
from .base import Installer, Target, add_grouped, json_target, mcp_json_target, remove_grouped

SESSION = "3f6c0e1a-5b7d-4c2e-9a18-6d0f4b2c7e91"


class ClaudeStyleInstaller(Installer):
    seam = "hooks"
    wrapper: Optional[str] = "hooks"   # None: events at the file's top level
    shell_tool = "Bash"                # PostToolUse matcher
    timeout = 10                       # seconds

    def settings_path(self) -> Path:
        raise NotImplementedError

    def mcp_target(self) -> Optional[Target]:
        return None

    # -- events -----------------------------------------------------------------------

    def _adapter(self):
        return get_adapter(self.name)

    def hook_events(self) -> List[str]:
        order = ["UserPromptSubmit", "PostToolUse", "PreCompact", "SessionStart"]
        order += [e for e in self._adapter().events if e not in order]
        return [e for e in order if e in self._adapter().events]

    def matcher(self, event: str) -> Optional[str]:
        return {"PostToolUse": self.shell_tool, "SessionStart": "compact"}.get(event)

    def entry(self, event: str) -> Dict[str, Any]:
        return {"type": "command", "command": self.command(event), "timeout": self.timeout}

    # -- file edits ---------------------------------------------------------------------

    def _table(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if self.wrapper is None:
            return data
        table = data.get(self.wrapper)
        if not isinstance(table, dict):
            table = data[self.wrapper] = {}
        return table

    def targets(self) -> List[Target]:
        def add(data: Dict[str, Any]) -> None:
            table = self._table(data)
            for event in self.hook_events():
                add_grouped(table, event, self.matcher(event), self.entry(event))

        def remove(data: Dict[str, Any]) -> None:
            table = data if self.wrapper is None else data.get(self.wrapper)
            if isinstance(table, dict):
                remove_grouped(table)
                if self.wrapper is not None and not table:
                    del data[self.wrapper]

        def installed(data: Dict[str, Any]) -> bool:
            table = data if self.wrapper is None else data.get(self.wrapper)
            if not isinstance(table, dict):
                return False
            probe = {k: [dict(g) for g in v if isinstance(g, dict)]
                     for k, v in table.items() if isinstance(v, list)}
            return bool(remove_grouped(probe))

        targets = [json_target(self.settings_path(), add, remove, installed)]
        mcp = self.mcp_target() if self.mcp else None
        if mcp is not None:
            targets.append(mcp)
        return targets

    # -- self-test ------------------------------------------------------------------------

    def sample_payload(self, event: str) -> Dict[str, Any]:
        common = {"session_id": SESSION, "transcript_path": "{transcript}", "cwd": "/tmp",
                  "hook_event_name": event, "permission_mode": "default"}
        if event == "UserPromptSubmit":
            return {**common, "prompt": "what does ls -la do?"}
        if event == "PostToolUse":
            return {**common, "tool_name": self.shell_tool, "tool_use_id": "toolu_01",
                    "tool_input": {"command": "make build"},
                    "tool_response": {"stdout": "{big_output}", "stderr": "",
                                      "interrupted": False, "isImage": False}}
        if event == "PreCompact":
            return {**common, "trigger": "auto", "custom_instructions": ""}
        return {**common, "source": "compact"}

    def expects_output(self, event: str) -> bool:
        if event == "PostToolUse":
            return bool(getattr(self._adapter(), "replaces_output", False))
        if event == "SessionStart":
            return "PreCompact" in self.hook_events()
        return event == "UserPromptSubmit"


def _home_dir(env: str, default: Path) -> Path:
    override = os.environ.get(env, "").strip()
    return Path(override).expanduser() if override else default


class ClaudeCodeInstaller(ClaudeStyleInstaller):
    name = "claude-code"
    display_name = "Claude Code"
    binaries = ("claude",)
    docs = "https://code.claude.com/docs/en/hooks"
    post_install = ("new Claude Code sessions pick this up; optional on-demand tools: "
                    "claude mcp add --scope user subcortex -- subcortex mcp")

    def settings_path(self) -> Path:
        return _home_dir("CLAUDE_CONFIG_DIR", Path.home() / ".claude") / "settings.json"


class QoderInstaller(ClaudeStyleInstaller):
    name = "qoder"
    display_name = "Qoder CLI"
    binaries = ("qodercli",)
    docs = "https://docs.qoder.com/cli/hooks"
    supports_mcp = True

    def qoder_dir(self) -> Path:
        # Qoder's own resolution: QODER_CONFIG_DIR, else (QODER_CLI_HOME |
        # GEMINI_CLI_HOME | ~)/(QODER_CONFIG_DIR_NAME | .qoder).
        explicit = os.environ.get("QODER_CONFIG_DIR", "").strip()
        if explicit:
            return Path(explicit).expanduser()
        home = (os.environ.get("QODER_CLI_HOME", "").strip() or os.environ.get("GEMINI_CLI_HOME", "").strip())
        name = os.environ.get("QODER_CONFIG_DIR_NAME", "").strip() or ".qoder"
        return (Path(home).expanduser() if home else Path.home()) / name

    def settings_path(self) -> Path:
        return self.qoder_dir() / "settings.json"

    def mcp_target(self) -> Optional[Target]:
        return mcp_json_target(self.settings_path(), self.mcp_command())


class CodeBuddyInstaller(ClaudeStyleInstaller):
    name = "codebuddy"
    display_name = "CodeBuddy Code"
    binaries = ("codebuddy",)
    docs = "https://www.codebuddy.ai/docs/cli/hooks"
    supports_mcp = True

    def codebuddy_dir(self) -> Path:
        return _home_dir("CODEBUDDY_CONFIG_DIR", Path.home() / ".codebuddy")

    def settings_path(self) -> Path:
        return self.codebuddy_dir() / "settings.json"

    def mcp_target(self) -> Optional[Target]:
        return mcp_json_target(self.codebuddy_dir() / ".mcp.json", self.mcp_command())


class DroidInstaller(ClaudeStyleInstaller):
    name = "droid"
    display_name = "Factory Droid"
    binaries = ("droid",)
    docs = "https://docs.factory.ai/reference/hooks-reference"
    wrapper = None  # ~/.factory/hooks.json is keyed by event directly
    shell_tool = "Execute"
    supports_mcp = True
    post_install = ("hooks are snapshotted at startup: restart droid. Hints reach interactive and "
                    "SDK/stream sessions; one-shot `droid exec` skips the prompt hook")

    def factory_dir(self) -> Path:
        return _home_dir("FACTORY_HOME_OVERRIDE", Path.home()) / ".factory"

    def settings_path(self) -> Path:
        return self.factory_dir() / "hooks.json"

    def mcp_target(self) -> Optional[Target]:
        return mcp_json_target(self.factory_dir() / "mcp.json", self.mcp_command(),
                               extra={"disabled": False})

    def sample_payload(self, event: str) -> Dict[str, Any]:
        payload = super().sample_payload(event)
        if event == "SessionStart":  # compaction rotates the session id
            payload["previous_session_id"] = payload["session_id"]
            payload["session_id"] = "a1d4e7f0-2c93-4b6e-9f15-7e0c3b8a2d41"
        return payload


class JunieInstaller(ClaudeStyleInstaller):
    name = "junie"
    display_name = "Junie CLI"
    binaries = ("junie",)
    docs = "https://junie.jetbrains.com/docs/junie-cli-hooks.html"
    supports_mcp = True
    post_install = "hints apply to interactive Junie sessions (batch mode doesn't run hooks)"

    def junie_dir(self) -> Path:
        return _home_dir("JUNIE_HOME", Path.home() / ".junie")

    def settings_path(self) -> Path:
        return self.junie_dir() / "config.json"

    def mcp_target(self) -> Optional[Target]:
        return mcp_json_target(self.junie_dir() / "mcp" / "mcp.json", self.mcp_command())


class DevinInstaller(ClaudeStyleInstaller):
    name = "devin"
    display_name = "Devin CLI"
    binaries = ("devin",)
    docs = "https://docs.devin.ai/cli/extensibility/hooks"
    supports_mcp = True

    def devin_dir(self) -> Path:
        return _home_dir("XDG_CONFIG_HOME", Path.home() / ".config") / "devin"

    def settings_path(self) -> Path:
        return self.devin_dir() / "config.json"

    def mcp_target(self) -> Optional[Target]:
        return mcp_json_target(self.devin_dir() / "mcp_config.json", self.mcp_command())
