"""Crush: register the ``subcortex mcp`` server in the global ``crush.json``.

Crush (>= v0.64) only has a ``PreToolUse`` hook — no prompt, post-tool or
compaction events — and it already truncates bash output itself, so its seam
is MCP. Crush refuses to start on invalid JSON (comments included), so the
file is only ever rewritten as strict JSON, and refused if it isn't.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

from .base import Installer, Target, mcp_json_target


def crush_config_dir() -> Path:
    override = os.environ.get("CRUSH_GLOBAL_CONFIG", "").strip()
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return (Path(xdg) if xdg else Path.home() / ".config") / "crush"


class CrushInstaller(Installer):
    name = "crush"
    display_name = "Crush"
    seam = "mcp"
    binaries = ("crush",)
    docs = "https://github.com/charmbracelet/crush#mcps"
    post_install = "restart crush to load the MCP server (tools appear as mcp_subcortex_*)"

    def targets(self) -> List[Target]:
        return [mcp_json_target(crush_config_dir() / "crush.json", self.mcp_command(), key="mcp",
                                extra={"type": "stdio", "timeout": 10})]
