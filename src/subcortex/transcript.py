"""Shape-tolerant transcript reading for compaction snapshots.

Every TUI records conversations differently (Claude Code JSONL entries with a
nested ``message``, Codex rollout lines wrapping a ``payload``, Gemini-style
``parts``, whole-file JSON with a ``messages`` array, ...). ``last_messages``
extracts the final few user/assistant texts from any of them and never raises.
Only the tail of large files is read.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

TAIL_BYTES = 1_000_000
WHOLE_FILE_MAX_BYTES = 20_000_000

_USER_ROLES = {"user", "human"}
_ASSISTANT_ROLES = {"assistant", "model", "gemini", "ai", "agent"}
_TEXT_BLOCK_TYPES = {"text", "input_text", "output_text"}
# Harness-injected pseudo-messages such as <environment_context>…
_INJECTED_RE = re.compile(r"<[a-z][\w-]*(?:\s[^>]*)?>", re.IGNORECASE)  # matched on stripped text
_INJECTED_PREFIXES = ("# AGENTS.md instructions",)  # Codex
# ...except wrappers around the user's own words (Cursor: <user_query>).
_USER_WRAPPERS = ("user_query", "user_message")


def _unwrap_user(text: str) -> Optional[str]:
    """The words inside <user_query>…</user_query>, else None. Plain string
    operations: a regex here backtracked cubically on long whitespace runs."""
    for tag in _USER_WRAPPERS:
        opening, closing = f"<{tag}>", f"</{tag}>"
        if text.startswith(opening) and text.endswith(closing) and len(text) >= len(opening) + len(closing):
            return text[len(opening):len(text) - len(closing)].strip()
    return None


def _role(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in _USER_ROLES:
        return "user"
    if lowered in _ASSISTANT_ROLES:
        return "assistant"
    return None


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                if block.get("type") in (None, *_TEXT_BLOCK_TYPES):
                    parts.append(block["text"])
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return ""


def _display_text(entry: Dict[str, Any]) -> str:
    """What the user typed, without the hook context the TUI appended to the
    model-bound message (Gemini CLI ``displayContent``, Qwen Code
    ``systemPayload.displayText``)."""
    if "displayContent" in entry:
        return _text(entry["displayContent"])
    payload = entry.get("systemPayload")
    if isinstance(payload, dict) and isinstance(payload.get("displayText"), str):
        return payload["displayText"]
    return ""


def _without_our_context(text: str) -> str:
    """Drop what subcortex itself injected (hint lines, restored-context blocks),
    so a snapshot never carries — or compounds — our own output."""
    if "[subcortex]" not in text:
        return text
    kept, skipping = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("[subcortex] Recent conversation from before context compaction"):
            skipping = True  # a restore block runs to the next blank line
            continue
        if skipping:
            if line.strip():
                continue
            skipping = False
        if line.lstrip().startswith("[subcortex]"):
            continue
        kept.append(line)
    return "\n".join(kept)


def message_from_entry(entry: Any) -> Optional[Dict[str, str]]:
    """``{"role", "text"}`` for one transcript record, or None if it isn't a
    user/assistant message with text."""
    if not isinstance(entry, dict):
        return None
    if entry.get("isMeta") or entry.get("isCompactSummary") or entry.get("isSidechain") \
            or entry.get("synthetic_reason"):
        return None  # reminders, compaction summaries, subagent turns, injected items
    payload = entry.get("payload")
    if isinstance(payload, dict):
        return message_from_entry(payload)
    kind = entry.get("type")
    if isinstance(kind, str) and kind.endswith(".message"):  # Copilot session events
        data = entry.get("data") if isinstance(entry.get("data"), dict) else entry
        role = _role(kind[: -len(".message")])
        text = _text(data.get("content", data.get("text", data.get("message")))).strip()
        return {"role": role, "text": text} if role and text else None
    message = entry.get("message")
    if isinstance(message, dict):
        role = _role(message.get("role")) or _role(entry.get("type")) or _role(entry.get("role"))
        text = _text(message.get("content", message.get("parts")))
    else:
        role = _role(entry.get("role")) or _role(entry.get("type")) or _role(entry.get("author"))
        text = _text(entry.get("content", entry.get("parts", entry.get("text"))))
        if not text and isinstance(message, str):
            text = message
    text = _without_our_context(_display_text(entry) or text).strip()
    wrapped = _unwrap_user(text)
    if wrapped is not None:
        text = wrapped
    elif _INJECTED_RE.match(text) or text.startswith(_INJECTED_PREFIXES):
        return None
    if role is None or not text:
        return None
    return {"role": role, "text": text}


def read_tail(path: Path) -> str:
    size = path.stat().st_size
    with open(path, "rb") as fh:
        if size > TAIL_BYTES:
            fh.seek(size - TAIL_BYTES)
            fh.readline()  # drop the partial first line
        return fh.read().decode("utf-8", errors="replace")


def _unwrap(whole: Any) -> List[Any]:
    if isinstance(whole, list):
        return whole
    if isinstance(whole, dict):
        for key in ("messages", "history", "items", "conversation"):
            if isinstance(whole.get(key), list):
                return whole[key]
        return [whole]
    return []


def _entries(path: Path) -> Iterable[Any]:
    """JSONL records from the file's tail, or the message list of a whole-JSON file."""
    raw = read_tail(path)
    entries, bad = [], 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            bad += 1
    if entries and bad <= len(entries):  # JSONL (a stray partial line is fine)
        return _unwrap(entries[0]) if len(entries) == 1 else entries
    if path.stat().st_size > WHOLE_FILE_MAX_BYTES:  # pretty-printed and huge: give up
        return []
    try:
        return _unwrap(json.loads(path.read_text(encoding="utf-8", errors="replace")))
    except ValueError:
        return []


def messages_from_entries(entries: Iterable[Any], limit: int, max_chars: int) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for entry in entries:
        msg = message_from_entry(entry)
        if msg is None:
            continue
        msg["text"] = msg["text"][:max_chars]
        if out and out[-1] == msg:  # rollouts often log the same message twice
            continue
        out.append(msg)
    return out[-limit:] if limit > 0 else []


def last_messages(path: Any, limit: int = 5, max_chars: int = 500) -> List[Dict[str, str]]:
    """Last ``limit`` user/assistant messages from a transcript file; [] on any problem."""
    try:
        if not isinstance(path, str) or not path:
            return []
        p = Path(path).expanduser()
        if not p.is_file():
            return []
        return messages_from_entries(_entries(p), limit, max_chars)
    except Exception:
        return []
