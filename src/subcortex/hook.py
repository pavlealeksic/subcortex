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
        cfg: Optional[Dict[str, Any]] = None, client: Any = None,
        commits: Optional[List[Any]] = None) -> Optional[str]:
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
    # Consumed state (restored snapshots) is committed only after delivery.
    event.commits = commits

    cfg = cfg or load_config()
    if client is None:
        from .client import DaemonClient

        client = DaemonClient(cfg)

    # The adapter can tell a compaction happened (e.g. a manual /compress).
    compacted = bool(event.extra.get("compacted"))
    if compacted and kind != PRE_COMPACT:
        policy.mark_compacted(event.session_id, tui=adapter.name)

    response = None
    if kind == PROMPT:
        policy.remember_prompt(event.session_id, event.prompt, tui=adapter.name)
        # The slow part (a daemon round trip) first, the restore last: context is
        # claimed only when there is budget left to deliver it.
        hint = policy.prompt_hint(event.prompt, cfg, client.classify) if adapter.delivers_hints else None
        parts = [adapter.prompt_context(event, cfg), hint]
        text = "\n\n".join(p for p in parts if p)
        response = adapter.render_prompt(event, text) if text else None
    elif kind == TOOL_OUTPUT:
        replacement = policy.trim_output(
            event.output, cfg, client.judge, event.tool, event.tool_input, event.failed,
            task=policy.last_prompt(event.session_id, tui=adapter.name))
        # Some TUIs can only deliver restored context alongside a tool result.
        context = adapter.output_context(event, cfg)
        if context:
            event.extra["context"] = context
        if replacement or event.extra.get("context"):
            response = adapter.render_tool_output(event, replacement)
    elif kind == PRE_COMPACT:
        if event.messages is not None:
            policy.save_snapshot(event.session_id, event.messages, cfg, event.trigger, tui=adapter.name)
        else:
            policy.snapshot_transcript(event.session_id, event.transcript_path, cfg, event.trigger,
                                       tui=adapter.name)
        if compacted:  # only once the snapshot exists
            policy.mark_compacted(event.session_id, tui=adapter.name)
        response = adapter.render_pre_compact(event)
    elif kind == POST_COMPACT:
        policy.mark_compacted(event.session_id, tui=adapter.name)
        response = adapter.render_post_compact(event)
    elif kind == SESSION_START and adapter.is_after_compaction(event):
        context = policy.restore_snapshot(event.session_id, cfg, tui=adapter.name, commits=commits)
        response = adapter.render_session_start(event, context) if context else None

    response = adapter.guard(kind, response)
    if response is None:
        return None
    return response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)


# Held while the response line is written: the watchdog must not cut it in half.
_WRITING = threading.Lock()
MAX_STDIN_BYTES = 64 * 1024 * 1024


def _bail() -> None:
    # Budget exhausted: leave without output. os._exit skips cleanup on purpose.
    if _WRITING.acquire(blocking=False):
        os._exit(0)


def _budget(cfg: Dict[str, Any]) -> float:
    try:
        budget = float((cfg.get("hooks") or {}).get("budget_s", 4.0))
    except (TypeError, ValueError):
        return 4.0
    return min(max(budget, 0.5), 30.0) if budget == budget else 4.0  # NaN -> default


def main(argv: Optional[List[str]] = None) -> int:
    """Hook process entry. Returns 0 on every path."""
    real_stdout = sys.stdout
    real_stderr = sys.stderr
    debug = bool(os.environ.get("SUBCORTEX_DEBUG"))
    # Streams are captured before anything else can print.
    sys.stdout = io.StringIO()
    if not debug:
        sys.stderr = io.StringIO()
    timer = None
    started = time.perf_counter()
    args: List[str] = []
    backstop = None
    try:
        args = [str(a) for a in (sys.argv[1:] if argv is None else argv)]
        tui = args[0] if args else ""
        event_name = args[1] if len(args) > 1 else None

        from .config import load_config

        cfg = load_config()
        budget = _budget(cfg)
        timer = threading.Timer(budget, _bail)
        timer.daemon = True
        timer.start()
        # The timer needs the GIL; C code (a regex, a JSON parse of a huge
        # payload) can hold it for longer than the budget. faulthandler's
        # watchdog is a C thread: it ends the process regardless (status 1,
        # which the installed "|| true" guard turns into 0).
        import faulthandler

        backstop = open(os.devnull, "w")
        faulthandler.dump_traceback_later(budget + 1.0, exit=True, file=backstop)

        stdin = getattr(sys.stdin, "buffer", None)
        data = stdin.read(MAX_STDIN_BYTES + 1) if stdin is not None else b""
        if len(data) > MAX_STDIN_BYTES:
            raise ValueError("hook payload too large")
        commits: List[Any] = []
        out = run(tui, event_name, data.decode("utf-8", "replace"), cfg, commits=commits)

        with _WRITING:
            timer.cancel()
            sys.stdout, sys.stderr = real_stdout, real_stderr
            if out:
                real_stdout.write(out + "\n")
                real_stdout.flush()
                for commit in commits:  # delivered: consumed state can go
                    try:
                        commit()
                    except Exception:
                        pass
        if debug:
            ms = (time.perf_counter() - started) * 1000
            _log(f"{' '.join(args)} ok {ms:.0f}ms -> {out[:200] if out else '(no output)'}")
    except BaseException:  # noqa: BLE001 — SystemExit/KeyboardInterrupt included, by design
        if timer is not None:
            timer.cancel()
        sys.stdout, sys.stderr = real_stdout, real_stderr
        _log(f"{' '.join(args)} failed:\n{traceback.format_exc()}")
    finally:
        if backstop is not None:
            import faulthandler

            faulthandler.cancel_dump_traceback_later()
            backstop.close()
    return 0


def entry() -> None:
    """Console-script entry (``subcortex-hook``): exit status is always 0."""
    main()
    sys.exit(0)


if __name__ == "__main__":
    entry()
