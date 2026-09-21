"""OpenHands CLI (>= 1.12, SDK hook engine) adapter.

Only ``UserPromptSubmit`` can inject (``{"additionalContext": ...}``); the
terminal tool truncates big outputs itself (30k chars) and PostToolUse /
SessionStart output is ignored, so those aren't registered.

There is no compaction hook. OpenHands' event log is append-only, though: a
``Condensation`` event hides older events from the model but leaves them on
disk. So on each prompt we check for a condensation we haven't handled yet and,
if there is one, hand back the last messages it hid. The first prompt of a
session only records a baseline (a resumed, already-condensed conversation is
not re-injected).

Never emitted, per the SDK executor: exit 2, ``decision: deny`` (with any exit
code) or ANY ``continue`` key (every falsy value blocks).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import state
from ..transcript import message_from_entry
from .base import PROMPT, HookAdapter, HookEvent, Response

MAX_EVENT_FILES = 3000


def conversations_dir() -> Path:
    override = os.environ.get("OPENHANDS_CONVERSATIONS_DIR", "").strip()
    if override:
        return Path(override)
    persistence = os.environ.get("OPENHANDS_PERSISTENCE_DIR", "").strip()
    base = Path(persistence) if persistence else Path.home() / ".openhands"
    return base / "conversations"


def _read(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _event_message(event: Dict[str, Any]) -> Optional[Dict[str, str]]:
    if event.get("kind") != "MessageEvent":
        return None
    role = {"user": "user", "agent": "assistant"}.get(str(event.get("source")))
    if role is None:
        return None
    msg = message_from_entry({"role": role, **(event.get("llm_message") or {})})
    return {"role": role, "text": msg["text"]} if msg else None


def _record(session_id: str, record: Dict[str, Any]) -> None:
    marker = state.path("openhands", "openhands", session_id)
    if marker is not None:
        with state.locked(state.key("openhands", session_id)):
            state.write_json(marker, record)


def condensation_context(session_id: str, cfg: Dict[str, Any],
                         commits: Optional[List[Any]] = None) -> Optional[str]:
    """Messages hidden by a condensation not yet reported for this session.

    Only event files newer than the last scan are read (a long conversation has
    thousands). The "reported" marker is written once the context was delivered
    (via ``commits``), so a hook that dies mid-way reports it next time.
    """
    from .. import policy

    if not session_id or "/" in session_id:
        return None
    events_dir = conversations_dir() / session_id.replace("-", "") / "events"
    try:
        files = sorted(events_dir.glob("event-*.json"))[-MAX_EVENT_FILES:]
        marker = state.path("openhands", "openhands", session_id)
        recorded = state.read_json(marker) if marker is not None else None
    except Exception:
        return None
    if not files:
        return None
    first_visit = recorded is None
    seen = (recorded or {}).get("condensation")
    scanned = str((recorded or {}).get("scanned") or "")
    newest, index = seen, -1
    for i in range(len(files) - 1, -1, -1):
        if not first_visit and files[i].name <= scanned:
            break  # older files were checked on an earlier prompt
        event = _read(files[i])
        if event and event.get("kind") == "Condensation":
            newest, index = event.get("id") or files[i].name, i
            break
    record = {"condensation": newest, "scanned": files[-1].name}
    deliver = not first_visit and index != -1 and newest != seen
    context = None
    if deliver:
        limit = int((cfg.get("hooks") or {}).get("snapshot_messages", 5))
        max_chars = int((cfg.get("hooks") or {}).get("snapshot_chars", 500))
        messages: List[Dict[str, str]] = []
        for path in reversed(files[:index]):  # newest hidden message first, stop when enough
            event = _read(path)
            msg = _event_message(event) if event else None
            if msg:
                messages.append({"role": msg["role"], "text": msg["text"][:max_chars]})
                if len(messages) >= limit:
                    break
        if messages:
            lines = [f"{m['role']}: {m['text']}" for m in reversed(messages)]
            context = policy.RESTORE_HEADER + "\n" + "\n".join(lines)
    try:
        state.prune("openhands", policy.SNAPSHOT_MAX_AGE_S)
        if context and commits is not None:
            commits.append(lambda: _record(session_id, record))
        elif record != recorded:
            _record(session_id, record)
    except Exception:
        return None
    return context


class OpenHandsAdapter(HookAdapter):
    name = "openhands"
    display_name = "OpenHands CLI"
    events = {"UserPromptSubmit": PROMPT}
    event_field = "event_type"

    def parse(self, name: str, kind: str, payload: Dict[str, Any]) -> Optional[HookEvent]:
        event = super().parse(name, kind, payload)
        message = payload.get("message")
        event.prompt = message if isinstance(message, str) else ""
        return event

    def prompt_context(self, event: HookEvent, cfg: Dict[str, Any]) -> Optional[str]:
        if not (cfg.get("features") or {}).get("compaction_snapshot", True):
            return None
        try:
            return condensation_context(event.session_id, cfg, event.commits)
        except Exception:
            return None

    def render_prompt(self, event: HookEvent, text: str) -> Response:
        return {"additionalContext": text}

    def guard(self, kind: str, response: Response) -> Response:
        # Any `continue` key with a falsy value blocks in OpenHands; allow none at all.
        if isinstance(response, dict) and "continue" in response:
            return None
        return super().guard(kind, response)
