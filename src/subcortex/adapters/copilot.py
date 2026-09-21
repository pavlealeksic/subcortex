"""GitHub Copilot CLI (>= 1.0.67) with camelCase hook events.

- ``userPromptSubmitted`` → ``{"additionalContext": ...}`` (model-facing since 1.0.65).
- ``postToolUse`` (successful calls only) → ``{"modifiedResult": {...}}`` replaces
  what the model sees; ``resultType`` must stay ``"success"`` (anything else
  routes the call to the failure path).
- ``preCompact`` is a notification (can't block); there is no post-compaction
  ``sessionStart`` source, so the snapshot rides along with the next prompt.
- Payloads carry no event name: it always comes from argv.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .base import PRE_COMPACT, PROMPT, TOOL_OUTPUT, HookAdapter, HookEvent, Response


class CopilotAdapter(HookAdapter):
    name = "copilot"
    display_name = "GitHub Copilot CLI"
    events = {"userPromptSubmitted": PROMPT, "postToolUse": TOOL_OUTPUT, "preCompact": PRE_COMPACT}
    event_field = "hookEventName"
    restore_on_prompt = True

    def parse(self, name: str, kind: str, payload: Dict[str, Any]) -> Optional[HookEvent]:
        event = HookEvent(kind=kind, name=name, payload=payload,
                          session_id=str(payload.get("sessionId") or ""),
                          transcript_path=str(payload.get("transcriptPath") or ""),
                          trigger=str(payload.get("trigger") or ""))
        if kind == PROMPT:
            prompt = payload.get("prompt")
            event.prompt = prompt if isinstance(prompt, str) else ""
        elif kind == TOOL_OUTPUT:
            result = payload.get("toolResult")
            if not isinstance(result, dict) or result.get("resultType") != "success":
                return event
            args = payload.get("toolArgs")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            event.tool = str(payload.get("toolName") or "")
            event.tool_input = args
            text = result.get("textResultForLlm")
            event.output = text if isinstance(text, str) else None
        elif kind == PRE_COMPACT:
            event.extra["compacted"] = True  # notification of a compaction in progress
        return event

    def render_prompt(self, event: HookEvent, text: str) -> Response:
        return {"additionalContext": text}

    def render_tool_output(self, event: HookEvent, replacement: str) -> Response:
        result = event.payload.get("toolResult")
        if not isinstance(result, dict):
            return None
        return {"modifiedResult": {**result, "resultType": "success", "textResultForLlm": replacement}}
