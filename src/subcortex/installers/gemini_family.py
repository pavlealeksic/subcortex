"""Gemini CLI (``~/.gemini/settings.json``) and Qwen Code (``~/.qwen/settings.json``).

Entries are tagged ``name: "subcortex:<Event>"`` (Gemini's dedupe key and the
``/hooks disable`` handle) plus ``description: "managed-by=subcortex"``.
Timeouts are milliseconds in Gemini; Qwen reads any value >= 1000 as ms on
every version, so both get ms.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from .base import Target, mcp_json_target
from .claude_family import ClaudeStyleInstaller

TIMEOUT_MS = 10000


class _GeminiStyleInstaller(ClaudeStyleInstaller):
    supports_mcp = True

    def entry(self, event: str) -> Dict[str, Any]:
        return {"type": "command", "name": f"subcortex:{event}",
                "description": "managed-by=subcortex",
                "command": self.command(event), "timeout": TIMEOUT_MS}

    def mcp_target(self) -> Optional[Target]:
        return mcp_json_target(self.settings_path(), self.mcp_command(),
                               extra={"description": "managed-by=subcortex"})


class GeminiCliInstaller(_GeminiStyleInstaller):
    name = "gemini-cli"
    display_name = "Gemini CLI"
    binaries = ("gemini",)
    docs = "https://geminicli.com/docs/hooks/"
    min_version = "0.27.0"
    post_install = ("restart gemini; hooks (even user-level ones) only run in trusted folders, "
                    "and the compaction snapshot is taken on /compress")

    def settings_path(self) -> Path:
        home = os.environ.get("GEMINI_CLI_HOME", "").strip()
        return (Path(home) if home else Path.home()) / ".gemini" / "settings.json"

    def matcher(self, event: str) -> Optional[str]:
        # PreCompress fires before *every* model request; lifecycle matchers are
        # exact strings, so this runs us only for a real /compress.
        return "manual" if event == "PreCompress" else None

    def sample_payload(self, event: str) -> Dict[str, Any]:
        common = {"session_id": "3f0c2a7e-5b1d-4c8e-9a41-2d7e6b0f9c13", "transcript_path": "{transcript}",
                  "cwd": "/tmp", "hook_event_name": event, "timestamp": "2026-09-21T14:02:11.512Z"}
        if event == "BeforeAgent":
            return {**common, "prompt": "what does ls -la do?"}
        return {**common, "trigger": "manual"}

    def expects_output(self, event: str) -> bool:
        return event == "BeforeAgent"


class QwenCodeInstaller(_GeminiStyleInstaller):
    name = "qwen-code"
    display_name = "Qwen Code"
    binaries = ("qwen",)
    docs = "https://qwenlm.github.io/qwen-code-docs/en/users/features/hooks/"
    min_version = "0.16.0"
    post_install = "restart qwen (or open /hooks to reload)"

    def settings_path(self) -> Path:
        home = os.environ.get("QWEN_HOME", "").strip()
        return (Path(home) if home else Path.home() / ".qwen") / "settings.json"

    def matcher(self, event: str) -> Optional[str]:
        return "^compact$" if event == "SessionStart" else None

    def sample_payload(self, event: str) -> Dict[str, Any]:
        payload = super().sample_payload(event)
        payload["timestamp"] = "2026-09-21T14:02:11.512Z"
        if event == "UserPromptSubmit":
            payload["submitted_prompt"] = payload["prompt"]
        return payload
