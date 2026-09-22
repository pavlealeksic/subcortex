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

import json
from pathlib import Path
from typing import Any, Dict, Optional

from .base import TOOL_OUTPUT, HookEvent, Response
from .claude_family import ALL_EVENTS, ClaudeStyleAdapter

CODEX_TRUNCATED = "Warning: truncated output"


def _exit_status(payload: Dict[str, Any]) -> Optional[int]:
    """The command's exit code. The payload has none, but by the time the hook
    runs the session log (``transcript_path``) records the finished command as
    an ``item_completed`` CommandExecution with ``exit_code``. None if unknown."""
    call_id, path = payload.get("tool_use_id"), payload.get("transcript_path")
    if not isinstance(call_id, str) or not call_id or not isinstance(path, str) or not path:
        return None
    try:
        from ..transcript import read_tail

        for line in reversed(read_tail(Path(path)).splitlines()):
            if call_id not in line or "CommandExecution" not in line:
                continue
            item = ((json.loads(line).get("payload") or {}).get("item") or {})
            if item.get("type") == "CommandExecution" and item.get("id") == call_id:
                code = item.get("exit_code")
                if item.get("status") == "failed" and not (isinstance(code, int) and code != 0):
                    return 1
                return code if isinstance(code, int) and not isinstance(code, bool) else None
    except Exception:
        return None
    return None


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
        if kind == TOOL_OUTPUT and event.output is not None:
            # A failed command must never be trimmed: the model has to see why.
            code = _exit_status(payload)
            event.extra["exit_code"] = code
            event.failed = event.failed or (code is not None and code != 0)
        return event

    def render_tool_output(self, event: HookEvent, replacement: str) -> Response:
        removed = len(event.output or "") - len(replacement)
        # `reason` replaces the whole result, Codex's exit-status header included.
        if event.extra.get("exit_code") == 0:
            replacement = "Process exited with code 0\n" + replacement
        return {
            "continue": False,
            "stopReason": f"subcortex: trimmed {max(removed, 0)} chars of low-value output",
            "reason": replacement,
        }


class OpenInterpreterAdapter(CodexAdapter):
    name = "open-interpreter"
    display_name = "Open Interpreter"
