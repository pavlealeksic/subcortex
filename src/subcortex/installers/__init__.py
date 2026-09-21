"""Registry of TUI installers (hook-, plugin- and MCP-based)."""

from __future__ import annotations

import importlib
from typing import Dict, List, Optional

from ..tuis import canonical
from .base import Installer

# canonical name -> "module:Class" (relative to this package)
_INSTALLERS: Dict[str, str] = {
    # command hooks
    "claude-code": "claude_family:ClaudeCodeInstaller",
    "qoder": "claude_family:QoderInstaller",
    "codebuddy": "claude_family:CodeBuddyInstaller",
    "droid": "claude_family:DroidInstaller",
    "junie": "claude_family:JunieInstaller",
    "devin": "claude_family:DevinInstaller",
    "codex": "codex:CodexInstaller",
    "open-interpreter": "codex:OpenInterpreterInstaller",
    "gemini-cli": "gemini_family:GeminiCliInstaller",
    "qwen-code": "gemini_family:QwenCodeInstaller",
    "cursor": "cursor:CursorInstaller",
    "copilot": "copilot:CopilotInstaller",
    "kimi-code": "kimi_code:KimiCodeInstaller",
    "openhands": "openhands:OpenHandsInstaller",
    "grok-build": "more_hooks:GrokBuildInstaller",
    "docker-agent": "more_hooks:DockerAgentInstaller",
    "letta": "more_hooks:LettaInstaller",
    "vibe": "more_hooks:VibeInstaller",
    # plugins
    "opencode": "opencode:OpenCodeInstaller",
    "kilo": "opencode:KiloInstaller",
    "amp": "amp:AmpInstaller",
    # MCP only
    "crush": "crush:CrushInstaller",
    "goose": "goose:GooseInstaller",
    "warp": "mcp_only:WarpInstaller",
    "zed": "mcp_only:ZedInstaller",
    "auggie": "mcp_only:AuggieInstaller",
    "cline": "mcp_only:ClineInstaller",
    "kiro": "mcp_only:KiroInstaller",
}


def canonical_name(name: str) -> Optional[str]:
    return canonical(name, _INSTALLERS)


def get_installer(name: str, mcp: bool = False) -> Optional[Installer]:
    key = canonical_name(name)
    if key is None:
        return None
    module_name, cls_name = _INSTALLERS[key].split(":")
    module = importlib.import_module(f".{module_name}", __name__)
    return getattr(module, cls_name)(mcp=mcp)


def names() -> List[str]:
    return sorted(_INSTALLERS)
