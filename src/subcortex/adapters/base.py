"""Base class for command-hook adapters.

An adapter is pure translation: it maps a TUI's hook event names onto the four
canonical kinds, parses the TUI's stdin payload into a ``HookEvent``, and
renders policy results into the TUI's stdout response. All decisions live in
``subcortex.policy``; process safety lives in ``subcortex.hook``.

The default ``parse`` understands the Claude-Code-style payload that many TUIs
copied (``session_id``, ``prompt``, ``tool_name``, ``tool_input``,
``tool_response``, ``transcript_path``, ``source``, ``trigger``); adapters
override only what differs.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple, Union

PROMPT = "prompt"
TOOL_OUTPUT = "tool_output"
PRE_COMPACT = "pre_compact"
POST_COMPACT = "post_compact"
SESSION_START = "session_start"
KINDS = (PROMPT, TOOL_OUTPUT, PRE_COMPACT, POST_COMPACT, SESSION_START)

Response = Union[Dict[str, Any], str, None]

# Values that would block, deny, or stop an agent in at least one TUI. A
# response containing any of them is dropped by ``guard`` unless the adapter
# explicitly allows that exact (key, value) for that kind.
BLOCKING_VALUES: Tuple[Tuple[str, Any], ...] = (
    ("continue", False),
    ("decision", "block"),
    ("decision", "deny"),
    ("decision", "ask"),
    ("permission", "deny"),
    ("permission", "ask"),
    ("permissionDecision", "deny"),
    ("permissionDecision", "ask"),
    ("behavior", "deny"),
    ("block", True),
    ("abort", True),
    ("cancel", True),
)


class HookEvent:
    """One hook event, normalized. A plain class on purpose: importing
    ``dataclasses`` costs ~3 ms in every (cold-started) hook process."""

    def __init__(self, kind: str, name: str, payload: Dict[str, Any], session_id: str = "",
                 prompt: str = "", tool: str = "", tool_input: Any = None,
                 output: Optional[str] = None, failed: bool = False, transcript_path: str = "",
                 source: str = "", trigger: str = "",
                 messages: Optional[List[Dict[str, str]]] = None,
                 extra: Optional[Dict[str, Any]] = None) -> None:
        self.kind = kind
        self.name = name
        self.payload = payload
        self.session_id = session_id
        self.prompt = prompt
        self.tool = tool
        self.tool_input = tool_input
        self.output = output
        self.failed = failed
        self.transcript_path = transcript_path
        self.source = source
        self.trigger = trigger
        self.messages = messages
        self.extra = {} if extra is None else extra
        # Set by the runner: commit callables to run once the response is delivered.
        self.commits: Optional[List[Any]] = None

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, HookEvent) and vars(self) == vars(other)

    def __repr__(self) -> str:
        return f"HookEvent({vars(self)!r})"


def text_of(value: Any) -> Optional[str]:
    """Flatten common tool-result shapes (str, content blocks, dicts) to text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [b.get("text") for b in value if isinstance(b, dict) and isinstance(b.get("text"), str)]
        parts += [b for b in value if isinstance(b, str)]
        return "\n".join(parts) if parts else None
    if isinstance(value, dict):
        for key in ("stdout", "output", "llmContent", "content", "text", "result", "returnDisplay"):
            if key in value:
                text = text_of(value[key])
                if text:
                    return text
    return None


def failed_of(value: Any) -> bool:
    """True when a tool-result dict signals failure (non-zero exit, error flag, interrupt)."""
    if not isinstance(value, dict):
        return False
    if value.get("interrupted") or value.get("is_error") or value.get("isError"):
        return True
    if value.get("error") not in (None, "", False):
        return True
    for key in ("exit_code", "exitCode", "returncode", "exit_status"):
        code = value.get(key)
        if isinstance(code, int) and code != 0:
            return True
    stderr = value.get("stderr")
    return isinstance(stderr, str) and "traceback" in stderr.lower()


