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
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional

from . import state
from .transcript import last_messages, message_from_entry

ClassifyFn = Callable[[str], Optional[Dict[str, Any]]]
JudgeFn = Callable[[str, str, str], Optional[Dict[str, Any]]]  # (output, tool call, task)

# Factual, not imperative: several TUIs wrap this in a system reminder, and
# text framed as an out-of-band instruction can trip prompt-injection defenses.
HINT = (
    "[subcortex] A decision model rated this request as simple "
    "(p={p:.2f}); the most direct, minimal change is likely sufficient."
)
TRIM_NOTE = (
    "\n[subcortex: truncated {lines} lines ({chars} chars) of routine output{kept}; "
    "re-run the command if you need the rest]\n"
)
RESTORE_HEADER = "[subcortex] Recent conversation from before context compaction (oldest first):"

SNAPSHOT_MAX_AGE_S = 7 * 24 * 3600
# A claimed snapshot whose delivery never completed (hook killed, plugin gone)
# becomes restorable again after this long.
CLAIM_RECOVER_S = 15.0
_TOOL_INPUT_CHARS = 500
_RESCUE_MAX_LINES = 40
_RESCUE_MAX_CHARS = 4000
_MIN_SAVING = 0.2  # a trim must remove at least this share of the output

# Errors are the most valuable output there is: never trim anything that looks
# like one. Every branch is linear-time: `[ \t]*`, never `\s*` under MULTILINE
# (`\s` crosses newlines; that backtracked quadratically over blank lines and
# froze hooks for seconds, past any watchdog).
_FAILURE_RE = re.compile(
    r"traceback \(most recent call last\)"
    r"|^[ \t]*(?:error|fatal|exception|panic)\b"
    r"|:[0-9]+(?::[0-9]+)?: (?:fatal )?error\b"        # gcc / clang / rustc: file:line: error
    r"|\berror TS[0-9]+"                              # tsc
    r"|^--- FAIL\b|^FAIL\b"                           # go test
    r"|^make(?:\[[0-9]+\])?: \*\*\*"                  # make: *** [target] Error 1
    r"|npm err!"
    r"|(?-i:\bFAILED\b)"                              # pytest, ctest (upper case only)
    r"|\b[1-9][0-9]* (?:failed|failing|failures?|errors?)\b"
    r"|segmentation fault|core dumped|\bpanicked at\b"
    r"|\bexit (?:code|status) [1-9]",
    re.IGNORECASE | re.MULTILINE,
)
_WARNING_RE = re.compile(r"\bwarn(?:ing)?\b|\bdeprecat", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]{2,}")
_STOPWORDS = frozenset("""
    about after again also and any are because been before being but can could does doing done
    each for from have having here how into its just like make more most much need needs not now
    only other our out over please same should show some such than that the their them then there
    these they this those through too under until very want was were what when where which while
    who why will with would you your fix add find run use update change remove delete create write
    read file files code function help check work works working thing things get got let lets new
""".split())


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
        # The verdict applies the backend's calibrated thresholds (verdicts.py).
        if not isinstance(verdict, dict) or verdict.get("label") != "simple":
            return None
        return HINT.format(p=float(verdict.get("confidence", 0.0)))
    except Exception:
        return None


# -- 2. tool-output trimming ------------------------------------------------------------


def looks_like_failure(text: str) -> bool:
    return bool(_FAILURE_RE.search(text))


def task_terms(task: str) -> List[str]:
    """Distinctive words of a request (names, paths, identifiers), lower-cased."""
    terms = set()
    for word in _WORD_RE.findall(task or ""):
        word = word.lower().strip("./-")
        for part in [word] + re.split(r"[./_-]+", word):
            if len(part) >= 4 and part not in _STOPWORDS:
                terms.add(part)
    return sorted(terms)


def _line_cut(text: str, at: int, forward: bool) -> int:
    """``at`` moved to a line boundary within 200 chars, if there is one."""
    if forward:
        newline = text.find("\n", at, at + 200)
        return newline + 1 if newline != -1 else at
    newline = text.rfind("\n", max(0, at - 200), at)
    return newline + 1 if newline != -1 else at


