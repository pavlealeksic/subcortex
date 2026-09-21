"""subcortex daemon: stdlib ThreadingHTTPServer on 127.0.0.1:<port>.

Endpoints:

- ``POST /decide``         ``{state, questions, backend?}`` → ``{success, answers|error}``
- ``POST /verdict/prompt`` ``{prompt}``            → ``{success, verdict|error}``
- ``POST /verdict/output`` ``{output, context?}``  → ``{success, verdict|error}``
- ``GET  /health``         → ``{ok, backend, model}``
- ``GET  /stats``          → in-memory counters + uptime

Policy endpoints — the four behaviors from ``subcortex.policy``, for plugin-based
TUIs (OpenCode, Amp, ...) so no plugin re-implements thresholds or heuristics:

- ``POST /v1/prompt-hint``  ``{prompt}``                          → ``{success, hint|null}``
- ``POST /v1/tool-output``  ``{output, tool?, input?, failed?}``  → ``{success, replacement|null}``
- ``POST /v1/snapshot``     ``{session_id, messages, trigger?}``  → ``{success, saved}``
- ``POST /v1/restore``      ``{session_id}``                      → ``{success, context|null}``

Single instance via an ``fcntl`` lock at ``~/.local/share/subcortex/daemon.lock``;
PID file and log alongside it. The backend is constructed lazily on the first
request and its model loads lazily on the first verdict.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional

from .backends import get_backend
from .backends.base import DecisionBackend
from .config import DATA_DIR, LOCK_PATH, LOG_PATH, PID_PATH, load_config
from .metrics import METRICS
from . import policy, verdicts

BackendFactory = Callable[..., DecisionBackend]


def _daemon_backend(state: Any, override: Optional[str] = None) -> DecisionBackend:
    """Cached per-name backend lookup; construction (not model load) happens here."""
    name = str(override or state.config.get("backend") or "laya").strip().lower()
    with state.lock:
        if name not in state.backends:
            state.backends[name] = state.factory(state.config, name)
        return state.backends[name]


def make_handler(state: Any):
    class Handler(BaseHTTPRequestHandler):
        def _log(self, msg: str) -> None:
            log_path = getattr(self.server, "log_path", None)
            if not log_path:
                return
            try:
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(f"{self.log_date_time_string()} {msg}\n")
            except OSError:
                pass

        def log_message(self, fmt: str, *args: Any) -> None:
            self._log(f"{self.address_string()} {fmt % args}")

        def _send_json(self, code: int, obj: Dict[str, Any]) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> Optional[Dict[str, Any]]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0 or length > 10_000_000:
                return None
            try:
                data = json.loads(self.rfile.read(length))
            except ValueError:
                return None
            return data if isinstance(data, dict) else None

        # -- GET -----------------------------------------------------------------

        def do_GET(self) -> None:
            if self.path == "/health":
                cfg = state.config
                self._send_json(200, {
                    "ok": True,
                    "backend": cfg.get("backend", "laya"),
                    "model": cfg.get("model") if cfg.get("backend", "laya") == "laya"
                             else (cfg.get("jev") or {}).get("model"),
                })
            elif self.path == "/stats":
                self._send_json(200, METRICS.snapshot())
            else:
                self._send_json(404, {"success": False, "error": "not found"})

        # -- POST -----------------------------------------------------------------

        def do_POST(self) -> None:
            if self.path == "/decide":
                self._handle_decide()
            elif self.path == "/verdict/prompt":
                self._handle_verdict_prompt()
            elif self.path == "/verdict/output":
                self._handle_verdict_output()
            elif self.path in _POLICY_ROUTES:
                self._handle_policy(_POLICY_ROUTES[self.path])
            else:
                self._send_json(404, {"success": False, "error": "not found"})

        def _handle_decide(self) -> None:
            payload = self._read_json()
            if payload is None or "state" not in payload or "questions" not in payload:
                self._send_json(400, {"success": False, "error": "need {state, questions}"})
                return
            try:
                backend = _daemon_backend(state, payload.get("backend"))
                start = time.perf_counter()
                result = backend.predict(payload["state"], payload["questions"])
                METRICS.record("decide", round((time.perf_counter() - start) * 1000.0, 2))
                self._send_json(200, {"success": True, "answers": result.get("answers", result)})
            except Exception as exc:
                self._log(f"decide failed: {exc}")
                self._send_json(200, {"success": False, "error": str(exc)})

        def _handle_verdict_prompt(self) -> None:
            payload = self._read_json()
            if payload is None or "prompt" not in payload:
                self._send_json(400, {"success": False, "error": "need {prompt}"})
                return
            try:
                backend = _daemon_backend(state, payload.get("backend"))
                verdict = verdicts.classify_prompt(str(payload["prompt"]), backend=backend, config=state.config)
            except Exception as exc:
                self._log(f"verdict/prompt failed: {exc}")
                verdict = None
            if verdict is None:
                self._send_json(200, {"success": False, "error": "verdict failed"})
            else:
                self._send_json(200, {"success": True, "verdict": verdict})

        def _handle_verdict_output(self) -> None:
            payload = self._read_json()
            if payload is None or "output" not in payload:
                self._send_json(400, {"success": False, "error": "need {output}"})
                return
            try:
                backend = _daemon_backend(state, payload.get("backend"))
                verdict = verdicts.judge_output(
                    str(payload["output"]),
                    context=str(payload.get("context", "")),
                    backend=backend,
                    config=state.config,
                )
            except Exception as exc:
                self._log(f"verdict/output failed: {exc}")
                verdict = None
            if verdict is None:
                self._send_json(200, {"success": False, "error": "verdict failed"})
            else:
                self._send_json(200, {"success": True, "verdict": verdict})

        def _handle_policy(self, route: str) -> None:
            payload = self._read_json()
            if payload is None:
                self._send_json(400, {"success": False, "error": "need a JSON object"})
                return
            cfg = state.config
            try:
                if route in ("prompt-hint", "tool-output"):
                    backend = _daemon_backend(state, payload.get("backend"))
                if route == "prompt-hint":
                    hint = policy.prompt_hint(
                        payload.get("prompt"), cfg,
                        lambda p: verdicts.classify_prompt(p, backend=backend, config=cfg))
                    METRICS.record("hint_given" if hint else "hint_skipped")
                    result: Dict[str, Any] = {"hint": hint}
                elif route == "tool-output":
                    replacement = policy.trim_output(
                        payload.get("output"), cfg,
                        lambda o, c: verdicts.judge_output(o, context=c, backend=backend, config=cfg),
                        tool=str(payload.get("tool") or ""),
                        tool_input=payload.get("input"),
                        failed=bool(payload.get("failed")))
                    METRICS.record("output_trimmed" if replacement else "output_kept")
                    result = {"replacement": replacement}
                elif route == "snapshot":
                    messages = payload.get("messages")
                    saved = policy.save_snapshot(
                        payload.get("session_id"), messages if isinstance(messages, list) else [],
                        cfg, str(payload.get("trigger") or ""))
                    result = {"saved": saved}
                else:  # restore
                    result = {"context": policy.restore_snapshot(payload.get("session_id"), cfg)}
            except Exception as exc:
                self._log(f"/v1/{route} failed: {exc}")
                self._send_json(200, {"success": False, "error": "policy failed"})
                return
            self._send_json(200, {"success": True, **result})

    return Handler


_POLICY_ROUTES = {
    "/v1/prompt-hint": "prompt-hint",
    "/v1/tool-output": "tool-output",
    "/v1/snapshot": "snapshot",
    "/v1/restore": "restore",
}


def create_server(
    port: int,
    config: Optional[Dict[str, Any]] = None,
    backend_factory: Optional[BackendFactory] = None,
) -> ThreadingHTTPServer:
    """Build (but do not start) the daemon HTTP server. ``port=0`` picks an
    ephemeral port — used by tests with a stubbed ``backend_factory``."""
    cfg = config or load_config()
    state = SimpleNamespace(
        config=cfg,
        factory=backend_factory or get_backend,
        backends={},
        lock=threading.Lock(),
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    server.daemon_threads = True
    return server


def _acquire_lock():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return None
    return fd


def run(port: Optional[int] = None, config: Optional[Dict[str, Any]] = None) -> int:
    """Run the daemon in the foreground. Holds the single-instance lock for the
    process lifetime; writes/removes the PID file."""
    cfg = config or load_config()
    lock_fd = _acquire_lock()
    if lock_fd is None:
        print(f"subcortex daemon already running (lock: {LOCK_PATH})", file=sys.stderr)
        return 1
    server = create_server(int(port or cfg["port"]), cfg)
    server.log_path = str(LOG_PATH)
    PID_PATH.write_text(str(os.getpid()))
    actual_port = server.server_address[1]
    print(f"subcortex daemon listening on 127.0.0.1:{actual_port} (pid {os.getpid()})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            PID_PATH.unlink()
        except OSError:
            pass
        lock_fd.close()
    return 0
