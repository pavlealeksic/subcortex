"""Adapters for TUIs that copied Claude Code's hook contract.

Same event names and stdin fields (``session_id``, ``transcript_path``,
``prompt``, ``tool_name``/``tool_input``/``tool_response``, ``source``,
``trigger``) and the same response envelope
(``{"hookSpecificOutput": {"hookEventName": ..., ...}}``). What differs is
which events exist and whether ``updatedToolOutput`` replaces a tool result —
so each TUI declares that explicitly. Blocking semantics differ too, which is
why nothing here ever emits ``decision``/``continue``/``permissionDecision``.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from .base import PRE_COMPACT, PROMPT, SESSION_START, TOOL_OUTPUT, HookAdapter, HookEvent, Response

ALL_EVENTS = {
    "UserPromptSubmit": PROMPT,
    "PostToolUse": TOOL_OUTPUT,
    "PreCompact": PRE_COMPACT,
    "SessionStart": SESSION_START,
}


class ClaudeStyleAdapter(HookAdapter):
    events = ALL_EVENTS
    # Does hookSpecificOutput.updatedToolOutput (a string) replace the result the model sees?
    replaces_output = False

    def _specific(self, event: HookEvent, **fields: Any) -> Dict[str, Any]:
        # hookEventName is mandatory: some clones reject the whole object without it.
        return {"hookSpecificOutput": {"hookEventName": event.name, **fields}}

    def render_prompt(self, event: HookEvent, text: str) -> Response:
        return self._specific(event, additionalContext=text)

    def render_tool_output(self, event: HookEvent, replacement: str) -> Response:
        if not self.replaces_output:
            return None
        return self._specific(event, updatedToolOutput=replacement)

    def render_session_start(self, event: HookEvent, context: str) -> Response:
        return self._specific(event, additionalContext=context)


class ClaudeCodeAdapter(ClaudeStyleAdapter):
    name = "claude-code"
    display_name = "Claude Code"
    # Since 2.1.121 updatedToolOutput replaces the output of any tool — but for
    # built-in tools only in the tool's own shape (a string is silently ignored).
    replaces_output = True

    def render_tool_output(self, event: HookEvent, replacement: str) -> Response:
        response = event.payload.get("tool_response")
        if not isinstance(response, dict) or not isinstance(response.get("stdout"), str):
            return None
        # Already spilled to a file / background / image / interrupted: leave it be.
        if any(response.get(k) for k in ("persistedOutputPath", "backgroundTaskId", "isImage", "interrupted")):
            return None
        return self._specific(event, updatedToolOutput={**response, "stdout": replacement})

    def parse(self, name: str, kind: str, payload: Dict[str, Any]) -> Optional[HookEvent]:
        # Grok Build, Devin CLI and Cortex Code also execute hooks from
        # ~/.claude/settings.json, with their own payload shapes. Only answer
        # payloads that look like Claude Code's own.
        if not is_claude_code_payload(payload):
            return None
        return super().parse(name, kind, payload)


# Set in hook processes by other TUIs that also execute ~/.claude/settings.json hooks.
FOREIGN_HOST_ENV = ("CURSOR_VERSION", "DROID_PROJECT_DIR", "FACTORY_PROJECT_DIR", "GROK_HOOK_EVENT")


def is_claude_code_payload(payload: Dict[str, Any]) -> bool:
    if any(os.environ.get(var) for var in FOREIGN_HOST_ENV) or "cursor_version" in payload:
        return False  # Cursor CLI / Factory Droid running Claude-format hooks
    if any(k in payload for k in ("hookEventName", "sessionId", "workspaceRoot", "toolName")):
        return False  # camelCase: Grok Build
    # NB: not `prompt_id` — Claude Code itself sends it on every event since 2.1.196.
    if not isinstance(payload.get("transcript_path"), str) or not payload["transcript_path"]:
        return False  # Devin CLI (absent), Continue cn (empty)
    if not isinstance(payload.get("session_id"), str) or not payload["session_id"]:
        return False
    tool = payload.get("tool_name")
    return not (isinstance(tool, str) and tool == "bash")  # Cortex Code's lowercase tools


class QoderAdapter(ClaudeStyleAdapter):
    name = "qoder"
    display_name = "Qoder CLI"
    replaces_output = True  # "Replaces the tool response (works for any tool)"


class CodeBuddyAdapter(ClaudeStyleAdapter):
    name = "codebuddy"
    display_name = "CodeBuddy Code"
    replaces_output = True  # "entirely replaces the original tool output"


class DroidAdapter(ClaudeStyleAdapter):
    name = "droid"
    display_name = "Factory Droid"
    # PostToolUse can only block or append in Droid: not registered.
    events = {k: v for k, v in ALL_EVENTS.items() if v != TOOL_OUTPUT}

    def parse(self, name: str, kind: str, payload: Dict[str, Any]) -> Optional[HookEvent]:
        event = super().parse(name, kind, payload)
        previous = payload.get("previous_session_id")
        if kind == SESSION_START and isinstance(previous, str) and previous:
            event.session_id = previous  # compaction starts a new session id
        return event


class JunieAdapter(ClaudeStyleAdapter):
    name = "junie"
    display_name = "Junie CLI"
    # No PostToolUse and no PreCompact (so nothing to restore after compaction).
    events = {"UserPromptSubmit": PROMPT}


class DevinAdapter(ClaudeStyleAdapter):
    name = "devin"
    display_name = "Devin CLI"
    # PostToolUse only appends; there is no pre-compaction event.
    events = {"UserPromptSubmit": PROMPT}
