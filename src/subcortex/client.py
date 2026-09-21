"""Tiny HTTP client for hook processes talking to the local daemon.

Stdlib only (hook processes start cold on every event, so imports stay light).
Every call returns ``None`` on any failure — connection refused, timeout, bad
JSON, ``success: false`` — so callers can fail open.

When the daemon is down (connection refused) and ``hooks.autostart_daemon`` is
on, the client starts it in the background — at most once a minute, never
waiting for it — so hooks come back to life after a reboot. The current hook
still fails open; the daemon's single-instance lock prevents duplicates.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

AUTOSTART_INTERVAL_S = 60.0


class DaemonClient:
    def __init__(self, cfg: Dict[str, Any], timeout: Optional[float] = None) -> None:
        self.base_url = f"http://127.0.0.1:{int(cfg['port'])}"
        hooks = cfg.get("hooks") or {}
        self.timeout = float(timeout if timeout is not None else hooks.get("http_timeout_s", 3.0))
        self.autostart = bool(hooks.get("autostart_daemon", True))

    def post(self, path: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            req = urllib.request.Request(
                self.base_url + path, data=json.dumps(payload).encode("utf-8"), method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ConnectionRefusedError):
                self._autostart()
            return None
        except Exception:
            return None
        if not isinstance(body, dict) or body.get("success") is False:
            return None
        return body

    def _autostart(self) -> None:
        if not self.autostart:
            return
        try:
            from .config import VENV_PYTHON, data_dir

            stamp = data_dir() / "autostart.stamp"
            if stamp.exists() and time.time() - stamp.stat().st_mtime < AUTOSTART_INTERVAL_S:
                return
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.touch()
            python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
            with open(data_dir() / "daemon.log", "ab") as log:
                subprocess.Popen([python, "-m", "subcortex", "serve", "--foreground"],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True, close_fds=True, env=dict(os.environ))
        except Exception:
            pass

    def _verdict(self, path: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        body = self.post(path, payload)
        verdict = body.get("verdict") if body else None
        return verdict if isinstance(verdict, dict) else None

    def classify(self, prompt: str) -> Optional[Dict[str, Any]]:
        return self._verdict("/verdict/prompt", {"prompt": prompt})

    def judge(self, output: str, context: str) -> Optional[Dict[str, Any]]:
        return self._verdict("/verdict/output", {"output": output, "context": context})
