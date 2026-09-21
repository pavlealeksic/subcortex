"""Hardened entry point for every command-hook TUI.

    subcortex-hook <tui> [<event>]          (console script)
    subcortex hook <tui> [<event>]          (same thing, routed before argparse)
    python -m subcortex.hook <tui> [<event>]

The hook JSON arrives on stdin; the response (if any) goes to stdout. The
process contract is what keeps TUIs safe, so it is enforced here, not in the
adapters:

- exit status is ALWAYS 0 — even for bad arguments, unknown TUIs/events,
  invalid JSON, exceptions, ``SystemExit`` or ``KeyboardInterrupt``. (Exit 2
  means "block" in Claude Code and several of its imitators.)
- a watchdog ends the process silently (exit 0, no output) once the
  ``hooks.budget_s`` wall-clock budget is spent, so a hung daemon can never
  stall a turn.
- stdout/stderr are captured while work runs; only the final response is
  written, as one line. Nothing is ever written to stderr (some TUIs surface
  it to the model or the user) unless ``SUBCORTEX_DEBUG`` is set.
- the adapter's ``guard`` drops any response carrying a blocking field.

Failures are appended to ``<data_dir>/hooks.log``; with ``SUBCORTEX_DEBUG=1``
every invocation is logged there too.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import traceback
from typing import Any, Dict, List, Optional

LOG_MAX_BYTES = 1_000_000


def _log(line: str) -> None:
    try:
        from .config import data_dir

        path = data_dir() / "hooks.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_suffix(".log.1"))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line.rstrip() + "\n")
    except Exception:
        pass


def run(tui: str, event_name: Optional[str], raw: str,
        cfg: Optional[Dict[str, Any]] = None, client: Any = None) -> Optional[str]:
    """Process one hook invocation; returns the stdout text or None.

    Pure with respect to the process (no exit, no stream handling) so tests and
    the installer self-test can call it directly. May raise; ``main`` doesn't.
    """
    from . import policy
    from .adapters import get_adapter
    from .adapters.base import POST_COMPACT, PRE_COMPACT, PROMPT, SESSION_START, TOOL_OUTPUT
    from .config import load_config

    adapter = get_adapter(tui)
    if adapter is None:
        return None
    try:
        payload = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    resolved = adapter.resolve(event_name or payload.get(adapter.event_field))
    if resolved is None:
        return None
    name, kind = resolved
    event = adapter.parse(name, kind, payload)
    if event is None:
        return None

    cfg = cfg or load_config()
    if client is None:
        from .client import DaemonClient

        client = DaemonClient(cfg)

    # The adapter can tell a compaction happened (e.g. a manual /compress).
    compacted = bool(event.extra.get("compacted"))
    if compacted and kind != PRE_COMPACT:
        policy.mark_compacted(event.session_id)

    response = None
    if kind == PROMPT:
        parts = [adapter.prompt_context(event, cfg),
                 policy.prompt_hint(event.prompt, cfg, client.classify)]
        text = "\n\n".join(p for p in parts if p)
        response = adapter.render_prompt(event, text) if text else None
    elif kind == TOOL_OUTPUT:
        replacement = policy.trim_output(
            event.output, cfg, client.judge, event.tool, event.tool_input, event.failed)
        # An adapter may also deliver context here (event.extra["context"]).
        if replacement or event.extra.get("context"):
            response = adapter.render_tool_output(event, replacement)
    elif kind == PRE_COMPACT:
        if event.messages is not None:
            policy.save_snapshot(event.session_id, event.messages, cfg, event.trigger)
        else:
            policy.snapshot_transcript(event.session_id, event.transcript_path, cfg, event.trigger)
        if compacted:  # only once the snapshot exists
            policy.mark_compacted(event.session_id)
        response = adapter.render_pre_compact(event)
    elif kind == POST_COMPACT:
        policy.mark_compacted(event.session_id)
        response = adapter.render_post_compact(event)
    elif kind == SESSION_START and adapter.is_after_compaction(event):
        context = policy.restore_snapshot(event.session_id, cfg)
        response = adapter.render_session_start(event, context) if context else None

    response = adapter.guard(kind, response)
    if response is None:
        return None
    return response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)


def _bail() -> None:
    # Budget exhausted: leave without output. os._exit skips cleanup on purpose.
    os._exit(0)


def main(argv: Optional[List[str]] = None) -> int:
    """Hook process entry. Returns 0 on every path."""
    real_stdout = sys.stdout
    real_stderr = sys.stderr
    debug = bool(os.environ.get("SUBCORTEX_DEBUG"))
    timer = None
    started = time.perf_counter()
    args: List[str] = []
    try:
        args = [str(a) for a in (sys.argv[1:] if argv is None else argv)]
        tui = args[0] if args else ""
        event_name = args[1] if len(args) > 1 else None

        from .config import load_config

        cfg = load_config()
        budget = float((cfg.get("hooks") or {}).get("budget_s", 4.0))
        timer = threading.Timer(max(0.5, budget), _bail)
        timer.daemon = True
        timer.start()

        sys.stdout = io.StringIO()
        if not debug:
            sys.stderr = io.StringIO()
        raw = sys.stdin.read() if sys.stdin is not None else ""
        out = run(tui, event_name, raw, cfg)

        timer.cancel()
        sys.stdout, sys.stderr = real_stdout, real_stderr
        if out:
            real_stdout.write(out + "\n")
            real_stdout.flush()
        if debug:
            ms = (time.perf_counter() - started) * 1000
            _log(f"{' '.join(args)} ok {ms:.0f}ms -> {out[:200] if out else '(no output)'}")
    except BaseException:  # noqa: BLE001 — SystemExit/KeyboardInterrupt included, by design
        if timer is not None:
            timer.cancel()
        sys.stdout, sys.stderr = real_stdout, real_stderr
        _log(f"{' '.join(args)} failed:\n{traceback.format_exc()}")
    return 0


def entry() -> None:
    """Console-script entry (``subcortex-hook``): exit status is always 0."""
    main()
    sys.exit(0)


if __name__ == "__main__":
    entry()
