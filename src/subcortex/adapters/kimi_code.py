"""Kimi Code CLI (MoonshotAI/kimi-code >= 0.33) hook adapter.

Kimi's hook contract differs from Claude Code's in ways that matter:

- ``UserPromptSubmit.prompt`` is a list of content parts, not a string.
- On exit 0 the hook's *message* is injected as context: ``{"message": ...}``
  or, failing that, the whole stdout verbatim — so Claude-style JSON (or even
  ``{}``) would be injected as literal text. We emit ``{"message": ...}`` only.
- ``PostToolUse`` is fire-and-forget: its output is discarded, so tool output
  can't be replaced (Kimi externalizes huge results itself). Not registered.
- ``SessionStart`` output is ignored and it never fires after compaction, so
  the snapshot is delivered with the first prompt after ``PostCompact``.
- Payloads carry no transcript path: the session's ``wire.jsonl`` is found via
  ``$KIMI_CODE_HOME/session_index.jsonl``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..transcript import read_tail
from .base import POST_COMPACT, PRE_COMPACT, PROMPT, HookAdapter, HookEvent, Response

SNAPSHOT_LIMIT = 5
SNAPSHOT_CHARS = 500


def kimi_home() -> Path:
    override = os.environ.get("KIMI_CODE_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".kimi-code"


def session_wire(session_id: str) -> Optional[Path]:
    """``<sessionDir>/agents/main/wire.jsonl`` for a Kimi session id."""
    if not session_id or "/" in session_id:
        return None
    home = kimi_home()
    session_dir = None
    try:
        with open(home / "session_index.jsonl", encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("sessionId") == session_id:
                    session_dir = None if record.get("deleted") else record.get("sessionDir")
    except OSError:
        pass
    if isinstance(session_dir, str):
        wire = Path(session_dir) / "agents" / "main" / "wire.jsonl"
        if wire.is_file():
            return wire
    matches = sorted((home / "sessions").glob(f"*/{session_id}/agents/main/wire.jsonl"))
    return matches[-1] if matches else None


def wire_messages(path: Path, limit: int = SNAPSHOT_LIMIT,
                  max_chars: int = SNAPSHOT_CHARS) -> List[Dict[str, str]]:
    """User prompts and assistant text from a Kimi ``wire.jsonl`` tail.

    User turns are ``context.append_message`` records whose origin kind is
    ``user`` (injections, hook results and task notices are skipped); assistant
    text streams as ``content.part`` loop events and is joined per step.
    """
    messages: List[Dict[str, str]] = []
    step, buffer = None, []

    def flush() -> None:
        text = "".join(buffer).strip()
        if text:
            messages.append({"role": "assistant", "text": text[:max_chars]})
        buffer.clear()

    for line in read_tail(path).splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        kind = record.get("type")
        if kind == "context.clear":
            flush()
            messages.clear()
        elif kind == "context.append_message":
            message = record.get("message") or {}
            origin = message.get("origin")
            origin_kind = origin.get("kind") if isinstance(origin, dict) else origin
            if message.get("role") != "user" or origin_kind not in (None, "user"):
                continue
            flush()
            text = "\n".join(
                part.get("text", "") for part in message.get("content") or []
                if isinstance(part, dict) and part.get("type") == "text").strip()
            if text:
                messages.append({"role": "user", "text": text[:max_chars]})
        elif kind == "context.append_loop_event":
            event = record.get("event") or {}
            part = event.get("part") or {}
            if event.get("type") != "content.part" or part.get("type") != "text":
                continue
            if event.get("stepUuid") != step:
                flush()
                step = event.get("stepUuid")
            buffer.append(str(part.get("text", "")))
    flush()
    return messages[-limit:]


class KimiCodeAdapter(HookAdapter):
    name = "kimi-code"
    display_name = "Kimi Code CLI"
    events = {
        "UserPromptSubmit": PROMPT,
        "PreCompact": PRE_COMPACT,
        "PostCompact": POST_COMPACT,
    }
    restore_on_prompt = True

    def parse(self, name: str, kind: str, payload: Dict[str, Any]) -> Optional[HookEvent]:
        event = super().parse(name, kind, payload)
        if kind == PROMPT:
            parts = payload.get("prompt")
            if isinstance(parts, list):
                event.prompt = "\n".join(
                    p.get("text", "") for p in parts
                    if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))
        elif kind == PRE_COMPACT:
            wire = session_wire(event.session_id)
            event.messages = wire_messages(wire) if wire else []
        return event

    def render_prompt(self, event: HookEvent, text: str) -> Response:
        return {"message": text}
