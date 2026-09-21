"""OpenCode and Kilo Code CLI: one Bun plugin file (+ optional MCP entry for OpenCode).

Both load every ``*.ts`` in their global plugin directory at startup with no
trust step; the file name is the tag, so uninstall deletes exactly our file.
Kilo is an OpenCode fork with its own directories (it doesn't read OpenCode's).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

from .base import Installer, Target, bundled_plugin, daemon_url, json_target, plugin_file_target


def _config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return Path(xdg) if xdg else Path.home() / ".config"


class OpenCodeInstaller(Installer):
    name = "opencode"
    display_name = "OpenCode"
    seam = "plugin"
    binaries = ("opencode",)
    docs = "https://opencode.ai/docs/plugins/"
    min_version = "1.1.62"
    supports_mcp = True
    post_install = "restart opencode to load the plugin"
    plugin_dir = ("opencode", "plugins")

    def plugin_path(self) -> Path:
        return _config_home().joinpath(*self.plugin_dir) / "subcortex.ts"

    def targets(self) -> List[Target]:
        content = bundled_plugin("opencode", "subcortex.ts").replace("__SUBCORTEX_URL__", daemon_url())
        targets = [plugin_file_target(self.plugin_path(), content)]
        if self.mcp:
            argv = self.mcp_command()

            def add(data: Dict[str, Any]) -> None:
                servers = data.get("mcp")
                if not isinstance(servers, dict):
                    servers = data["mcp"] = {}
                servers["subcortex"] = {"type": "local", "command": argv, "enabled": True}

            def remove(data: Dict[str, Any]) -> None:
                servers = data.get("mcp")
                if isinstance(servers, dict) and servers.pop("subcortex", None) is not None and not servers:
                    del data["mcp"]

            targets.append(json_target(_config_home() / "opencode" / "opencode.json", add, remove,
                                       lambda d: isinstance(d.get("mcp"), dict) and "subcortex" in d["mcp"]))
        return targets


class KiloInstaller(OpenCodeInstaller):
    name = "kilo"
    display_name = "Kilo Code CLI"
    binaries = ("kilo",)
    docs = "https://kilo.ai/docs/cli"
    min_version = None
    supports_mcp = False
    post_install = "restart kilo to load the plugin"
    plugin_dir = ("kilo", "plugin")
