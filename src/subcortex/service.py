"""Start the daemon at login: a launchd agent (macOS) or a systemd user unit (Linux).

The service runs ``<daemon python> -m subcortex serve --foreground`` and is
restarted only if it crashes (a second instance exits 0 when the daemon is
already running, so there is no restart loop). All system commands go through
``runner`` so tests never touch launchctl/systemctl.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence
from xml.sax.saxutils import escape

from .config import data_dir, log_path
from .provision import daemon_argv, daemon_python

LABEL = "ai.subcortex.daemon"
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=30)


def platform_kind() -> Optional[str]:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    return None


def unit_path() -> Optional[Path]:
    kind = platform_kind()
    if kind == "launchd":
        return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if kind == "systemd":
        xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
        return (Path(xdg) if xdg else Path.home() / ".config") / "systemd" / "user" / "subcortex.service"
    return None


def _environment() -> Dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    for key in ("SUBCORTEX_CONFIG", "SUBCORTEX_DATA_DIR"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def render(python: Optional[str] = None) -> str:
    argv = daemon_argv(python or daemon_python())  # isolated; see provision.daemon_argv
    workdir = str(data_dir())
    env = _environment()
    if platform_kind() == "launchd":
        args = "".join(f"\n    <string>{escape(a)}</string>" for a in argv)
        envs = "".join(f"\n    <key>{escape(k)}</key><string>{escape(v)}</string>" for k, v in env.items())
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>{args}
  </array>
  <key>EnvironmentVariables</key>
  <dict>{envs}
  </dict>
  <key>WorkingDirectory</key><string>{escape(workdir)}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>{escape(str(log_path()))}</string>
  <key>StandardErrorPath</key><string>{escape(str(log_path()))}</string>
</dict>
</plist>
"""
    exec_start = " ".join(_systemd_quote(a) for a in argv)
    env_lines = "\n".join(f"Environment={_systemd_quote(f'{k}={v}')}" for k, v in env.items())
    return f"""[Unit]
Description=subcortex decision daemon

[Service]
ExecStart={exec_start}
WorkingDirectory={_systemd_quote(workdir)}
{env_lines}
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
"""


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def install(python: Optional[str] = None, runner: Runner = _run) -> List[str]:
    """Write and load the unit; returns human-readable notes (raises RuntimeError on failure)."""
    path = unit_path()
    if path is None:
        raise RuntimeError(f"no login service support for {sys.platform}; "
                           "hooks still start the daemon on demand")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(python))
    notes = [f"wrote {path}"]
    if platform_kind() == "launchd":
        domain = f"gui/{os.getuid()}"
        runner(["launchctl", "bootout", f"{domain}/{LABEL}"])  # reload if already loaded
        result = runner(["launchctl", "bootstrap", domain, str(path)])
        if result.returncode != 0:
            result = runner(["launchctl", "load", "-w", str(path)])
        if result.returncode != 0:
            raise RuntimeError(f"launchctl could not load {path}: {(result.stderr or '').strip()}")
        notes.append("loaded with launchctl (starts at every login)")
    else:
        for argv in (["systemctl", "--user", "daemon-reload"],
                     ["systemctl", "--user", "enable", "--now", "subcortex.service"]):
            result = runner(argv)
            if result.returncode != 0:
                raise RuntimeError(f"{' '.join(argv)} failed: {(result.stderr or '').strip()}")
        notes.append("enabled with systemctl --user (starts at every login)")
    return notes


def uninstall(runner: Runner = _run) -> List[str]:
    path = unit_path()
    if path is None or not path.exists():
        return ["no login service installed"]
    if platform_kind() == "launchd":
        runner(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"])
    else:
        runner(["systemctl", "--user", "disable", "--now", "subcortex.service"])
    path.unlink()
    if platform_kind() == "systemd":
        runner(["systemctl", "--user", "daemon-reload"])
    return [f"removed {path}"]


def status() -> Dict[str, object]:
    path = unit_path()
    return {"supported": path is not None, "installed": bool(path and path.exists()),
            "path": str(path) if path else None}
