"""MCP-only integrations: TUIs whose extension seam is an MCP server entry.

None of these offers a hook that can inject context or replace tool output,
so subcortex registers ``subcortex mcp`` (on-demand decision tools). Every
target file is plain JSON; files with comments are refused, never rewritten.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from .base import Installer, Target, json_target, mcp_json_target


class _McpInstaller(Installer):
    seam = "mcp"
    extra: Dict[str, Any] = {}

    def mcp_path(self) -> Path:
        raise NotImplementedError

    def targets(self) -> List[Target]:
        return [mcp_json_target(self.mcp_path(), self.mcp_command(), extra=dict(self.extra))]


class WarpInstaller(_McpInstaller):
    name = "warp"
    display_name = "Warp"
    binaries = ("warp", "warp-terminal")
    docs = "https://docs.warp.dev/agents/capabilities/mcp"
    post_install = "global MCP servers start automatically in Warp's agent"

    def mcp_path(self) -> Path:
        return Path.home() / ".warp" / ".mcp.json"


class AuggieInstaller(_McpInstaller):
    name = "auggie"
    display_name = "Augment Auggie CLI"
    binaries = ("auggie",)
    docs = "https://docs.augmentcode.com/cli/integrations"

    def mcp_path(self) -> Path:
        return Path.home() / ".augment" / "settings.json"


class ClineInstaller(_McpInstaller):
    name = "cline"
    display_name = "Cline CLI"
    binaries = ("cline",)
    docs = "https://docs.cline.bot/mcp/configuring-mcp-servers"
    extra = {"type": "stdio"}

    def mcp_path(self) -> Path:
        explicit = os.environ.get("CLINE_MCP_SETTINGS_PATH", "").strip()
        if explicit:
            return Path(explicit)
        data = os.environ.get("CLINE_DATA_DIR", "").strip()
        if data:
            return Path(data) / "settings" / "cline_mcp_settings.json"
        base = os.environ.get("CLINE_DIR", "").strip()
        root = Path(base) if base else Path.home() / ".cline"
        return root / "data" / "settings" / "cline_mcp_settings.json"


class KiroInstaller(_McpInstaller):
    name = "kiro"
    display_name = "Kiro CLI"
    binaries = ("kiro-cli",)
    docs = "https://kiro.dev/docs/mcp/configuration"
    extra = {"disabled": False}
    post_install = "custom Kiro agents only see it with \"includeMcpJson\": true"

    def mcp_path(self) -> Path:
        home = os.environ.get("KIRO_HOME", "").strip()
        return (Path(home) if home else Path.home() / ".kiro") / "settings" / "mcp.json"


class ZedInstaller(Installer):
    """Zed's settings.json is JSONC; its sibling global_settings.json (merged
    under user settings, never written by Zed) holds our entry instead."""

    name = "zed"
    display_name = "Zed (agent panel)"
    seam = "mcp"
    binaries = ("zed",)
    docs = "https://zed.dev/docs/ai/mcp"
    post_install = "Zed asks for confirmation the first time a subcortex tool runs"

    def config_dir(self) -> Path:
        if sys.platform.startswith("linux"):
            xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
            return (Path(xdg) if xdg else Path.home() / ".config") / "zed"
        return Path.home() / ".config" / "zed"

    def targets(self) -> List[Target]:
        argv = self.mcp_command()

        def add(data: Dict[str, Any]) -> None:
            servers = data.get("context_servers")
            if not isinstance(servers, dict):
                servers = data["context_servers"] = {}
            servers["subcortex"] = {"command": argv[0], "args": argv[1:], "env": {}}

        def remove(data: Dict[str, Any]) -> None:
            servers = data.get("context_servers")
            if isinstance(servers, dict) and servers.pop("subcortex", None) is not None and not servers:
                del data["context_servers"]

        return [json_target(self.config_dir() / "global_settings.json", add, remove,
                            lambda d: isinstance(d.get("context_servers"), dict)
                            and "subcortex" in d["context_servers"])]
