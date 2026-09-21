"""Install/uninstall subcortex hooks in Claude Code's ``~/.claude/settings.json``.

``install()`` merges hook entries (existing content is preserved and backed up
to ``settings.json.subcortex.bak``); ``uninstall()`` removes exactly the entries
subcortex added; ``status()`` reports what is currently wired. Other keys in
the settings file are never touched.
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# event -> matcher (None = no matcher field)
HOOKS: Dict[str, Optional[str]] = {
    "UserPromptSubmit": None,
    "PostToolUse": "Bash",
    "PreCompact": "auto|manual",
    "SessionStart": "compact",
}

HOOK_TIMEOUT = 10
_MARKER = "subcortex"


def _settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _backup_path(settings: Path) -> Path:
    return settings.with_name(settings.name + ".subcortex.bak")


def _executable_command() -> str:
    """Absolute path to the subcortex CLI, or ``python -m subcortex`` as fallback."""
    exe = shutil.which("subcortex")
    if exe:
        return exe
    return shlex.join([sys.executable, "-m", "subcortex"])


def _hook_command(event: str) -> str:
    return f"{_executable_command()} hook claude {event}"


def _is_subcortex_hook(hook: Any) -> bool:
    return isinstance(hook, dict) and _MARKER in str(hook.get("command", ""))


def _read_settings(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_settings(path: Path, settings: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        shutil.copy2(path, _backup_path(path))
    path.write_text(json.dumps(settings, indent=2) + "\n")


def install() -> Dict[str, Any]:
    """Merge subcortex hooks into ~/.claude/settings.json. Idempotent."""
    path = _settings_path()
    settings = _read_settings(path)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = settings["hooks"] = {}

    added: List[str] = []
    for event, matcher in HOOKS.items():
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            groups = hooks[event] = []
        already = any(
            _is_subcortex_hook(h)
            for group in groups
            if isinstance(group, dict)
            for h in (group.get("hooks") or [])
        )
        if already:
            continue
        entry = {"type": "command", "command": _hook_command(event), "timeout": HOOK_TIMEOUT}
        group = None
        for candidate in groups:
            if isinstance(candidate, dict) and candidate.get("matcher") == matcher:
                group = candidate
                break
        if group is None:
            group = {"hooks": []}
            if matcher is not None:
                group["matcher"] = matcher
            groups.append(group)
        if not isinstance(group.get("hooks"), list):
            group["hooks"] = []
        group["hooks"].append(entry)
        added.append(event)

    _write_settings(path, settings)
    return {
        "settings_path": str(path),
        "backup": str(_backup_path(path)),
        "added": added,
        "already_present": [e for e in HOOKS if e not in added],
    }


def uninstall() -> Dict[str, Any]:
    """Remove exactly the hook entries subcortex added; prune empty groups."""
    path = _settings_path()
    settings = _read_settings(path)
    hooks = settings.get("hooks")
    removed: List[str] = []
    if isinstance(hooks, dict):
        for event in list(hooks.keys()):
            groups = hooks.get(event)
            if not isinstance(groups, list):
                continue
            kept_groups = []
            event_removed = False
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    kept_groups.append(group)
                    continue
                kept = [h for h in group["hooks"] if not _is_subcortex_hook(h)]
                if len(kept) != len(group["hooks"]):
                    event_removed = True
                if kept:
                    group["hooks"] = kept
                    kept_groups.append(group)
            if event_removed:
                removed.append(event)
            if kept_groups:
                hooks[event] = kept_groups
            else:
                del hooks[event]
    _write_settings(path, settings)
    return {"settings_path": str(path), "removed": removed}


def status() -> Dict[str, Any]:
    """Report which subcortex hooks are currently installed."""
    path = _settings_path()
    settings = _read_settings(path)
    hooks = settings.get("hooks")
    events: Dict[str, bool] = {}
    entries = 0
    if isinstance(hooks, dict):
        for event in HOOKS:
            groups = hooks.get(event)
            count = sum(
                1
                for group in (groups or [])
                if isinstance(group, dict)
                for h in (group.get("hooks") or [])
                if _is_subcortex_hook(h)
            )
            events[event] = count > 0
            entries += count
    return {
        "settings_path": str(path),
        "settings_exists": path.is_file(),
        "installed": entries == len(HOOKS),
        "events": events,
        "entries": entries,
    }
