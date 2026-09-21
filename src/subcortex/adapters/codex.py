"""Codex CLI hook adapter for subcortex.

Codex invokes ``subcortex hook codex <event>`` with the hook payload as JSON on
stdin; our JSON (if any) goes to stdout. Handled events:

- ``UserPromptSubmit`` — classify the prompt via ``/verdict/prompt``; prompts
  rated simple with confidence >= ``thresholds.prompt_simple_confidence`` get an
  ``additionalContext`` hint nudging the model toward the direct path.
- ``PostToolUse`` — tool output at least ``thresholds.min_output_chars`` long
  that looks successful is judged via ``/verdict/output``; disposable output
  (``p_needed`` below ``thresholds.output_needed_threshold``) is replaced with
  head + marker + tail via ``{"decision": "block", "reason": ...}``. Outputs
  with tracebacks/errors pass through untouched.
- ``PreCompact`` — write a snapshot of the last few transcript messages to
  ``~/.local/share/subcortex/compact/<session_id>.json``. Never vetoes.
- ``SessionStart`` (source ``compact``) — re-inject the snapshot as
  ``additionalContext`` and delete the file.

Everything fails open: any exception, unreachable daemon or timeout yields no
output and exit code 0.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..config import load_config

TIMEOUT_S = 5.0
HEAD_CHARS = 1000
TAIL_CHARS = 500
SNAPSHOT_MESSAGES = 5

SIMPLE_HINT = (
    "[subcortex] Local decision model rates this request as simple "
    "(confidence {confidence:.2f}). Prefer the most direct, minimal path."
)

TRUNCATED_NOTE = (
    "\n\n[subcortex: truncated {n} chars of low-value output; "
    "re-run the command if you need the rest]\n\n"
)


# -- daemon calls -----------------------------------------------------------------


def _post(path: str, payload: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """POST to the daemon and unwrap the verdict. Returns None on any failure.

    Accepts both wire shapes: the daemon's ``{"success": true, "verdict": {...}}``
    and a bare verdict object.
    """
    url = f"http://127.0.0.1:{int(cfg['port'])}{path}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(body, dict) or body.get("success") is False:
        return None
    verdict = body.get("verdict", body)
    return verdict if isinstance(verdict, dict) else None


def _additional_context(event: str, text: str) -> Dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


# -- UserPromptSubmit ---------------------------------------------------------------


def _on_user_prompt(payload: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    prompt = str(payload.get("prompt") or "")
    if not prompt.strip():
        return None
    verdict = _post("/verdict/prompt", {"prompt": prompt}, cfg)
    if not verdict:
        return None
    threshold = float(cfg["thresholds"]["prompt_simple_confidence"])
    confidence = float(verdict.get("confidence", 0.0))
    if verdict.get("label") == "simple" and confidence >= threshold:
        return _additional_context(
            "UserPromptSubmit", SIMPLE_HINT.format(confidence=confidence))
    return None


# -- PostToolUse ---------------------------------------------------------------------


def _tool_output_text(response: Any) -> str:
    """Best-effort flattening of Codex's tool_response into plain text."""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        for key in ("output", "stdout", "text", "content", "result"):
            value = response.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(response, ensure_ascii=False)
    if isinstance(response, list):
        parts = [item["text"] for item in response
                 if isinstance(item, dict) and isinstance(item.get("text"), str)]
        return "".join(parts) if parts else json.dumps(response, ensure_ascii=False)
    return str(response or "")


def _looks_successful(response: Any, text: str) -> bool:
    """Errors and tracebacks are high-value: they pass through unfiltered."""
    if "Traceback" in text:
        return False
    if isinstance(response, dict):
        if response.get("is_error") or response.get("isError"):
            return False
        for key in ("exit_code", "exitCode", "returncode"):
            code = response.get(key)
            if isinstance(code, int) and code != 0:
                return False
    return True


