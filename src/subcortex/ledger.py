"""What subcortex did, for ``subcortex stats``: one JSON line per event.

Hooks and the daemon append counts only — never prompts or output — to a
private (0600) file in the data dir. Appends of one short line are atomic, so
concurrent hook processes need no lock. The file rotates at ``MAX_BYTES``.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Iterator, Optional

from .config import data_dir

MAX_BYTES = 5_000_000


def _path():
    return data_dir() / "ledger.jsonl"


def record(kind: str, tui: str = "", **fields: Any) -> None:
    """Append one event (``hint``, ``trim``, ``restore``, ``jev``). Never raises."""
    try:
        path = _path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            if path.stat().st_size > MAX_BYTES:
                os.replace(path, path.with_name("ledger.1.jsonl"))
        except FileNotFoundError:
            pass
        line = json.dumps({"t": int(time.time()), "k": kind, "tui": tui, **fields},
                          separators=(",", ":")) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass


def _events() -> Iterator[Dict[str, Any]]:
    for name in ("ledger.1.jsonl", "ledger.jsonl"):
        try:
            with open(data_dir() / name, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        yield event
        except OSError:
            continue


def summary(since: Optional[float] = None) -> Dict[str, Any]:
    """Totals overall and per TUI, optionally only for events after ``since``."""
    total: Dict[str, Any] = {"hints": 0, "trims": 0, "chars_removed": 0, "restores": 0,
                             "jev_calls": 0, "jev_input_tokens": 0, "jev_cost_usd": 0.0}
    per_tui: Dict[str, Dict[str, int]] = {}
    first = None
    for event in _events():
        t = event.get("t") or 0
        if since is not None and t < since:
            continue
        first = t if first is None else min(first, t)
        kind, tui = event.get("k"), str(event.get("tui") or "?")
        row = per_tui.setdefault(tui, {"hints": 0, "trims": 0, "chars_removed": 0, "restores": 0})
        if kind == "hint":
            total["hints"] += 1
            row["hints"] += 1
        elif kind == "trim":
            removed = max(0, int(event.get("before", 0)) - int(event.get("after", 0)))
            total["trims"] += 1
            total["chars_removed"] += removed
            row["trims"] += 1
            row["chars_removed"] += removed
        elif kind == "restore":
            total["restores"] += 1
            row["restores"] += 1
        elif kind == "jev":
            total["jev_calls"] += 1
            total["jev_input_tokens"] += int(event.get("tokens", 0))
            total["jev_cost_usd"] += float(event.get("usd", 0.0))
    total["jev_cost_usd"] = round(total["jev_cost_usd"], 6)
    per_tui = {k: v for k, v in per_tui.items() if k != "?" or any(v.values())}
    return {"since": first, "total": total,
            "per_tui": {k: v for k, v in per_tui.items() if any(v.values())}}