class HookAdapter:
    name: str = ""
    display_name: str = ""
    aliases: Tuple[str, ...] = ()
    # TUI event name -> canonical kind. Lookup is case-insensitive.
    events: Dict[str, str] = {}
    # Payload key carrying the event name, used when argv omits it.
    event_field: str = "hook_event_name"
    # SessionStart ``source`` values meaning "right after compaction".
    compact_sources: Tuple[str, ...] = ("compact",)
    # (kind, key, value) triples this adapter may emit despite BLOCKING_VALUES.
    allowed_blocking: Tuple[Tuple[str, str, Any], ...] = ()
    # Deliver the compaction snapshot with the first prompt after compaction
    # (for TUIs whose session-start/post-compact hooks can't inject context).
    restore_on_prompt: bool = False
    # False when the TUI discards prompt-hook output: the hint isn't computed.
    delivers_hints: bool = True

    # -- event resolution -----------------------------------------------------------

    def resolve(self, name: Any) -> Optional[Tuple[str, str]]:
        """(TUI event name, kind) for ``name``, or None if we don't handle it."""
        if not isinstance(name, str) or not name.strip():
            return None
        wanted = name.strip().lower().replace("_", "").replace("-", "")
        for event, kind in self.events.items():
            if event.lower().replace("_", "").replace("-", "") == wanted:
                return event, kind
        return None

    # -- parsing ----------------------------------------------------------------------

    def parse(self, name: str, kind: str, payload: Dict[str, Any]) -> Optional[HookEvent]:
        event = HookEvent(
            kind=kind,
            name=name,
            payload=payload,
            session_id=str(payload.get("session_id") or payload.get("sessionId") or ""),
            transcript_path=str(payload.get("transcript_path") or payload.get("transcriptPath") or ""),
            source=str(payload.get("source") or ""),
            trigger=str(payload.get("trigger") or ""),
        )
        if kind == PROMPT:
            prompt = payload.get("prompt")
            event.prompt = prompt if isinstance(prompt, str) else ""
        elif kind == TOOL_OUTPUT:
            response = payload.get("tool_response", payload.get("tool_output"))
            event.tool = str(payload.get("tool_name") or "")
            event.tool_input = payload.get("tool_input")
            event.output = text_of(response)
            event.failed = failed_of(response)
        return event

    def is_after_compaction(self, event: HookEvent) -> bool:
        return event.source.lower() in self.compact_sources

    def prompt_context(self, event: HookEvent, cfg: Dict[str, Any]) -> Optional[str]:
        """Context to deliver alongside a prompt (before any hint). By default:
        the compaction snapshot, for adapters that restore on the next prompt."""
        if not self.restore_on_prompt:
            return None
        from .. import policy

        return policy.restore_snapshot(event.session_id, cfg, require_ready=True, tui=self.name,
                                       commits=event.commits)

    def output_context(self, event: HookEvent, cfg: Dict[str, Any]) -> Optional[str]:
        """Context to deliver alongside a tool result (after the trim decision).
        None by default; for TUIs whose only model-visible channel is a tool result."""
        return None

    # -- rendering (None = no output = pass-through) ---------------------------------

    def render_prompt(self, event: HookEvent, text: str) -> Response:
        """``text`` is the hint, possibly preceded by restored compaction context."""
        return None

    def render_tool_output(self, event: HookEvent, replacement: str) -> Response:
        return None

    def render_pre_compact(self, event: HookEvent) -> Response:
        return None

    def render_post_compact(self, event: HookEvent) -> Response:
        return None

    def render_session_start(self, event: HookEvent, context: str) -> Response:
        return None

    # -- safety -------------------------------------------------------------------------

    def guard(self, kind: str, response: Response) -> Response:
        """Drop any response that could block, deny, or stop the agent."""
        if response is None or isinstance(response, str):
            return response
        if not isinstance(response, dict):
            return None
        for key, value in _walk(response):
            for bad_key, bad_value in BLOCKING_VALUES:
                if key == bad_key and value == bad_value \
                        and (kind, key, value) not in self.allowed_blocking:
                    return None
        try:
            json.dumps(response)
        except (TypeError, ValueError):
            return None
        return response


def _walk(obj: Any):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)
