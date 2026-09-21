"""Canonical TUI names and their aliases (single source for CLI, adapters, installers)."""

from __future__ import annotations

from typing import Dict, Optional

ALIASES: Dict[str, str] = {
    "claude": "claude-code",
    "claudecode": "claude-code",
    "codex-cli": "codex",
    "open-code": "opencode",
    "kimi": "kimi-code",
    "kimicode": "kimi-code",
    "qodercli": "qoder",
    "qoder-cli": "qoder",
    "codebuddy-code": "codebuddy",
    "factory": "droid",
    "factory-droid": "droid",
    "junie-cli": "junie",
    "devin-cli": "devin",
    "open-hands": "openhands",
    "ampcode": "amp",
    "gemini": "gemini-cli",
    "qwen": "qwen-code",
    "cursor-agent": "cursor",
    "cursor-cli": "cursor",
    "copilot-cli": "copilot",
    "github-copilot": "copilot",
    "interpreter": "open-interpreter",
    "openinterpreter": "open-interpreter",
    "kilocode": "kilo",
    "kilo-code": "kilo",
    "grok": "grok-build",
    "cagent": "docker-agent",
    "docker": "docker-agent",
    "letta-code": "letta",
    "mistral-vibe": "vibe",
    "augment": "auggie",
    "kiro-cli": "kiro",
    "cline-cli": "cline",
}


def canonical(name: str, known) -> Optional[str]:
    """Canonical id for ``name`` if it (or its alias) is in ``known``."""
    key = str(name or "").strip().lower().replace("_", "-")
    if key in known:
        return key
    alias = ALIASES.get(key) or ALIASES.get(key.replace("-", ""))
    return alias if alias in known else None
