"""Claude Code adapter: translate Claude Code hook payloads into subcortex verdicts.

``handle(event, payload)`` is the pure translation layer; ``main(argv)`` is the
hook entry point (event name on argv, hook JSON on stdin, response JSON on
stdout). Everything fails open: any exception, an unreachable daemon, or a
timeout results in exit 0 with no output, so Claude Code is never blocked by
subcortex.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import load_config

HTTP_TIMEOUT = 5.0
HEAD_CHARS = 1000
TAIL_CHARS = 500
SNAPSHOT_MAX_MESSAGES = 5
SNAPSHOT_TEXT_CHARS = 500
INPUT_CONTEXT_CHARS = 500

_EVENT_ALIASES = {
    "userpromptsubmit": "UserPromptSubmit",
    "posttooluse": "PostToolUse",
    "precompact": "PreCompact",
    "sessionstart": "SessionStart",
}

_ERROR_LINE_RE = re.compile(r"^\s*(error|fatal|exception)\b", re.IGNORECASE | re.MULTILINE)


def _compact_dir() -> Path:
    return Path.home() / ".local" / "share" / "subcortex" / "compact"


def _base_url(cfg: Dict[str, Any]) -> str:
    return f"http://127.0.0.1:{int(cfg['port'])}"


def _post(path: str, payload: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """POST JSON to the daemon. Returns the decoded body or None on any failure."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(_base_url(cfg) + path, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body if isinstance(body, dict) else None
    except Exception:
        return None


def _unwrap_verdict(body: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Accept both the daemon's ``{success, verdict}`` envelope and a bare verdict."""
    if not body:
        return None
    if body.get("success") is False:
        return None
    verdict = body.get("verdict")
    if isinstance(verdict, dict):
        return verdict
    return body


# -- UserPromptSubmit -------------------------------------------------------------


def _handle_user_prompt(payload: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    verdict = _unwrap_verdict(_post("/verdict/prompt", {"prompt": prompt}, cfg))
    if not verdict:
        return None
    threshold = float(cfg["thresholds"]["prompt_simple_confidence"])
    confidence = float(verdict.get("confidence", 0.0))
    if verdict.get("label") != "simple" or confidence < threshold:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                f"[subcortex] Local decision model rates this request as simple "
                f"(confidence {confidence}). Prefer the most direct, minimal path."
            ),
        }
    }


# -- PostToolUse ------------------------------------------------------------------


def _extract_output(tool_response: Any) -> Optional[str]:
    """Pull the judgeable text out of a tool_response (str or dict)."""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        for key in ("stdout", "content", "output"):
            value = tool_response.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, list):  # content blocks
                parts = [
                    block.get("text", "")
                    for block in value
                    if isinstance(block, dict) and isinstance(block.get("text"), str)
                ]
                if parts:
                    return "\n".join(parts)
    return None


def _looks_like_failure(text: str, tool_response: Any) -> bool:
    if isinstance(tool_response, dict):
        if tool_response.get("interrupted"):
            return True
        for key in ("exit_code", "exitCode", "returncode"):
            code = tool_response.get(key)
            if isinstance(code, int) and code != 0:
                return True
        stderr = tool_response.get("stderr")
        if isinstance(stderr, str) and stderr.strip() and "traceback" in stderr.lower():
            return True
    lowered = text.lower()
    if "traceback (most recent call last)" in lowered:
        return True
    return bool(_ERROR_LINE_RE.search(text))


def _truncate(text: str) -> str:
    removed = len(text) - HEAD_CHARS - TAIL_CHARS
    return (
        text[:HEAD_CHARS]
        + f"\n\n[subcortex: truncated {removed} chars of low-value output; "
          f"re-run the command if you need the rest]\n\n"
        + text[-TAIL_CHARS:]
    )


def _replace_output(tool_response: Any, truncated: str) -> Any:
    """Rebuild the tool output with ``truncated`` in place of the original text,
    preserving the shape Claude Code expects (Bash: stdout/stderr/interrupted/isImage)."""
    if isinstance(tool_response, str):
        return truncated
    if isinstance(tool_response, dict):
        updated = dict(tool_response)
        for key in ("stdout", "content", "output"):
            value = tool_response.get(key)
            if isinstance(value, str):
                updated[key] = truncated
                return updated
            if isinstance(value, list):
                updated[key] = [{"type": "text", "text": truncated}]
                return updated
        updated["stdout"] = truncated  # no text key found; fall back to Bash schema
        return updated
    return truncated


