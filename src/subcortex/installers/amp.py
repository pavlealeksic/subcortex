"""Amp: a Bun plugin in the system plugin dir (+ optional ``amp.mcpServers``).

Amp's Neo plugin API has no command hooks, but its plugins can append hidden
context to a prompt (``agent.start``) and replace a tool result
(``tool.result``), so the plugin covers behaviors 1 and 2 natively and 3/4 via
a rolling snapshot plus compaction detection. See ``plugins/amp/subcortex.ts``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

from .base import Installer, Target, bundled_plugin, daemon_url, mcp_json_target, plugin_file_target


def amp_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return (Path(xdg) if xdg else Path.home() / ".config") / "amp"


class AmpInstaller(Installer):
    name = "amp"
    display_name = "Amp"
    seam = "plugin"
    binaries = ("amp",)
    docs = "https://ampcode.com/docs/customize/plugins"
    supports_mcp = True
    post_install = ("reload plugins in Amp (Ctrl+O → plugins: reload) or restart it; "
                    "for `amp -x` runs pass --plugin-ready-timeout")

    def targets(self) -> List[Target]:
        content = bundled_plugin("amp", "subcortex.ts").replace("__SUBCORTEX_URL__", daemon_url())
        targets = [plugin_file_target(amp_config_dir() / "plugins" / "subcortex.ts", content)]
        if self.mcp:
            targets.append(mcp_json_target(amp_config_dir() / "settings.json", self.mcp_command(),
                                           key="amp.mcpServers"))
        return targets
