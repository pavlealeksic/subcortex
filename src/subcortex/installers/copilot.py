"""GitHub Copilot CLI: a hooks file we own, ``$COPILOT_HOME/hooks/subcortex.json``.

Copilot loads every ``*.json`` in its user hooks directory, so subcortex gets
its own file (uninstall deletes it) and never touches ``settings.json``.
Hooks are read at startup: restart copilot after installing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

from .base import Installer, Target, mcp_json_target, owned_file_target

EVENTS = {"userPromptSubmitted": (None, 5), "postToolUse": ("bash|powershell", 10), "preCompact": (None, 10)}
SESSION = "8c5d7b2e-3f41-4d0a-9c6b-1e2f3a4b5c6d"


def copilot_home() -> Path:
    override = os.environ.get("COPILOT_HOME", "").strip()
    return Path(override) if override else Path.home() / ".copilot"


class CopilotInstaller(Installer):
    name = "copilot"
    display_name = "GitHub Copilot CLI"
    seam = "hooks"
    binaries = ("copilot",)
    docs = "https://docs.github.com/en/copilot/reference/hooks-reference"
    min_version = "1.0.67"
    supports_mcp = True
    post_install = "restart copilot to load the hooks"

    def _content(self) -> str:
        hooks: Dict[str, List[Dict[str, Any]]] = {}
        for event, (matcher, timeout) in EVENTS.items():
            entry: Dict[str, Any] = {"type": "command", "bash": self.command(event), "timeoutSec": timeout}
            if matcher:
                entry["matcher"] = matcher
            hooks[event] = [entry]
        return json.dumps({"version": 1, "hooks": hooks}, indent=2) + "\n"

    def targets(self) -> List[Target]:
        targets = [owned_file_target(copilot_home() / "hooks" / "subcortex.json", self._content())]
        if self.mcp:
            targets.append(mcp_json_target(copilot_home() / "mcp-config.json", self.mcp_command(),
                                           extra={"type": "local", "env": {}, "tools": ["*"]}))
        return targets

    def hook_events(self) -> List[str]:
        return list(EVENTS)

    def sample_payload(self, event: str) -> Dict[str, Any]:
        common = {"sessionId": SESSION, "timestamp": 1790019371523, "cwd": "/tmp"}
        if event == "userPromptSubmitted":
            return {**common, "prompt": "what does ls -la do?"}
        if event == "postToolUse":
            return {**common, "toolName": "bash", "toolArgs": {"command": "make build"},
                    "toolResult": {"resultType": "success", "textResultForLlm": "{big_output}"}}
        return {**common, "transcriptPath": "{transcript}", "trigger": "auto", "customInstructions": ""}

    def expects_output(self, event: str) -> bool:
        return event in ("userPromptSubmitted", "postToolUse")
