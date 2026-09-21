"""OpenHands CLI: ``~/.openhands/hooks.json`` (+ optional ``mcp.json``).

OpenHands loads exactly one hooks file — a project ``.openhands/hooks.json``
replaces the user one entirely — and a file with an unknown event key or both
key styles (``user_prompt_submit`` and ``UserPromptSubmit``) breaks conversation
setup. So we reuse whatever style the file already has and add nothing else.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from .base import Installer, Target, add_grouped, json_target, mcp_json_target, remove_grouped

SNAKE, PASCAL = "user_prompt_submit", "UserPromptSubmit"
_PASCAL_KEYS = {"PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionStart", "SessionEnd"}


def _table(data: Dict[str, Any]) -> Dict[str, Any]:
    wrapper = data.get("hooks")
    return wrapper if isinstance(wrapper, dict) else data


def persistence_dir() -> Path:
    override = os.environ.get("OPENHANDS_PERSISTENCE_DIR", "").strip()
    return Path(override) if override else Path.home() / ".openhands"


class OpenHandsInstaller(Installer):
    name = "openhands"
    display_name = "OpenHands CLI"
    seam = "hooks"
    binaries = ("openhands",)
    docs = "https://docs.openhands.dev/openhands/usage/customization/hooks"
    min_version = "1.12.0"
    supports_mcp = True
    post_install = "applies to new OpenHands conversations"

    def _entry(self) -> Dict[str, Any]:
        return {"type": "command", "command": self.command("UserPromptSubmit"), "timeout": 10}

    def targets(self) -> List[Target]:
        def add(data: Dict[str, Any]) -> None:
            table = _table(data)
            key = PASCAL if any(k in _PASCAL_KEYS for k in table) else SNAKE
            add_grouped(table, key, "*", self._entry())

        def remove(data: Dict[str, Any]) -> None:
            remove_grouped(_table(data))

        def installed(data: Dict[str, Any]) -> bool:
            probe = {k: v for k, v in _table(data).items() if isinstance(v, list)}
            return bool(remove_grouped(probe))

        targets = [json_target(Path.home() / ".openhands" / "hooks.json", add, remove, installed)]
        if self.mcp:
            targets.append(mcp_json_target(
                persistence_dir() / "mcp.json", self.mcp_command(),
                extra={"transport": "stdio", "enabled": True}))
        return targets

    def warnings(self) -> List[str]:
        try:
            project = Path.cwd() / ".openhands" / "hooks.json"
        except OSError:  # cwd was deleted
            return []
        if project.is_file():
            return [f"{project} exists and replaces the user-level hooks file inside this "
                    "project; subcortex will not run here unless you add it there too"]
        return []

    def hook_events(self) -> List[str]:
        return ["UserPromptSubmit"]

    def sample_payload(self, event: str) -> Dict[str, Any]:
        return {"event_type": "UserPromptSubmit", "tool_name": None, "tool_input": None,
                "tool_response": None, "message": "what does ls -la do?",
                "session_id": "4f1c2a9e-8b7d-4e21-9c3a-0d5e6f7a8b9c",
                "working_dir": "/tmp", "metadata": {}}

    def expects_output(self, event: str) -> bool:
        return True
