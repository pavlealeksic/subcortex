"""Grok Build (xAI ``grok`` >= 1.0.40), verified end to end against 1.0.40.

- Prompt-submit output is discarded by Grok (no hint possible).
- ``PostToolUse`` can genuinely replace a shell result — but only with the
  complete tagged ``ToolOutput`` object: the received ``toolResult`` with
  ``output_for_prompt`` replaced and ``output: []`` (Grok's own doc example is
  rejected). The ``exit: N …`` header line is kept verbatim; truncated,
  backgrounded or non-Bash results are left alone.
- ``PreCompact`` is observe-only; the model's view lives in
  ``chat_history.jsonl`` next to ``transcriptPath``. ``PostCompact`` output is
  ignored, so the snapshot rides the next shell call as ``additionalContext``
  (the only model-visible channel Grok gives command hooks).
- Any ``decision``/``permissionDecision``/``continue:false`` would block or
  steer; subcortex only ever writes ``updatedToolOutput`` and ``additionalContext``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from .. import policy
from .base import POST_COMPACT, PRE_COMPACT, TOOL_OUTPUT, HookAdapter, HookEvent, Response

MAX_CONTEXT_CHARS = 9000


def _split_header(text: str):
    """(``exit: N …`` header incl. newline or "", body)."""
    if text.startswith("exit: "):
        newline = text.find("\n")
        if newline != -1:
            return text[:newline + 1], text[newline + 1:]
    return "", text


class GrokBuildAdapter(HookAdapter):
    name = "grok-build"
    display_name = "Grok Build"
    events = {"PostToolUse": TOOL_OUTPUT, "PreCompact": PRE_COMPACT, "PostCompact": POST_COMPACT}

    def parse(self, name: str, kind: str, payload: Dict[str, Any]) -> Optional[HookEvent]:
        event = HookEvent(kind=kind, name=name, payload=payload,
                          session_id=str(payload.get("sessionId") or payload.get("session_id") or ""),
                          transcript_path=str(payload.get("transcriptPath") or ""),
                          trigger=str(payload.get("source") or ""))
        if kind == PRE_COMPACT and event.transcript_path:
            chat = Path(event.transcript_path).parent / "chat_history.jsonl"
            if chat.is_file():
                event.transcript_path = str(chat)
        elif kind == TOOL_OUTPUT:
            from ..config import load_config

            restored = policy.restore_snapshot(event.session_id, load_config(), require_ready=True)
            if restored:
                event.extra["context"] = restored[:MAX_CONTEXT_CHARS]
            result = payload.get("toolResult")
            if payload.get("toolResultTruncated") or not isinstance(result, dict) \
                    or result.get("type") != "Bash" or result.get("signal") == "backgrounded":
                return event
            text = result.get("output_for_prompt")
            if not isinstance(text, str):
                return event
            header, body = _split_header(text)
            event.extra["header"] = header
            event.output = body
            event.tool = str(payload.get("toolName") or "run_terminal_command")
            event.tool_input = payload.get("toolInput")
            code = result.get("exit_code")
            event.failed = bool(result.get("timed_out")) or (isinstance(code, int) and code != 0)
        return event

    def render_tool_output(self, event: HookEvent, replacement: Optional[str]) -> Response:
        specific: Dict[str, Any] = {"hookEventName": "PostToolUse"}
        result = event.payload.get("toolResult")
        if replacement and isinstance(result, dict):
            specific["updatedToolOutput"] = {**result, "output": [],
                                             "output_for_prompt": event.extra.get("header", "") + replacement}
        if event.extra.get("context"):
            specific["additionalContext"] = event.extra["context"]
        return {"hookSpecificOutput": specific} if len(specific) > 1 else None

    def guard(self, kind: str, response: Response) -> Response:
        if isinstance(response, dict) and set(response) - {"hookSpecificOutput"}:
            return None  # only hookSpecificOutput is ever safe in Grok
        return super().guard(kind, response)


def grok_home() -> Path:
    override = os.environ.get("GROK_HOME", "").strip()
    return Path(override) if override else Path.home() / ".grok"
