"""subcortex command line: serve / decide / stats / doctor / hook / install / uninstall / status / mcp."""

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


def _resolve_tuis(args: argparse.Namespace) -> Optional[list]:
    from . import installers

    requested = list(args.tuis or []) + ([args.tui] if getattr(args, "tui", None) else [])
    if not requested:
        print("name at least one TUI (see: subcortex tuis)", file=sys.stderr)
        return None
    if requested == ["all"]:
        return installers.names()
    resolved = []
    for name in requested:
        key = installers.canonical_name(name)
        if key is None:
            print(f"unknown TUI {name!r}; supported: {', '.join(installers.names())}",
                  file=sys.stderr)
            return None
        resolved.append(key)
    return resolved


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _print_result(result) -> None:
    status = "ok" if result.ok else "FAILED"
    print(f"[{status}] {result.action} {result.tui}: {', '.join(result.paths)}")
    for saved in result.backups:
        print(f"       backup: {saved}")
    for message in result.messages:
        print(f"       {message}")


def cmd_install(args: argparse.Namespace) -> int:
    from .installers import get_installer

    tuis = _resolve_tuis(args)
    if tuis is None:
        return 2
    if args.backend:
        from .config import save_config

        save_config({"backend": args.backend})
        print(f"backend set to {args.backend!r}")
    ok = True
    for tui in tuis:
        installer = get_installer(tui, mcp=args.mcp)
        try:
            plans = installer.plans()
        except Exception as exc:
            print(f"[FAILED] {tui}: {exc}")
            ok = False
            continue
        changed = [p for p in plans if p.changed]
        if not changed:
            print(f"[ok] {tui}: already installed ({', '.join(str(p.path) for p in plans)})")
            continue
        for plan in changed:
            print(plan.diff() or f"(creates {plan.path})")
        if not args.dry_run and not args.yes and not _confirm(f"Apply these changes for {tui}?"):
            print(f"[skipped] {tui}: not confirmed (use --yes to apply non-interactively)")
            ok = False
            continue
        result = installer.install(dry_run=args.dry_run, run_self_test=not args.no_self_test,
                                   check_version=not args.ignore_version)
        _print_result(result)
        ok = ok and result.ok
    return 0 if ok else 1


def cmd_uninstall(args: argparse.Namespace) -> int:
    from .installers import get_installer

    tuis = _resolve_tuis(args)
    if tuis is None:
        return 2
    ok = True
    for tui in tuis:
        result = get_installer(tui, mcp=True).uninstall(dry_run=args.dry_run)
        _print_result(result)
        ok = ok and result.ok
    return 0 if ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    from . import installers

    names = installers.names() if not args.tuis else (_resolve_tuis(args) or [])
    rows = [installers.get_installer(n, mcp=True).status() for n in names]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    width = max(len(r["tui"]) for r in rows) if rows else 10
    for r in rows:
        mark = "installed" if r["installed"] else "-"
        if r.get("mcp_installed"):
            mark += "+mcp"
        found = r["detected"] or "not on PATH"
        print(f"{r['tui']:<{width}}  {r['seam']:<6}  {mark:<13}  {found}")
    return 0


def cmd_wrap(args: argparse.Namespace) -> int:
    """Run a command; print its output trimmed by the policy; exit with its code.

    For TUIs with no hook seam (Aider: ``test-cmd: subcortex wrap -- pytest -q``).
    Output is captured (stdout+stderr merged, in order) and printed once the
    command exits. Anything going wrong on our side prints the output as is.
    """
    argv = list(args.command or [])
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print("usage: subcortex wrap -- <command> [args...]", file=sys.stderr)
        return 2
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as exc:
        print(f"subcortex wrap: {exc}", file=sys.stderr)
        return 127
    output = proc.stdout.decode("utf-8", errors="replace")
    text = output
    if proc.returncode == 0:
        try:
            from . import policy
            from .client import DaemonClient

            cfg = load_config()
            text = policy.trim_output(output, cfg, DaemonClient(cfg).judge,
                                      tool="shell", tool_input={"command": " ".join(argv)}) or output
        except Exception:
            text = output
    sys.stdout.write(text)
    sys.stdout.flush()
    return proc.returncode


def cmd_mcp(args: argparse.Namespace) -> int:
    from . import mcp_server
    mcp_server.serve()
    return 0


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

    # `hook` is dispatched in main() before argparse runs (see there); this
    # entry only documents it in --help.
    sub.add_parser("hook", help="TUI hook entry point: subcortex hook <tui> [event] (payload on stdin)")

    for name, func, help_text in (
        ("install", cmd_install, "wire subcortex into one or more TUIs"),
        ("uninstall", cmd_uninstall, "remove subcortex from one or more TUIs"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("tuis", nargs="*", metavar="TUI", help="TUI name(s), or 'all'")
        p.add_argument("--tui", help=argparse.SUPPRESS)  # 0.1.0 spelling
        p.add_argument("--dry-run", action="store_true", help="show the change, write nothing")
        if name == "install":
            p.add_argument("--backend", choices=["laya", "jev"], default=None,
                           help="decision backend (default: keep configured one)")
            p.add_argument("--yes", "-y", action="store_true", help="apply without asking")
            p.add_argument("--no-self-test", action="store_true",
                           help="skip running the hook commands before writing them")
            p.add_argument("--ignore-version", action="store_true",
                           help="install even if the detected TUI version is too old for its hooks")
            p.add_argument("--mcp", action="store_true",
                           help="also register the `subcortex mcp` server where the TUI supports it")
        p.set_defaults(func=func)

    p_status = sub.add_parser("status", aliases=["tuis"], help="list supported TUIs and install state")
    p_status.add_argument("tuis", nargs="*", metavar="TUI")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status, tui=None)

    p_wrap = sub.add_parser("wrap", help="run a command and trim its disposable output (for TUIs without hooks)")
    p_wrap.add_argument("command", nargs=argparse.REMAINDER, help="-- <command> [args...]")
    p_wrap.set_defaults(func=cmd_wrap)

    p_mcp = sub.add_parser("mcp", help="run the stdio MCP server")
    p_mcp.set_defaults(func=cmd_mcp)

    return parser


def main(argv: Optional[list] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "hook":
        # Hooks bypass argparse entirely: a usage error would exit 2, which
        # several TUIs treat as "block this prompt". hook.main always returns 0.
        from .hook import main as hook_main

        return hook_main(argv[1:])
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
