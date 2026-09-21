"""Simple in-memory counters + uptime, exposed by /stats."""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = time.time()
        self._counts: Dict[str, int] = {}
        self._latency_totals: Dict[str, float] = {}
        self._sums: Dict[str, float] = {}
        self._notes: Dict[str, str] = {}

    def record(self, name: str, latency_ms: Optional[float] = None) -> None:
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + 1
            if latency_ms is not None:
                self._latency_totals[name] = self._latency_totals.get(name, 0.0) + latency_ms

    def add(self, name: str, amount: float) -> None:
        """Accumulate a quantity (tokens, dollars, characters)."""
        with self._lock:
            self._sums[name] = self._sums.get(name, 0) + amount

    def note(self, name: str, value: str) -> None:
        """Remember the latest value of something (e.g. the model that answered)."""
        with self._lock:
            self._notes[name] = value

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            counts = dict(self._counts)
            totals = dict(self._latency_totals)
            sums = {k: round(v, 6) if isinstance(v, float) else v for k, v in self._sums.items()}
            notes = dict(self._notes)
            started = self._started
        avg = {
            name: round(totals[name] / counts[name], 2)
            for name in totals
            if counts.get(name)
        }
        return {
            "uptime_s": round(time.time() - started, 1),
            "counts": counts,
            "avg_latency_ms": avg,
            "totals": sums,
            "latest": notes,
        }


METRICS = Metrics()
