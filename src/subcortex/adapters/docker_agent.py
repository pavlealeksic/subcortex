"""Docker Agent (formerly cagent, >= 1.137).

snake_case payloads and responses — Claude's camelCase ``hookSpecificOutput``
is silently ignored here, so everything goes under ``hook_specific_output``.

- ``user_prompt_submit`` plus the steering / follow-up events (messages sent
  while the agent is busy) → ``additional_context``.
- ``tool_response_transform`` on ``shell`` → ``updated_tool_response`` replaces
  what the model sees *and* what is persisted (never an empty string: that
  would erase the output). Synthesized errors (``tool_error``) are left alone.
- ``before_compaction`` gets no transcript; recent messages are read
  read-only from the session database. ``after_compaction`` marks the
  snapshot ready and it is delivered with the next prompt. Never returns
  ``summary``/``decision``/``continue`` (they would replace the summary or block).
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..transcript import message_from_entry
from .base import POST_COMPACT, PRE_COMPACT, PROMPT, TOOL_OUTPUT, HookAdapter, HookEvent, Response

SNAPSHOT_ROWS = 40


def session_db() -> Path:
    return Path.home() / ".cagent" / "session.db"


def recent_messages(session_id: str, limit: int = SNAPSHOT_ROWS) -> List[Dict[str, str]]:
    """Latest user/assistant messages of a session, read-only; [] on any problem."""
    path = session_db()
    if not session_id or not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT message_json FROM session_items WHERE session_id = ? AND item_type = 'message' "
            "ORDER BY position DESC LIMIT ?", (session_id, limit)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    messages = []
    for (raw,) in reversed(rows):
        try:
            msg = message_from_entry(json.loads(raw))
        except (TypeError, ValueError):
            continue
        if msg:
            messages.append(msg)
    return messages


class DockerAgentAdapter(HookAdapter):
    name = "docker-agent"
    display_name = "Docker Agent"
    events = {
        "user_prompt_submit": PROMPT,
        "user_steering_messages_submit": PROMPT,
        "user_followup_submit": PROMPT,
        "tool_response_transform": TOOL_OUTPUT,
        "before_compaction": PRE_COMPACT,
        "after_compaction": POST_COMPACT,
    }
    restore_on_prompt = True

    def parse(self, name: str, kind: str, payload: Dict[str, Any]) -> Optional[HookEvent]:
        event = HookEvent(kind=kind, name=name, payload=payload,
                          session_id=str(payload.get("session_id") or ""),
                          trigger=str(payload.get("compaction_reason") or ""))
        if kind == PROMPT:
            steering = payload.get("steering_messages")
            if isinstance(steering, list):
                event.prompt = "\n".join(m for m in steering if isinstance(m, str))
            elif isinstance(payload.get("prompt"), str):
                event.prompt = payload["prompt"]
        elif kind == TOOL_OUTPUT:
            response = payload.get("tool_response")
            event.output = response if isinstance(response, str) else None
            event.failed = bool(payload.get("tool_error"))
            event.tool = str(payload.get("tool_name") or "shell")
            event.tool_input = payload.get("tool_input")
        elif kind == PRE_COMPACT:
            event.messages = recent_messages(event.session_id)
        return event

    def render_prompt(self, event: HookEvent, text: str) -> Response:
        return {"hook_specific_output": {"additional_context": text}}

    def render_tool_output(self, event: HookEvent, replacement: Optional[str]) -> Response:
        if not replacement:
            return None  # an empty string would erase the output
        return {"hook_specific_output": {"updated_tool_response": replacement}}

    def guard(self, kind: str, response: Response) -> Response:
        if isinstance(response, dict) and set(response) - {"hook_specific_output"}:
            return None
        return super().guard(kind, response)


def config_dir() -> Path:
    for var in ("DOCKER_AGENT_CONFIG_DIR", "CAGENT_CONFIG_DIR"):
        value = os.environ.get(var, "").strip()
        if value:
            return Path(value)
    return Path.home() / ".config" / "cagent"
