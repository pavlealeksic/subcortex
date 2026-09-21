"""The four subcortex behaviors, shared by every TUI integration.

1. ``prompt_hint``   — simple prompt → short context hint for the model.
2. ``trim_output``   — large, disposable, successful tool output → head + marker + tail.
3. ``save_snapshot`` — before compaction, keep the last few messages on disk.
4. ``restore_snapshot`` — after compaction, hand them back as context.

Compaction restore has two delivery paths, chosen per TUI: directly from a
session-start-after-compaction hook, or — for TUIs whose post-compaction hooks
can't inject context — on the first prompt after compaction. The second path
needs to know compaction actually happened, so snapshots start "pending" and
``mark_compacted`` (a post-compaction event) makes them "ready".

Nothing here is TUI-specific: adapters translate payloads into these calls and
the results into each TUI's response format. Verdicts are injected
(``classify(prompt)`` / ``judge(output, context)``) so the same code runs in
the hook process (verdicts over HTTP from the daemon) and inside the daemon
(verdicts computed in-process, served to JS plugins). Every function returns
``None``/``False`` instead of raising.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Dict, List, Optional

from .config import data_dir
from .transcript import last_messages, message_from_entry

ClassifyFn = Callable[[str], Optional[Dict[str, Any]]]
JudgeFn = Callable[[str, str], Optional[Dict[str, Any]]]

# Factual, not imperative: several TUIs wrap this in a system reminder, and
# text framed as an out-of-band instruction can trip prompt-injection defenses.
HINT = (
    "[subcortex] A local classifier rated this request as simple "
    "(confidence {confidence:.2f}); the most direct, minimal change is likely sufficient."
)
TRUNCATION_NOTE = (
    "\n\n[subcortex: truncated {removed} chars of low-value output; "
    "re-run the command if you need the rest]\n\n"
)
RESTORE_HEADER = "[subcortex] Recent conversation from before context compaction (oldest first):"

SNAPSHOT_MAX_AGE_S = 7 * 24 * 3600
_TOOL_INPUT_CHARS = 500

# Errors are the most valuable output there is: never trim anything that looks like one.
_FAILURE_RE = re.compile(
    r"traceback \(most recent call last\)"
    r"|^\s*(error|fatal|exception|panic)\b"
    r"|npm err!"
    r"|\bFAILED\b"
    r"|\bexit (code|status) [1-9]",
    re.IGNORECASE | re.MULTILINE,
)


def _feature(cfg: Dict[str, Any], name: str) -> bool:
    return bool((cfg.get("features") or {}).get(name, True))


def _hooks(cfg: Dict[str, Any], key: str, default: Any) -> Any:
    return (cfg.get("hooks") or {}).get(key, default)


# -- 1. prompt hint ---------------------------------------------------------------------


def prompt_hint(prompt: Any, cfg: Dict[str, Any], classify: ClassifyFn) -> Optional[str]:
    """Hint text for a prompt the model rates simple, else None."""
    try:
        if not _feature(cfg, "prompt_hint") or not isinstance(prompt, str) or not prompt.strip():
            return None
        verdict = classify(prompt)
        if not isinstance(verdict, dict) or verdict.get("label") != "simple":
            return None
        confidence = float(verdict.get("confidence", 0.0))
        if confidence < float(cfg["thresholds"]["prompt_simple_confidence"]):
            return None
        return HINT.format(confidence=confidence)
    except Exception:
        return None


# -- 2. tool-output trimming ------------------------------------------------------------


def looks_like_failure(text: str) -> bool:
    return bool(_FAILURE_RE.search(text))


def truncate(text: str, head: int, tail: int) -> str:
    removed = len(text) - head - tail
    return text[:head] + TRUNCATION_NOTE.format(removed=removed) + text[-tail:]


def trim_output(
    output: Any,
    cfg: Dict[str, Any],
    judge: JudgeFn,
    tool: str = "",
    tool_input: Any = None,
    failed: bool = False,
) -> Optional[str]:
    """Truncated replacement for a disposable tool output, else None (keep it)."""
    try:
        if not _feature(cfg, "trim_output") or not isinstance(output, str) or failed:
            return None
        head = int(_hooks(cfg, "head_chars", 1000))
        tail = int(_hooks(cfg, "tail_chars", 500))
        min_chars = max(int(cfg["thresholds"]["min_output_chars"]), head + tail + 500)
        if len(output) < min_chars or looks_like_failure(output):
            return None
        context = tool or "tool"
        if tool_input is not None:
            context += ": " + json.dumps(tool_input, default=str)[:_TOOL_INPUT_CHARS]
        verdict = judge(output, context)
        if not isinstance(verdict, dict) or verdict.get("needed") is not False:
            return None
        p_needed = verdict.get("p_needed")
        if p_needed is not None and float(p_needed) >= float(cfg["thresholds"]["output_needed_threshold"]):
            return None
        return truncate(output, head, tail)
    except Exception:
        return None


# -- 3/4. compaction snapshot -----------------------------------------------------------


def _snapshot_path(session_id: Any):
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in session_id.strip())[:200]
    return data_dir() / "compact" / f"{safe}.json"


def _prune(directory) -> None:
    cutoff = time.time() - SNAPSHOT_MAX_AGE_S
    try:
        for old in directory.glob("*.json"):
            if old.stat().st_mtime < cutoff:
                old.unlink()
    except OSError:
        pass


def save_snapshot(session_id: Any, messages: List[Dict[str, str]], cfg: Dict[str, Any],
                  trigger: str = "") -> bool:
    """Persist ``messages`` (``[{role, text}]``) for ``session_id``. Never vetoes anything."""
    try:
        path = _snapshot_path(session_id)
        if path is None or not _feature(cfg, "compaction_snapshot"):
            return False
        limit = int(_hooks(cfg, "snapshot_messages", 5))
        max_chars = int(_hooks(cfg, "snapshot_chars", 500))
        kept = []
        for m in messages:
            # Plugins may send their TUI's raw message objects; normalize them.
            if not (isinstance(m, dict) and isinstance(m.get("text"), str) and "role" in m):
                m = message_from_entry(m)
            if m and str(m.get("text", "")).strip():
                kept.append({"role": str(m.get("role", "?")), "text": str(m["text"])[:max_chars]})
        kept = kept[-limit:]
        if not kept:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        _prune(path.parent)
        path.write_text(json.dumps(
            {"session_id": session_id, "trigger": trigger, "saved_at": time.time(),
             "ready": False, "messages": kept},
            ensure_ascii=False, indent=2))
        return True
    except Exception:
        return False


def mark_compacted(session_id: Any) -> bool:
    """Flag a pending snapshot as ready: compaction really happened."""
    try:
        path = _snapshot_path(session_id)
        if path is None or not path.is_file():
            return False
        data = json.loads(path.read_text())
        data["ready"] = True
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return True
    except Exception:
        return False


def snapshot_transcript(session_id: Any, transcript_path: Any, cfg: Dict[str, Any],
                        trigger: str = "") -> bool:
    """``save_snapshot`` fed from a transcript file on disk."""
    limit = int(_hooks(cfg, "snapshot_messages", 5))
    max_chars = int(_hooks(cfg, "snapshot_chars", 500))
    return save_snapshot(session_id, last_messages(transcript_path, limit, max_chars), cfg, trigger)


def restore_snapshot(session_id: Any, cfg: Dict[str, Any], consume: bool = True,
                     require_ready: bool = False) -> Optional[str]:
    """Context text for a saved snapshot (deleted once read), else None.

    ``require_ready`` restores only snapshots ``mark_compacted`` has flagged
    (the next-prompt delivery path must not fire when compaction was skipped).
    """
    try:
        path = _snapshot_path(session_id)
        if path is None or not _feature(cfg, "compaction_snapshot") or not path.is_file():
            return None
        data = json.loads(path.read_text())
        if require_ready and not data.get("ready"):
            return None
        if consume:
            path.unlink()
        lines = [
            f"{m.get('role', '?')}: {m.get('text', '')}"
            for m in (data.get("messages") or [])
            if isinstance(m, dict) and str(m.get("text", "")).strip()
        ]
        return RESTORE_HEADER + "\n" + "\n".join(lines) if lines else None
    except Exception:
        return None
