"""Letta Code (>= 0.32) and Mistral Vibe (>= 2.25.5).

Letta: ``UserPromptSubmit`` exit-0 stdout is injected verbatim (JSON is not
parsed — ``{}`` would be injected as text), so the hint is plain text. No
hook can replace tool output, and there's no transcript to snapshot.

Vibe: no prompt or compaction events, but ``post_tool`` can replace what the
model sees: ``{"decision": "deny", "reason": R}`` swaps ``tool_output_text``
for ``R`` without failing or un-running the call (verified in both of Vibe's
harnesses). That is the one place subcortex emits ``decision: deny``; it is
allowed for this adapter's tool-output event only. Failed calls
(``tool_status`` other than ``success``) are left alone.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base import PROMPT, TOOL_OUTPUT, HookAdapter, HookEvent, Response


class LettaAdapter(HookAdapter):
    name = "letta"
    display_name = "Letta Code"
    events = {"UserPromptSubmit": PROMPT}
    event_field = "event_type"

    def parse(self, name: str, kind: str, payload: Dict[str, Any]) -> Optional[HookEvent]:
        if payload.get("is_command"):
            return None  # slash commands
        prompt = payload.get("prompt")
        return HookEvent(kind=kind, name=name, payload=payload,
                         session_id=str(payload.get("conversation_id") or payload.get("agent_id") or ""),
                         prompt=prompt if isinstance(prompt, str) else "")

    def render_prompt(self, event: HookEvent, text: str) -> Response:
        return text  # plain stdout; Letta wraps it in a system reminder


class VibeAdapter(HookAdapter):
    name = "vibe"
    display_name = "Mistral Vibe"
    events = {"post_tool": TOOL_OUTPUT}
    allowed_blocking = ((TOOL_OUTPUT, "decision", "deny"),)

    def parse(self, name: str, kind: str, payload: Dict[str, Any]) -> Optional[HookEvent]:
        event = HookEvent(kind=kind, name=name, payload=payload,
                          session_id=str(payload.get("session_id") or ""))
        text = payload.get("tool_output_text")
        if payload.get("tool_status") != "success" or not isinstance(text, str):
            return event
        event.output = text
        event.tool = str(payload.get("tool_name") or "bash")
        event.tool_input = payload.get("tool_input")
        return event

    def render_tool_output(self, event: HookEvent, replacement: Optional[str]) -> Response:
        if not replacement:
            return None  # an empty reason would blank the output
        removed = len(event.output or "") - len(replacement)
        return {"decision": "deny", "reason": replacement,
                "system_message": f"subcortex: trimmed {max(removed, 0)} chars of low-value output"}
