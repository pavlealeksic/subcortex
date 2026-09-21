"""Registry of command-hook adapters (lazy: a hook process imports only its own)."""

from __future__ import annotations

import importlib
from typing import Dict, List, Optional

from ..tuis import canonical
from .base import HookAdapter

# canonical name -> "module:Class" (relative to this package)
_ADAPTERS: Dict[str, str] = {
    "claude-code": "claude_family:ClaudeCodeAdapter",
    "qoder": "claude_family:QoderAdapter",
    "codebuddy": "claude_family:CodeBuddyAdapter",
    "droid": "claude_family:DroidAdapter",
    "junie": "claude_family:JunieAdapter",
    "devin": "claude_family:DevinAdapter",
    "codex": "codex:CodexAdapter",
    "open-interpreter": "codex:OpenInterpreterAdapter",
    "gemini-cli": "gemini_family:GeminiCliAdapter",
    "qwen-code": "gemini_family:QwenCodeAdapter",
    "cursor": "cursor:CursorAdapter",
    "copilot": "copilot:CopilotAdapter",
    "kimi-code": "kimi_code:KimiCodeAdapter",
    "openhands": "openhands:OpenHandsAdapter",
    "grok-build": "grok:GrokBuildAdapter",
    "docker-agent": "docker_agent:DockerAgentAdapter",
    "letta": "letta_vibe:LettaAdapter",
    "vibe": "letta_vibe:VibeAdapter",
}


def canonical_name(name: str) -> Optional[str]:
    return canonical(name, _ADAPTERS)


def get_adapter(name: str) -> Optional[HookAdapter]:
    key = canonical_name(name)
    if key is None:
        return None
    module_name, cls_name = _ADAPTERS[key].split(":")
    module = importlib.import_module(f".{module_name}", __name__)
    return getattr(module, cls_name)()


def names() -> List[str]:
    return sorted(_ADAPTERS)