def _on_post_tool_use(payload: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    response = payload.get("tool_response")
    text = _tool_output_text(response)
    if len(text) < int(cfg["thresholds"]["min_output_chars"]):
        return None  # short output is cheap to keep; don't even call the daemon
    if not _looks_successful(response, text):
        return None
    context = str(payload.get("tool_name") or "")
    verdict = _post("/verdict/output", {"output": text, "context": context}, cfg)
    if not verdict:
        return None
    threshold = float(cfg["thresholds"]["output_needed_threshold"])
    p_needed = float(verdict.get("p_needed", 1.0))
    if verdict.get("needed", True) or p_needed >= threshold:
        return None
    trimmed = len(text) - HEAD_CHARS - TAIL_CHARS
    reason = text[:HEAD_CHARS] + TRUNCATED_NOTE.format(n=trimmed) + text[-TAIL_CHARS:]
    # "block" on PostToolUse replaces the tool result the model sees with `reason`;
    # the tool's side effects have already happened.
    return {"decision": "block", "reason": reason}


# -- PreCompact / SessionStart --------------------------------------------------------


def _compact_dir() -> Path:
    override = os.environ.get("SUBCORTEX_DATA_DIR", "").strip()
    base = Path(override) if override else Path.home() / ".local" / "share" / "subcortex"
    return base / "compact"


def _snapshot_path(session_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id) or "unknown"
    return _compact_dir() / f"{safe}.json"


def _extract_text(entry: Any) -> str:
    """Pull message text out of one transcript line (several shapes tolerated)."""
    if isinstance(entry, str):
        return entry
    if not isinstance(entry, dict):
        return ""
    message = entry.get("message")
    if isinstance(message, dict):
        return _extract_text(message)
    text = entry.get("text")
    if isinstance(text, str):
        return text
    content = entry.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part["text"] for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _read_transcript_texts(path_str: str) -> List[str]:
    """Non-empty message texts from a jsonl transcript; missing/corrupt → []."""
    texts: List[str] = []
    try:
        with open(path_str, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                text = _extract_text(entry).strip()
                if text:
                    texts.append(text)
    except OSError:
        pass
    return texts


def _on_pre_compact(payload: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    session_id = str(payload.get("session_id") or "")
    texts = _read_transcript_texts(str(payload.get("transcript_path") or ""))
    snapshot = {
        "session_id": session_id,
        "saved_at": time.time(),
        "trigger": str(payload.get("trigger") or ""),
        "messages": texts[-SNAPSHOT_MESSAGES:],
    }
    try:
        path = _snapshot_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2))
    except OSError:
        pass
    return None  # PreCompact never vetoes compaction


def _on_session_start(payload: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    source = str(payload.get("source") or payload.get("matcher") or "")
    if source != "compact":
        return None
    path = _snapshot_path(str(payload.get("session_id") or ""))
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    try:
        path.unlink()
    except OSError:
        pass
    messages = []
    if isinstance(data, dict):
        messages = [str(m) for m in data.get("messages", []) if str(m).strip()]
    if not messages:
        return None
    lines = [
        "[subcortex] Snapshot of the conversation before context compaction "
        "(most recent messages last):",
        "",
    ]
    lines += [f"{i}. {m}" for i, m in enumerate(messages, 1)]
    return _additional_context("SessionStart", "\n".join(lines))


# -- dispatch --------------------------------------------------------------------------


_HANDLERS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Optional[Dict[str, Any]]]] = {
    "UserPromptSubmit": _on_user_prompt,
    "PostToolUse": _on_post_tool_use,
    "PreCompact": _on_pre_compact,
    "SessionStart": _on_session_start,
}


def handle(event: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Handle one Codex hook event. Never raises; None means 'no opinion'."""
    try:
        if not isinstance(payload, dict):
            return None
        handler = _HANDLERS.get(str(event))
        if handler is None:
            return None
        return handler(payload, load_config())
    except Exception:
        return None


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry: event from argv, payload JSON from stdin, response JSON on
    stdout. Always exits 0 (fail-open)."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except ValueError:
        payload = {}
    event = args[-1] if args else str(payload.get("hook_event_name") or "")
    try:
        response = handle(event, payload)
    except Exception:
        response = None
    if response:
        try:
            sys.stdout.write(json.dumps(response) + "\n")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
