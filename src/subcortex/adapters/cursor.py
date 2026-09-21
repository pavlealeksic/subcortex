"""Cursor CLI (``agent`` / ``cursor-agent``, builds >= 2026.05.20).

- ``beforeSubmitPrompt`` → ``{"additional_context": ...}`` (> 10,000 chars
  and Cursor drops it entirely, so we stay under). Only fires in interactive
  sessions, not ``-p``.
- ``postToolUse`` can't replace Shell output (only MCP results): not registered.
- ``preCompact`` is observe-only and fires when compaction happens; there is
  no post-compaction event (``sessionStart`` is new chats only), so the
  snapshot is marked ready right away and delivered with the next prompt.
- Never ``continue``/``permission``/``decision`` keys; exit 2 would reject the prompt.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base import PRE_COMPACT, PROMPT, HookAdapter, HookEvent, Response

MAX_CONTEXT_CHARS = 9000


class CursorAdapter(HookAdapter):
    name = "cursor"
    display_name = "Cursor CLI"
    events = {"beforeSubmitPrompt": PROMPT, "preCompact": PRE_COMPACT}
    restore_on_prompt = True

    def parse(self, name: str, kind: str, payload: Dict[str, Any]) -> Optional[HookEvent]:
        event = super().parse(name, kind, payload)
        event.session_id = str(payload.get("conversation_id") or payload.get("session_id") or "")
        if kind == PRE_COMPACT:
            event.extra["compacted"] = True  # fires as compaction runs; can't veto
        return event

    def render_prompt(self, event: HookEvent, text: str) -> Response:
        return {"additional_context": text[:MAX_CONTEXT_CHARS]}
