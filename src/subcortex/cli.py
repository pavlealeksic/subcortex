"""subcortex command line: serve / decide / stats / doctor."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from . import __version__, daemon
from .backends import get_backend
from .config import LOG_PATH, PID_PATH, VENV_PYTHON, load_config


def _http(method: str, url: str, payload: Optional[Dict[str, Any]] = None,
          timeout: float = 30.0) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _base_url(cfg: Dict[str, Any]) -> str:
    return f"http://127.0.0.1:{int(cfg['port'])}"


def _health(cfg: Dict[str, Any], timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    try:
        data = _http("GET", f"{_base_url(cfg)}/health", timeout=timeout)
        return data if data.get("ok") else None
    except Exception:
        return None


def _daemon_interpreter() -> str:
    """Prefer the dedicated backend venv; fall back to the current interpreter."""
    return str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable


def _spawn_daemon(cfg: Dict[str, Any]) -> subprocess.Popen:
    """Spawn a detached background daemon (new session, logs to daemon.log)."""
    src_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(src_root) + os.pathsep + env.get("PYTHONPATH", "")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = open(LOG_PATH, "ab")
    proc = subprocess.Popen(
        [_daemon_interpreter(), "-m", "subcortex", "serve", "--foreground"],
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True, env=env,
    )
    return proc


def _wait_for_health(cfg: Dict[str, Any], timeout_s: float = 30.0) -> Optional[Dict[str, Any]]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        health = _health(cfg)
        if health:
            return health
        time.sleep(0.2)
    return None


def _ensure_daemon(cfg: Dict[str, Any]) -> bool:
    if _health(cfg):
        return True
    print(f"daemon not running on port {cfg['port']}; starting it...", file=sys.stderr)
    _spawn_daemon(cfg)
    return _wait_for_health(cfg) is not None


# -- subcommands -----------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.stop:
        return _stop_daemon()
    if args.foreground:
        return daemon.run(config=cfg)
    health = _health(cfg)
    if health:
        print(f"subcortex daemon already running on 127.0.0.1:{cfg['port']} "
              f"(backend {health.get('backend')})")
        return 0
    proc = _spawn_daemon(cfg)
    if _wait_for_health(cfg):
        print(f"subcortex daemon started: pid {proc.pid}, port {cfg['port']} "
              f"(log: {LOG_PATH})")
        return 0
    print(f"daemon (pid {proc.pid}) did not come up within 30s; check {LOG_PATH}",
          file=sys.stderr)
    return 1


def _stop_daemon() -> int:
    try:
        pid = int(PID_PATH.read_text().strip())
    except (OSError, ValueError):
        print("no daemon PID file; is the daemon running?", file=sys.stderr)
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print(f"daemon pid {pid} is not running; cleaning up PID file")
        try:
            PID_PATH.unlink()
        except OSError:
            pass
        return 0
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    print(f"stopped subcortex daemon (pid {pid})")
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    cfg = load_config()
    try:
        questions = json.loads(args.questions)
    except ValueError:
        print("--questions must be a JSON object", file=sys.stderr)
        return 2
    try:
        state: Any = json.loads(args.state)
    except ValueError:
        state = args.state  # plain string state is fine
    if not _ensure_daemon(cfg):
        print("could not reach or start the daemon", file=sys.stderr)
        return 1
    payload: Dict[str, Any] = {"state": state, "questions": questions}
    if args.backend:
        payload["backend"] = args.backend
    try:
        resp = _http("POST", f"{_base_url(cfg)}/decide", payload)
    except Exception as exc:
        print(f"decide request failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(resp, indent=2))
    return 0 if resp.get("success") else 1


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = load_config()
    try:
        resp = _http("GET", f"{_base_url(cfg)}/stats")
    except Exception:
        print(f"daemon not reachable on 127.0.0.1:{cfg['port']} "
              "(start it with: subcortex serve)", file=sys.stderr)
        return 1
    print(json.dumps(resp, indent=2))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    print(f"subcortex {__version__} doctor\n")

    cfg: Dict[str, Any] = {}
    try:
        cfg = load_config()
        print(f"[ok] config loads (backend={cfg['backend']}, port={cfg['port']}, "
              f"model={cfg.get('model')})")
    except Exception as exc:
        print(f"[FAIL] config does not load: {exc}")
        print("       fix: remove or repair ~/.config/subcortex/config.json")
        return 1

    try:
        backend = get_backend(cfg)
        available, reason = backend.available()
        if available:
            print(f"[ok] backend {backend.name!r}: {reason}")
        else:
            ok = False
            print(f"[FAIL] backend {backend.name!r}: {reason}")
    except Exception as exc:
        ok = False
        print(f"[FAIL] backend: {exc}")

    health = _health(cfg)
    if health:
        print(f"[ok] daemon healthy on 127.0.0.1:{cfg['port']} "
              f"(backend {health.get('backend')}, model {health.get('model')})")
    else:
        print(f"[..] daemon not running on port {cfg['port']}; starting it...")
        _spawn_daemon(cfg)
        health = _wait_for_health(cfg)
        if health:
            print(f"[ok] daemon started and healthy on 127.0.0.1:{cfg['port']}")
        else:
            ok = False
            print(f"[FAIL] daemon did not come up; check {LOG_PATH}")

    print(f"\n{'all checks passed' if ok else 'some checks failed — see fixes above'}")
    return 0 if ok else 1


# -- parser -----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="subcortex",
        description="Local decision layer for coding-agent TUIs",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the daemon (background by default)")
    p_serve.add_argument("--foreground", action="store_true",
                         help="run in-process instead of spawning a detached daemon")
    p_serve.add_argument("--stop", action="store_true", help="stop the running daemon")
    p_serve.set_defaults(func=cmd_serve)

    p_decide = sub.add_parser("decide", help="one-shot typed decision via the daemon")
    p_decide.add_argument("--state", required=True, help="state (JSON or plain string)")
    p_decide.add_argument("--questions", required=True, help="questions as a JSON object")
    p_decide.add_argument("--backend", choices=["laya", "jev"], default=None,
                          help="override the configured backend for this call")
    p_decide.set_defaults(func=cmd_decide)

    p_stats = sub.add_parser("stats", help="pretty-print daemon /stats")
    p_stats.set_defaults(func=cmd_stats)

    p_doctor = sub.add_parser("doctor", help="check config, backend and daemon health")
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
