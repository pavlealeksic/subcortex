"""subcortex daemon: stdlib ThreadingHTTPServer on 127.0.0.1:<port>.

Endpoints:

- ``POST /decide``         ``{state, questions, backend?}`` → ``{success, answers|error}``
- ``POST /verdict/prompt`` ``{prompt}``                     → ``{success, verdict|error}``
- ``POST /verdict/output`` ``{output, context?, task?}``    → ``{success, verdict|error}``
- ``GET  /health``         → ``{ok, version, backend, model}`` (no token needed)
- ``GET  /stats``          → in-memory counters + uptime

Policy endpoints — the four behaviors from ``subcortex.policy``, for plugin-based
TUIs (OpenCode, Amp, ...) so no plugin re-implements thresholds or heuristics:

- ``POST /v1/prompt-hint``  ``{prompt, session_id?, tui?}``       → ``{success, hint|null}``
- ``POST /v1/tool-output``  ``{output, tool?, input?, failed?, session_id?, tui?, task?}``
                                                                  → ``{success, replacement|null}``
- ``POST /v1/snapshot``     ``{session_id, messages, trigger?, tui?}`` → ``{success, saved}``
- ``POST /v1/restore``      ``{session_id, tui?}``                → ``{success, context|null}``

Every request except ``GET /health`` must carry the per-user token
(``auth.HEADER``); requests with an ``Origin``, a foreign ``Host`` or a
non-JSON body are refused, so web pages can't reach it either. Model work is
bounded: at most ``MAX_CONCURRENT_DECISIONS`` run at once and a request that
can't start before its client gives up (``X-Subcortex-Timeout-Ms``) gets 503
and fails open. The config file is re-read when it changes, and the daemon
exits (to be restarted by the next hook or the login service) when a newer
subcortex is installed underneath it.

Single instance via an ``fcntl`` lock in the data dir; PID file and a rotated
log alongside it. The backend is constructed lazily on the first request and
its model loads lazily on the first verdict.
"""

from __future__ import annotations

import fcntl
import hmac
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from . import __version__, auth, ledger, policy, verdicts
from .backends import get_backend
from .backends.base import DecisionBackend
from .config import config_path, data_dir, load_config, lock_path, log_path, pid_path
from .metrics import METRICS

MAX_CONCURRENT_DECISIONS = 4
DEFAULT_CLIENT_TIMEOUT_S = 3.0
LOG_MAX_BYTES = 5_000_000
VERSION_CHECK_S = 30.0
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")

BackendFactory = Callable[..., DecisionBackend]


def _daemon_backend(state: Any, override: Optional[str] = None) -> DecisionBackend:
    """Cached per-name backend lookup; construction (not model load) happens here."""
    _refresh_config(state)
    name = str(override or state.config.get("backend") or "laya").strip().lower()
    with state.lock:
        if name not in state.backends:
            state.backends[name] = state.factory(state.config, name)
        return state.backends[name]


