"""Gemini CLI and its fork Qwen Code.

Both use Claude-like ``hookSpecificOutput`` envelopes but their own event
names and semantics:

Gemini CLI (>= 0.27): ``BeforeAgent`` injects context; ``AfterTool`` can only
append or *block* (no replacement — Gemini truncates at 40k chars itself), so
it isn't registered; ``PreCompress`` fires before every model request (the
installer matches only ``manual`` triggers, i.e. a real ``/compress``) and there
is no post-compaction SessionStart, so the snapshot is delivered with the next
prompt. Any exit status >= 2 with text output BLOCKS in Gemini — the runner
and the installer's shell guard make that impossible.

Qwen Code (>= 0.16): ``UserPromptSubmit`` also fires on tool-result
continuations, so only prompts carrying ``submitted_prompt`` are classified;
``PostToolUse`` is append-only (not registered); ``PreCompact`` fires only when
compaction will run and ``SessionStart(compact)`` puts context into the system
instruction.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from .base import PRE_COMPACT, PROMPT, SESSION_START
from .claude_family import ClaudeStyleAdapter
from .base import HookEvent

_HOOK_CONTEXT_RE = re.compile(r"<hook_context>.*?</hook_context>\s*", re.S)


class GeminiCliAdapter(ClaudeStyleAdapter):
    name = "gemini-cli"
    display_name = "Gemini CLI"
    events = {"BeforeAgent": PROMPT, "PreCompress": PRE_COMPACT}
    restore_on_prompt = True

    def parse(self, name: str, kind: str, payload: Dict[str, Any]) -> Optional[HookEvent]:
        event = super().parse(name, kind, payload)
        if kind == PROMPT:
            # In -p mode SessionStart context is prefixed to the prompt.
            event.prompt = _HOOK_CONTEXT_RE.sub("", event.prompt)
        elif kind == PRE_COMPACT and event.trigger == "manual":
            event.extra["compacted"] = True  # /compress always compresses
        return event


class QwenCodeAdapter(ClaudeStyleAdapter):
    name = "qwen-code"
    display_name = "Qwen Code"
    events = {"UserPromptSubmit": PROMPT, "PreCompact": PRE_COMPACT, "SessionStart": SESSION_START}

    def parse(self, name: str, kind: str, payload: Dict[str, Any]) -> Optional[HookEvent]:
        event = super().parse(name, kind, payload)
        if kind == PROMPT:
            submitted = payload.get("submitted_prompt")
            # Absent on tool-result continuations: nothing to classify there.
            event.prompt = submitted if isinstance(submitted, str) else ""
        return event
