"""Codex CLI (``$CODEX_HOME/hooks.json``) and Open Interpreter (``~/.openinterpreter/hooks.json``).

``hooks.json`` rather than inline ``[hooks]`` in ``config.toml``: Codex
writes its own hook-trust state into ``config.toml``, so a JSON file we merge
into is the exact-uninstall target. ``config.toml`` is still touched for two
things: removing the (malformed) block subcortex 0.1.0 wrote there, and, with
``--mcp``, a marked ``[mcp_servers.subcortex]`` block.

Codex only runs non-managed hooks after the user trusts them (``/hooks``);
until then they are skipped, which is fail-safe.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import (InstallError, Target, append_block, has_block, strip_block, toml_str)
from .claude_family import SESSION, ClaudeStyleInstaller

TRUST = ("run /hooks inside {name} once and trust the subcortex hooks (hooks are hash-verified; "
         "they are skipped until trusted)")


def _toml_target(path: Path, body: Optional[str], tui: str) -> Target:
    """config.toml: drop any subcortex block (0.1.0 hooks included); add ``body`` if given."""
    import tomllib

    def check(text: str) -> str:
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise InstallError(f"{path}: result would not be valid TOML ({exc}); refusing to write") from exc
        return text

    def merge(text: str) -> str:
        if body is None:
            return check(strip_block(text)) if has_block(text) else text
        return check(append_block(text, body, tui))

    def unmerge(text: str) -> str:
        return check(strip_block(text)) if has_block(text) else text

    return Target(path, merge, unmerge, has_block)


class _CodexEngineInstaller(ClaudeStyleInstaller):
    supports_mcp = True

    def home(self) -> Path:
        raise NotImplementedError

    def settings_path(self) -> Path:
        return self.home() / "hooks.json"

    def matcher(self, event: str) -> Optional[str]:
        return {"PostToolUse": "^Bash$", "SessionStart": "^compact$"}.get(event)

    def targets(self) -> List[Target]:
        targets = [t for t in super().targets() if t.path == self.settings_path()]
        body = None
        if self.mcp:
            argv = self.mcp_command()
            body = "\n".join([
                "[mcp_servers.subcortex]",
                f"command = {toml_str(argv[0])}",
                f"args = [{', '.join(toml_str(a) for a in argv[1:])}]",
                "startup_timeout_sec = 10",
            ]) + "\n"
        targets.append(_toml_target(self.home() / "config.toml", body, self.name))
        return targets

    def sample_payload(self, event: str) -> Dict[str, Any]:
        common = {"session_id": SESSION, "transcript_path": "{transcript}", "cwd": "/tmp",
                  "hook_event_name": event, "model": "gpt-5.5-codex"}
        if event == "UserPromptSubmit":
            return {**common, "turn_id": "0", "permission_mode": "default", "prompt": "what does ls -la do?"}
        if event == "PostToolUse":
            return {**common, "turn_id": "3", "permission_mode": "default", "tool_name": "Bash",
                    "tool_use_id": "call_1", "tool_input": {"command": "make build"},
                    "tool_response": "{big_output}"}
        if event == "PreCompact":
            return {**common, "turn_id": "7", "trigger": "auto"}
        return {**common, "permission_mode": "default", "source": "compact"}


class CodexInstaller(_CodexEngineInstaller):
    name = "codex"
    display_name = "Codex CLI"
    binaries = ("codex",)
    docs = "https://developers.openai.com/codex/hooks"
    min_version = "0.133.0"
    post_install = TRUST.format(name="codex")

    def home(self) -> Path:
        override = os.environ.get("CODEX_HOME", "").strip()
        return Path(override) if override else Path.home() / ".codex"


class OpenInterpreterInstaller(_CodexEngineInstaller):
    name = "open-interpreter"
    display_name = "Open Interpreter"
    binaries = ("interpreter",)
    docs = "https://github.com/openinterpreter/openinterpreter/blob/main/docs/hooks.md"
    post_install = TRUST.format(name="interpreter")

    def home(self) -> Path:
        return Path.home() / ".openinterpreter"