def _host_name(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        return value[:value.find("]") + 1] if "]" in value else value
    return value.split(":", 1)[0]


def _config_stamp() -> Any:
    try:
        st = config_path().stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _refresh_config(state: Any) -> None:
    """Pick up config changes (an opt-out must apply without a restart)."""
    if not state.watch_config:
        return
    now = time.monotonic()
    if now - state.config_checked < 1.0:
        return
    state.config_checked = now
    stamp = _config_stamp()
    if stamp == state.config_stamp:
        return
    fresh = load_config()
    with state.lock:
        backend_keys = ("backend", "model", "jev", "daemon_python")
        if any(fresh.get(k) != state.config.get(k) for k in backend_keys):
            state.backends.clear()
        state.config, state.config_stamp = fresh, stamp


def _installed_version() -> str:
    """The version string of the subcortex now on disk (may differ from ours)."""
    try:
        text = (Path(__file__).resolve().parent / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return __version__
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return __version__


def make_handler(state: Any):
    class Handler(BaseHTTPRequestHandler):
        # Socket reads/writes only (not model time): a client that connects and
        # stalls must not pin a thread forever.
        timeout = 15

        def _log(self, msg: str) -> None:
            _append_log(getattr(self.server, "log_path", None), f"{self.log_date_time_string()} {msg}")

        def log_message(self, fmt: str, *args: Any) -> None:
            if getattr(self.server, "log_requests", False):
                self._log(f"{self.address_string()} {fmt % args}")

        def _refuse(self, code: int, error: str) -> None:
            METRICS.record(f"refused_{code}")
            self._send_json(code, {"success": False, "error": error})

        def _allowed(self, needs_token: bool) -> bool:
            """Only this user's own local processes: no browsers, no other users."""
            host = _host_name(self.headers.get("Host") or "")
            if host and host not in _LOCAL_HOSTS:  # DNS rebinding
                self._refuse(403, "foreign Host")
                return False
            if self.headers.get("Origin"):
                self._refuse(403, "cross-origin requests are refused")
                return False
            if needs_token and state.token:
                sent = self.headers.get(auth.HEADER) or ""
                if not hmac.compare_digest(sent.encode(), state.token.encode()):
                    self._refuse(401, "missing or wrong token")
                    return False
            return True

        def _deadline(self) -> float:
            try:
                budget = float(self.headers.get("X-Subcortex-Timeout-Ms") or 0) / 1000.0
            except ValueError:
                budget = 0.0
            budget = budget if 0 < budget <= 3600 else DEFAULT_CLIENT_TIMEOUT_S
            return self.received + budget - 0.1

        def _decision_slot(self) -> bool:
            """Wait for a model slot only as long as the client still listens."""
            wait = self._deadline() - time.monotonic()
            if wait > 0 and state.slots.acquire(timeout=wait):
                return True
            self._refuse(503, "busy")
            return False

        def _send_json(self, code: int, obj: Dict[str, Any]) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> Optional[Dict[str, Any]]:
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type != "application/json":
                return None
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0 or length > 10_000_000:
                return None
            try:
                data = json.loads(self.rfile.read(length))
            except (ValueError, RecursionError, OSError):  # garbage, pathological nesting, stalled client
                return None
            return data if isinstance(data, dict) else None

        # -- GET -----------------------------------------------------------------

        def do_GET(self) -> None:
            self.received = time.monotonic()
            if not self._allowed(needs_token=self.path != "/health"):
                return
            if self.path == "/health":
                _refresh_config(state)
                cfg = state.config
                self._send_json(200, {
                    "ok": True,
                    "version": __version__,
                    "pid": os.getpid(),
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
            self.received = time.monotonic()
            if not self._allowed(needs_token=True):
                return
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
            if not self._decision_slot():
                return
            try:
                backend = _daemon_backend(state, payload.get("backend"))
                start = time.perf_counter()
                result = backend.predict(payload["state"], payload["questions"])
                METRICS.record("decide", round((time.perf_counter() - start) * 1000.0, 2))
                reply = {"success": True, "answers": result.get("answers", result)}
                for key in ("model", "usage"):
                    if key in result:
                        reply[key] = result[key]
            except Exception as exc:
                self._log(f"decide failed: {exc}")
                reply = {"success": False, "error": str(exc)}
            finally:
                state.slots.release()
            self._send_json(200, reply)

        def _handle_verdict_prompt(self) -> None:
            payload = self._read_json()
            if payload is None or "prompt" not in payload:
                self._send_json(400, {"success": False, "error": "need {prompt}"})
                return
            if not self._decision_slot():
                return
            try:
                backend = _daemon_backend(state)
                verdict = verdicts.classify_prompt(str(payload["prompt"]), backend=backend, config=state.config)
            except Exception as exc:
                self._log(f"verdict/prompt failed: {exc}")
                verdict = None
            finally:
                state.slots.release()
            if verdict is None:
                self._send_json(200, {"success": False, "error": "verdict failed"})
            else:
                self._send_json(200, {"success": True, "verdict": verdict})

        def _handle_verdict_output(self) -> None:
            payload = self._read_json()
            if payload is None or "output" not in payload:
                self._send_json(400, {"success": False, "error": "need {output}"})
                return
            if not self._decision_slot():
                return
            try:
                backend = _daemon_backend(state)
                verdict = verdicts.judge_output(
                    str(payload["output"]),
                    context=str(payload.get("context", "")),
                    backend=backend,
                    config=state.config,
                    task=str(payload.get("task") or ""),
                )
            except Exception as exc:
                self._log(f"verdict/output failed: {exc}")
                verdict = None
            finally:
                state.slots.release()
            if verdict is None:
                self._send_json(200, {"success": False, "error": "verdict failed"})
            else:
                self._send_json(200, {"success": True, "verdict": verdict})

        def _handle_policy(self, route: str) -> None:
            payload = self._read_json()
            if payload is None:
                self._send_json(400, {"success": False, "error": "need a JSON object"})
                return
            _refresh_config(state)
            cfg = state.config
            # Plugins name their TUI so sessions of different TUIs never share state.
            tui = str(payload.get("tui") or "plugin")
            sid = payload.get("session_id")
            decides = route in ("prompt-hint", "tool-output")
            if decides and not self._decision_slot():
                return
            commits: List[Callable[[], None]] = []
            try:
                if decides:
                    backend = _daemon_backend(state)
                if route == "prompt-hint":
                    policy.remember_prompt(sid, payload.get("prompt"), tui=tui)
                    hint = policy.prompt_hint(
                        payload.get("prompt"), cfg,
                        lambda p: verdicts.classify_prompt(p, backend=backend, config=cfg))
                    METRICS.record("hint_given" if hint else "hint_skipped")
                    result: Dict[str, Any] = {"hint": hint}
                    if hint:
                        commits.append(lambda: ledger.record("hint", tui))
                elif route == "tool-output":
                    replacement = policy.trim_output(
                        payload.get("output"), cfg,
                        lambda o, c, t: verdicts.judge_output(o, context=c, backend=backend,
                                                              config=cfg, task=t),
                        tool=str(payload.get("tool") or ""),
                        tool_input=payload.get("input"),
                        failed=bool(payload.get("failed")),
                        task=str(payload.get("task") or policy.last_prompt(sid, tui=tui)))
                    METRICS.record("output_trimmed" if replacement else "output_kept")
                    result = {"replacement": replacement}
                    if replacement:
                        before, after = len(str(payload.get("output") or "")), len(replacement)
                        commits.append(lambda: ledger.record("trim", tui, before=before, after=after))
                elif route == "snapshot":
                    messages = payload.get("messages")
                    saved = policy.save_snapshot(
                        sid, messages if isinstance(messages, list) else [],
                        cfg, str(payload.get("trigger") or ""), tui=tui)
                    result = {"saved": saved}
                else:  # restore: deleted only once the reply went out (claim-then-commit)
                    result = {"context": policy.restore_snapshot(sid, cfg, tui=tui, commits=commits)}
                    if result["context"]:
                        commits.append(lambda: ledger.record("restore", tui))
            except Exception as exc:
                self._log(f"/v1/{route} failed: {exc}")
                self._send_json(200, {"success": False, "error": "policy failed"})
                return
            finally:
                if decides:
                    state.slots.release()
            self._send_json(200, {"success": True, **result})
            self.wfile.flush()
            for commit in commits:
                try:
                    commit()
                except Exception:
                    pass

    return Handler


_POLICY_ROUTES = {
    "/v1/prompt-hint": "prompt-hint",
    "/v1/tool-output": "tool-output",
    "/v1/snapshot": "snapshot",
    "/v1/restore": "restore",
}


def _append_log(path: Optional[str], line: str) -> None:
    if not path:
        return
    try:
        log = Path(path)
        if log.is_file() and log.stat().st_size > LOG_MAX_BYTES:
            log.replace(log.with_name(log.name + ".1"))
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")
    except OSError:
        pass


class _Server(ThreadingHTTPServer):
    # The stdlib default backlog is 5: a burst of hooks from several sessions
    # (plus plugins) would get connection resets. Every hook must get an answer.
    request_queue_size = 256
    daemon_threads = True
    log_path: Optional[str] = None
    log_requests = False

    def server_bind(self) -> None:
        # HTTPServer.server_bind resolves its own address (socket.getfqdn), a
        # reverse-DNS lookup that can hang for many seconds on some machines.
        # We only ever serve 127.0.0.1: skip it.
        import socketserver

        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client that gave up (hook budget spent) is normal, not an error.
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        import traceback

        _append_log(self.log_path, f"request from {client_address} failed:\n{traceback.format_exc()}")


def create_server(
    port: int,
    config: Optional[Dict[str, Any]] = None,
    backend_factory: Optional[BackendFactory] = None,
    token: Optional[str] = None,
) -> ThreadingHTTPServer:
    """Build (but do not start) the daemon HTTP server. ``port=0`` picks an
    ephemeral port — used by tests with a stubbed ``backend_factory``. The token
    defaults to the per-user one in the data dir (created if missing); pass ""
    to disable the check. A given ``config`` is used as is (never reloaded)."""
    cfg = config or load_config()
    state = SimpleNamespace(
        config=cfg,
        factory=backend_factory or get_backend,
        backends={},
        lock=threading.Lock(),
        slots=threading.BoundedSemaphore(MAX_CONCURRENT_DECISIONS),
        token=auth.ensure_token() if token is None else token,
        watch_config=config is None,
        config_checked=time.monotonic(),
        config_stamp=_config_stamp(),
    )
    return _Server(("127.0.0.1", port), make_handler(state))


def _acquire_lock():
    data_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = open(lock_path(), "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return None
    return fd


def _watch_version(server: ThreadingHTTPServer) -> None:
    """Exit when a different subcortex version is installed underneath us, so
    the next hook (or the login service) starts the new code."""
    while True:
        time.sleep(VERSION_CHECK_S)
        installed = _installed_version()
        if installed != __version__:
            _append_log(getattr(server, "log_path", None),
                        f"subcortex {installed} is installed (running {__version__}); exiting to restart")
            server.restart_requested = True
            server.shutdown()
            return


def run(port: Optional[int] = None, config: Optional[Dict[str, Any]] = None) -> int:
    """Run the daemon in the foreground. Holds the single-instance lock for the
    process lifetime; writes/removes the PID file; stops cleanly on SIGTERM."""
    cfg = config or load_config()
    _append_log(str(log_path()), f"{time.strftime('%Y-%m-%d %H:%M:%S')} subcortex {__version__} "
                                 f"starting (pid {os.getpid()}, port {port or cfg['port']})")
    lock_fd = _acquire_lock()
    if lock_fd is None:
        # Another instance is serving: not an error (a login service must not
        # restart-loop because a hook already started the daemon).
        print(f"subcortex daemon already running (lock: {lock_path()})", file=sys.stderr)
        return 0
    server = create_server(int(port or cfg["port"]), None if config is None else cfg)
    server.log_path = str(log_path())
    pid_path().write_text(str(os.getpid()))
    # serve_forever must return (so the PID file is removed) on SIGTERM too.
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    threading.Thread(target=_watch_version, args=(server,), daemon=True).start()
    actual_port = server.server_address[1]
    message = f"subcortex daemon {__version__} listening on 127.0.0.1:{actual_port} (pid {os.getpid()})"
    _append_log(server.log_path, f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}")
    print(message, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            pid_path().unlink()
        except OSError:
            pass
        lock_fd.close()
    # 75 (EX_TEMPFAIL) makes launchd/systemd start the new version; hooks would too.
    return 75 if getattr(server, "restart_requested", False) else 0