def _handle_post_tool_use(payload: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tool_response = payload.get("tool_response")
    text = _extract_output(tool_response)
    if not text:
        return None
    min_chars = int(cfg["thresholds"]["min_output_chars"])
    if len(text) < min_chars:
        return None
    if _looks_like_failure(text, tool_response):
        return None
    tool_name = str(payload.get("tool_name") or "tool")
    tool_input = json.dumps(payload.get("tool_input"), default=str)[:INPUT_CONTEXT_CHARS]
    verdict = _unwrap_verdict(
        _post("/verdict/output", {"output": text, "context": f"{tool_name}: {tool_input}"}, cfg))
    if not verdict or verdict.get("needed", True):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": _replace_output(tool_response, _truncate(text)),
        }
    }


# -- PreCompact / SessionStart ------------------------------------------------------


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "\n".join(p for p in parts if p)
    return ""


def _read_transcript_messages(path: Any) -> List[Dict[str, str]]:
    """Last few non-empty user/assistant texts from a transcript JSONL file."""
    if not isinstance(path, str) or not path:
        return []
    messages: List[Dict[str, str]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                role = entry.get("type")
                message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
                role = role or message.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = _message_text(message.get("content", entry.get("content")))
                text = text.strip()
                if text:
                    messages.append({"role": role, "text": text[:SNAPSHOT_TEXT_CHARS]})
    except OSError:
        return []
    return messages[-SNAPSHOT_MAX_MESSAGES:]


def _snapshot_path(session_id: Any) -> Optional[Path]:
    if not isinstance(session_id, str) or not session_id or "/" in session_id:
        return None
    return _compact_dir() / f"{session_id}.json"


def _handle_pre_compact(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    path = _snapshot_path(payload.get("session_id"))
    if path is None:
        return None
    snapshot = {
        "session_id": payload.get("session_id"),
        "trigger": payload.get("trigger"),
        "custom_instructions": payload.get("custom_instructions"),
        "saved_at": time.time(),
        "messages": _read_transcript_messages(payload.get("transcript_path")),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, indent=2))
    except OSError:
        pass
    return None  # PreCompact never blocks and has nothing to say


def _handle_session_start(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    path = _snapshot_path(payload.get("session_id"))
    if path is None or not path.is_file():
        return None
    try:
        snapshot = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    try:
        path.unlink()
    except OSError:
        pass
    messages = snapshot.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    lines = [
        f"{m.get('role', '?')}: {m.get('text', '')}"
        for m in messages
        if isinstance(m, dict) and m.get("text")
    ]
    if not lines:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "[subcortex] Pre-compaction context:\n" + "\n".join(lines),
        }
    }


# -- entry points -------------------------------------------------------------------


def handle(event: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Translate a Claude Code hook event into a stdout JSON response, or None.

    Never raises: any failure means "no opinion", which Claude Code treats as
    pass-through.
    """
    try:
        if not isinstance(payload, dict):
            return None
        canonical = _EVENT_ALIASES.get(str(event).strip().lower())
        cfg = load_config()
        if canonical == "UserPromptSubmit":
            return _handle_user_prompt(payload, cfg)
        if canonical == "PostToolUse":
            return _handle_post_tool_use(payload, cfg)
        if canonical == "PreCompact":
            return _handle_pre_compact(payload)
        if canonical == "SessionStart":
            return _handle_session_start(payload)
        return None
    except Exception:
        return None


def main(argv: Optional[List[str]] = None) -> int:
    """Hook entry: ``main(["UserPromptSubmit"])`` with the hook JSON on stdin.

    Always exits 0 (fail-open); writes the response JSON to stdout when there
    is one.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    event = args[0] if args else str(payload.get("hook_event_name") or "")
    response = handle(event, payload)
    if response is not None:
        try:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
