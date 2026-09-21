"""Pi and Cline CLI: one TypeScript plugin file each (+ optional MCP for Cline).

Both load every file in their plugin directory, so the plugin is a single
self-contained file with type-only imports. Pi exits on a plugin load error
and Cline fails the run on a hook error or a hook slower than 3 s — the
plugins catch everything and race every hook against a deadline, and
tests/test_plugins.py runs them under Bun against a live daemon.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

from .base import Installer, Target, rendered_plugin, mcp_json_target, plugin_file_target
from .mcp_only import cline_dir, cline_mcp_path


class PiInstaller(Installer):
    name = "pi"
    display_name = "Pi"
    seam = "plugin"
    binaries = ("pi",)
    docs = "https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md"
    min_version = "0.87.0"
    post_install = "new pi sessions load the extension"

    def extensions_dir(self) -> Path:
        override = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
        return (Path(override) if override else Path.home() / ".pi" / "agent") / "extensions"

    def targets(self) -> List[Target]:
        content = rendered_plugin("pi")
        return [plugin_file_target(self.extensions_dir() / "subcortex.ts", content)]


class ClineInstaller(Installer):
    name = "cline"
    display_name = "Cline CLI"
    seam = "plugin"
    binaries = ("cline",)
    docs = "https://docs.cline.bot/sdk/plugins"
    min_version = "3.0.62"
    supports_mcp = True
    post_install = "new cline runs load the plugin"

    def targets(self) -> List[Target]:
        content = rendered_plugin("cline")
        targets = [plugin_file_target(cline_dir() / "plugins" / "subcortex.ts", content)]
        if self.mcp:
            targets.append(mcp_json_target(cline_mcp_path(), self.mcp_command(), extra={"type": "stdio"}))
        return targets
