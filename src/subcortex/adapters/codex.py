"""OpenAI Codex CLI (>= 0.133) and Open Interpreter (a Rust fork on the same hook engine).

Claude-like event names and ``hookSpecificOutput`` envelope, with Codex's own
semantics:

- Every output struct is ``deny_unknown_fields``: any extra key makes the run
  "Failed" and the output is ignored — so only schema keys are ever emitted.
- ``PostToolUse``: ``tool_response`` is the model-facing text (a string, no
  exit code). ``{"decision": "block"}`` would hand the model a *failed* tool
  call; ``{"continue": false, "stopReason": S, "reason": R}`` replaces the
  result with ``R`` and the turn continues — the one deliberate ``continue:
  false`` subcortex ever emits, allowed for this event only. Output Codex
  already truncated (``Warning: truncated output``) is left alone.
- ``SessionStart`` with ``source: "compact"`` runs before the next model
  request after compaction; same ``session_id`` as before.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base import TOOL_OUTPUT, HookEvent, Response
from .claude_family import ALL_EVENTS, ClaudeStyleAdapter

CODEX_TRUNCATED = "Warning: truncated output"


class CodexAdapter(ClaudeStyleAdapter):
    name = "codex"
    display_name = "Codex CLI"
    events = ALL_EVENTS
    replaces_output = True
    allowed_blocking = ((TOOL_OUTPUT, "continue", False),)

    def parse(self, name: str, kind: str, payload: Dict[str, Any]) -> Optional[HookEvent]:
        event = super().parse(name, kind, payload)
        if kind == TOOL_OUTPUT and isinstance(event.output, str) \
                and event.output.lstrip().startswith(CODEX_TRUNCATED):
            event.output = None  # Codex already head/tail-truncated it
        return event

    def render_tool_output(self, event: HookEvent, replacement: str) -> Response:
        removed = len(event.output or "") - len(replacement)
        return {
            "continue": False,
            "stopReason": f"subcortex: trimmed {max(removed, 0)} chars of low-value output",
            "reason": replacement,
        }


class OpenInterpreterAdapter(CodexAdapter):
    name = "open-interpreter"
    display_name = "Open Interpreter"
