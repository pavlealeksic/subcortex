"""Tiny HTTP client for hook processes talking to the local daemon.

Stdlib only (hook processes start cold on every event, so imports stay light).
Every call returns ``None`` on any failure — connection refused, timeout, bad
JSON, ``success: false`` — so callers can fail open.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict, Optional


class DaemonClient:
    def __init__(self, cfg: Dict[str, Any], timeout: Optional[float] = None) -> None:
        self.base_url = f"http://127.0.0.1:{int(cfg['port'])}"
        self.timeout = float(timeout if timeout is not None
                             else (cfg.get("hooks") or {}).get("http_timeout_s", 3.0))

    def post(self, path: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            req = urllib.request.Request(
                self.base_url + path, data=json.dumps(payload).encode("utf-8"), method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
        if not isinstance(body, dict) or body.get("success") is False:
            return None
        return body

    def _verdict(self, path: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        body = self.post(path, payload)
        verdict = body.get("verdict") if body else None
        return verdict if isinstance(verdict, dict) else None

    def classify(self, prompt: str) -> Optional[Dict[str, Any]]:
        return self._verdict("/verdict/prompt", {"prompt": prompt})

    def judge(self, output: str, context: str) -> Optional[Dict[str, Any]]:
        return self._verdict("/verdict/output", {"output": output, "context": context})