def trimmed(text: str, head: int, tail: int, task: str = "") -> Optional[str]:
    """Head + tail of ``text``, keeping lines of the removed middle that mention
    the request or warn about something. None when that would not save enough."""
    head, tail = max(0, int(head)), max(0, int(tail))
    if head + tail >= len(text):
        return None
    cut_head = _line_cut(text, head, forward=True)
    cut_tail = _line_cut(text, len(text) - tail, forward=False)
    if cut_tail <= cut_head:
        cut_head, cut_tail = head, len(text) - tail
    middle = text[cut_head:cut_tail].split("\n")
    terms = task_terms(task)
    # A term found on most lines (e.g. "test" in a test log) says nothing about any one line.
    if terms and middle:
        lowered = [line.lower() for line in middle]
        terms = [t for t in terms if sum(t in line for line in lowered) <= max(3, len(middle) // 5)]
    kept, kept_chars = [], 0
    for i, line in enumerate(middle):
        low = line.lower()
        if (terms and any(t in low for t in terms)) or _WARNING_RE.search(line):
            if len(kept) >= _RESCUE_MAX_LINES or kept_chars + len(line) > _RESCUE_MAX_CHARS:
                break
            kept.append((i, line))
            kept_chars += len(line) + 1
    body, previous = [], -1
    for i, line in kept:
        if previous != -1 and i != previous + 1:
            body.append("…")
        body.append(line)
        previous = i
    note = TRIM_NOTE.format(
        lines=len(middle) - len(kept), chars=f"{cut_tail - cut_head - kept_chars:,}",
        kept=f"; kept {len(kept)} lines that mention the request or warn" if kept else "")
    result = text[:cut_head] + note + ("\n".join(body) + "\n" if body else "") + text[cut_tail:]
    return result if len(result) <= len(text) * (1 - _MIN_SAVING) else None


def trim_output(
    output: Any,
    cfg: Dict[str, Any],
    judge: JudgeFn,
    tool: str = "",
    tool_input: Any = None,
    failed: bool = False,
    task: str = "",
) -> Optional[str]:
    """Trimmed replacement for a disposable tool output, else None (keep it).

    ``task`` — the user's latest request — is the evidence the judge needs to
    tell whether the output still matters, and it picks the lines to keep.
    The judge applies the backend's calibrated rule (verdicts.py).
    """
    try:
        if not _feature(cfg, "trim_output") or not isinstance(output, str) or failed:
            return None
        head = max(0, int(_hooks(cfg, "head_chars", 1000)))
        tail = max(0, int(_hooks(cfg, "tail_chars", 500)))
        min_chars = max(int(cfg["thresholds"]["min_output_chars"]), head + tail + 500)
        if len(output) < min_chars or looks_like_failure(output):
            return None
        context = tool or "tool"
        if tool_input is not None:
            context += ": " + json.dumps(tool_input, default=str)[:_TOOL_INPUT_CHARS]
        verdict = judge(output, context, task or "")
        if not isinstance(verdict, dict) or verdict.get("needed") is not False:
            return None
        return trimmed(output, head, tail, task)
    except Exception:
        return None


# -- task memory (evidence for the output judge) --------------------------------------------
#
# State lives in ``state``: keyed by (TUI, session), atomic, private, locked.

TASK_MAX_AGE_S = 12 * 3600
_TASK_CHARS = 2000


def remember_prompt(session_id: Any, prompt: Any, tui: str = "") -> None:
    """Keep the user's latest request per session, for judging tool output later."""
    try:
        path = state.path("tasks", tui, session_id)
        if path is None or not isinstance(prompt, str) or not prompt.strip():
            return
        state.prune("tasks", TASK_MAX_AGE_S)
        state.write_json(path, {"task": prompt.strip()[:_TASK_CHARS], "at": time.time()})
    except Exception:
        pass


def last_prompt(session_id: Any, tui: str = "") -> str:
    try:
        path = state.path("tasks", tui, session_id)
        if path is None or not path.is_file() or time.time() - path.stat().st_mtime > TASK_MAX_AGE_S:
            return ""
        return str((state.read_json(path) or {}).get("task") or "")
    except Exception:
        return ""


# -- 3/4. compaction snapshot -----------------------------------------------------------


def save_snapshot(session_id: Any, messages: List[Dict[str, str]], cfg: Dict[str, Any],
                  trigger: str = "", tui: str = "") -> bool:
    """Persist ``messages`` (``[{role, text}]``) for ``session_id``. Never vetoes anything."""
    try:
        path = state.path("compact", tui, session_id)
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
        state.prune("compact", SNAPSHOT_MAX_AGE_S)
        with state.locked(state.key(tui, session_id)):
            state.write_json(path, {"session_id": session_id, "trigger": trigger,
                                    "saved_at": time.time(), "ready": False, "messages": kept})
        return True
    except Exception:
        return False


def mark_compacted(session_id: Any, tui: str = "") -> bool:
    """Flag a pending snapshot as ready: compaction really happened."""
    try:
        path = state.path("compact", tui, session_id)
        if path is None or not path.is_file():
            return False
        with state.locked(state.key(tui, session_id)):
            data = state.read_json(path)
            if data is None:
                return False
            data["ready"] = True
            state.write_json(path, data)
        return True
    except Exception:
        return False


def snapshot_transcript(session_id: Any, transcript_path: Any, cfg: Dict[str, Any],
                        trigger: str = "", tui: str = "") -> bool:
    """``save_snapshot`` fed from a transcript file on disk."""
    limit = int(_hooks(cfg, "snapshot_messages", 5))
    max_chars = int(_hooks(cfg, "snapshot_chars", 500))
    return save_snapshot(session_id, last_messages(transcript_path, limit, max_chars), cfg,
                         trigger, tui=tui)


def restore_snapshot(session_id: Any, cfg: Dict[str, Any], consume: bool = True,
                     require_ready: bool = False, tui: str = "",
                     commits: Optional[List[Callable[[], None]]] = None) -> Optional[str]:
    """Context text for a saved snapshot, else None.

    ``require_ready`` restores only snapshots ``mark_compacted`` has flagged
    (the next-prompt delivery path must not fire when compaction was skipped).

    Consuming is claim-then-commit, so a restore is never lost: under the
    session lock the snapshot is renamed to ``.claim`` (of two racing hooks,
    exactly one gets it), and the claim is deleted only once the text was
    delivered. Pass ``commits`` to defer that deletion: the caller runs the
    appended callables after writing its response. A claim that is never
    committed (hook killed mid-way) becomes restorable again after
    ``CLAIM_RECOVER_S``.
    """
    try:
        path = state.path("compact", tui, session_id)
        if path is None or not _feature(cfg, "compaction_snapshot"):
            return None
        claim = path.with_suffix(".claim")
        if not path.is_file() and not claim.is_file():
            return None
        with state.locked(state.key(tui, session_id)):
            source, data = path, state.read_json(path)
            if data is None and claim.is_file() and time.time() - claim.stat().st_mtime > CLAIM_RECOVER_S:
                source, data = claim, state.read_json(claim)
            if data is None or (require_ready and not data.get("ready")):
                return None
            if consume:
                if source is path:
                    os.replace(path, claim)
                os.utime(claim)  # the claim's age starts now
                if commits is None:
                    claim.unlink()
                else:
                    commits.append(lambda: claim.unlink(missing_ok=True))
        lines = [
            f"{m.get('role', '?')}: {m.get('text', '')}"
            for m in (data.get("messages") or [])
            if isinstance(m, dict) and str(m.get("text", "")).strip()
        ]
        return RESTORE_HEADER + "\n" + "\n".join(lines) if lines else None
    except Exception:
        return None
