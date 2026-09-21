"""Cursor CLI: ``~/.cursor/hooks.json`` (flat, versioned) + optional ``~/.cursor/mcp.json``.

Two ways to disable *every* hook in that file, both avoided here: an unknown
event key, and any ``//`` inside a string (Cursor strips comments naively).
The path is hard-coded to the real home directory by Cursor
(``CURSOR_CONFIG_DIR`` does not move it).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .base import InstallError, Installer, Target, add_flat, json_target, mcp_json_target, remove_flat

EVENTS = {"beforeSubmitPrompt": 5, "preCompact": 10}
CONVERSATION = "5b0d6c1e-8a4f-4f1e-9d8e-2b7f1c3a9e10"


class CursorInstaller(Installer):
    name = "cursor"
    display_name = "Cursor CLI"
    seam = "hooks"
    binaries = ("agent", "cursor-agent")
    docs = "https://cursor.com/docs/hooks"
    supports_mcp = True
    post_install = "prompt hints fire in interactive sessions (Cursor skips beforeSubmitPrompt in -p mode)"

    def cursor_dir(self) -> Path:
        return Path.home() / ".cursor"

    def _entry(self, event: str) -> Dict[str, Any]:
        command = self.command(event)
        if "//" in command:
            raise InstallError("the hook command contains '//', which Cursor's hooks.json parser "
                               f"would treat as a comment: {command}")
        return {"command": command, "timeout": EVENTS[event]}

    def targets(self) -> List[Target]:
        def add(data: Dict[str, Any]) -> None:
            data.setdefault("version", 1)
            table = data.get("hooks")
            if not isinstance(table, dict):
                table = data["hooks"] = {}
            for event in EVENTS:
                add_flat(table, event, self._entry(event))

        def remove(data: Dict[str, Any]) -> None:
            table = data.get("hooks")
            if isinstance(table, dict):
                remove_flat(table)
            if data == {"version": 1, "hooks": {}}:
                data.clear()  # only ever held our entries

        def installed(data: Dict[str, Any]) -> bool:
            table = data.get("hooks")
            return isinstance(table, dict) and bool(remove_flat(json.loads(json.dumps(table))))

        targets = [json_target(self.cursor_dir() / "hooks.json", add, remove, installed)]
        if self.mcp:
            targets.append(mcp_json_target(self.cursor_dir() / "mcp.json", self.mcp_command(),
                                           extra={"type": "stdio", "env": {}}))
        return targets

    def hook_events(self) -> List[str]:
        return list(EVENTS)

    def sample_payload(self, event: str) -> Dict[str, Any]:
        common = {"conversation_id": CONVERSATION, "generation_id": "f2a9c0d4", "model": "composer-2.5",
                  "session_id": CONVERSATION, "hook_event_name": event,
                  "cursor_version": "2026.09.18-9a7762b", "workspace_roots": ["/tmp"],
                  "transcript_path": "{transcript}"}
        if event == "beforeSubmitPrompt":
            return {**common, "prompt": "what does ls -la do?", "attachments": []}
        return {**common, "trigger": "auto", "context_usage_percent": 91, "message_count": 146}

    def expects_output(self, event: str) -> bool:
        return event == "beforeSubmitPrompt"
