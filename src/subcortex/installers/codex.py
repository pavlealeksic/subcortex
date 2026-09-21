"""Install/remove subcortex hooks in Codex CLI's ``config.toml``.

Codex config is TOML. stdlib ``tomllib`` reads but cannot write, so we append
our own block wrapped in ``# >>> subcortex`` / ``# <<< subcortex`` marker
comments and remove exactly that block on uninstall. The merged file is
validated with ``tomllib`` before anything is written, and the original is
backed up to ``config.toml.subcortex.bak``. Everything else in the file is
preserved byte-for-byte.

Public API: ``install() -> bool``, ``uninstall() -> bool``, ``status() -> dict``.

IMPORTANT: Codex CLI requires new hooks to be *trusted* (per definition hash)
before they run. After installing, open the Codex TUI and run ``/hooks`` to
trust the subcortex hooks — ``install()`` prints this reminder too.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional

MARKER_BEGIN = "# >>> subcortex"
MARKER_END = "# <<< subcortex"

TRUST_REMINDER = (
    "Codex CLI requires new hooks to be trusted before they run:\n"
    "open the Codex TUI and run /hooks to trust the subcortex hooks "
    "(hook definitions are hashed; editing config.toml re-triggers the prompt)."
)

_EVENTS = ("UserPromptSubmit", "PostToolUse", "PreCompact", "SessionStart")


def _config_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    base = Path(codex_home) if codex_home else Path.home() / ".codex"
    return base / "config.toml"


def _backup_path(config_path: Path) -> Path:
    return config_path.with_name(config_path.name + ".subcortex.bak")


def _command(event: str) -> str:
    """Absolute subcortex command; falls back to ``python -m subcortex``."""
    exe = shutil.which("subcortex")
    argv: List[str] = [exe] if exe else [sys.executable, "-m", "subcortex"]
    return shlex.join([*argv, "hook", "codex", event])


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _block() -> str:
    """Our TOML block: one inline hook per event, matcher groups where needed."""
    lines = [
        MARKER_BEGIN,
        "# subcortex — local decision layer (https://pypi.org/project/subcortex/)",
        "# remove with: subcortex uninstall --tui codex",
        "",
        "[[hooks.UserPromptSubmit.hooks]]",
        'type = "command"',
        f"command = {_toml_str(_command('UserPromptSubmit'))}",
        "timeout = 10",
        "",
        "[[hooks.PostToolUse]]",
        'matcher = "Bash"',
        "",
        "[[hooks.PostToolUse.hooks]]",
        'type = "command"',
        f"command = {_toml_str(_command('PostToolUse'))}",
        "timeout = 10",
        "",
        "[[hooks.PreCompact.hooks]]",
        'type = "command"',
        f"command = {_toml_str(_command('PreCompact'))}",
        "timeout = 10",
        "",
        "[[hooks.SessionStart]]",
        'matcher = "compact"',
        "",
        "[[hooks.SessionStart.hooks]]",
        'type = "command"',
        f"command = {_toml_str(_command('SessionStart'))}",
        "timeout = 10",
        MARKER_END,
        "",
    ]
    return "\n".join(lines)


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _backup(path: Path) -> Optional[Path]:
    if not path.is_file():
        return None
    backup = _backup_path(path)
    try:
        shutil.copy2(path, backup)
    except OSError:
        return None
    return backup


def _strip_block(text: str) -> str:
    """Remove every ``# >>> subcortex`` ... ``# <<< subcortex`` span."""
    out: List[str] = []
    inside = False
    for line in text.splitlines():
        if line.strip() == MARKER_BEGIN:
            inside = True
            continue
        if line.strip() == MARKER_END and inside:
            inside = False
            continue
        if not inside:
            out.append(line)
    cleaned = "\n".join(out)
    # collapse the blank run our block may leave behind
    while "\n\n\n" in cleaned:
        cleaned = cleaned.replace("\n\n\n", "\n\n")
    return cleaned.strip("\n") + "\n" if cleaned.strip() else ""


def install() -> bool:
    """Append subcortex hooks to Codex's config.toml. Idempotent."""
    path = _config_path()
    text = _read(path)
    if MARKER_BEGIN in text:
        print(f"subcortex hooks already installed in {path}")
        print(TRUST_REMINDER)
        return True
    if text.strip():
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            print(f"refusing to edit {path}: existing config is invalid TOML ({exc}); "
                  "fix it first", file=sys.stderr)
            return False
    merged = text
    if merged and not merged.endswith("\n"):
        merged += "\n"
    if merged.strip():
        merged += "\n"
    merged += _block()
    try:
        tomllib.loads(merged)
    except tomllib.TOMLDecodeError as exc:
        print(f"refusing to write {path}: merged config would be invalid TOML ({exc})",
              file=sys.stderr)
        return False
    backup = _backup(path)
    _write(path, merged)
    print(f"installed subcortex hooks into {path}")
    if backup:
        print(f"backup of the previous config: {backup}")
    print(TRUST_REMINDER)
    return True


def uninstall() -> bool:
    """Remove exactly the marked subcortex block from Codex's config.toml."""
    path = _config_path()
    text = _read(path)
    if MARKER_BEGIN not in text:
        print(f"no subcortex hooks found in {path}")
        return True
    cleaned = _strip_block(text)
    try:
        tomllib.loads(cleaned)
    except tomllib.TOMLDecodeError as exc:
        print(f"refusing to write {path}: result would be invalid TOML ({exc})",
              file=sys.stderr)
        return False
    backup = _backup(path)
    _write(path, cleaned)
    print(f"removed subcortex hooks from {path}")
    if backup:
        print(f"backup of the previous config: {backup}")
    return True


def status() -> Dict[str, Any]:
    """Report whether the subcortex hooks are present in Codex's config."""
    path = _config_path()
    text = _read(path)
    installed = MARKER_BEGIN in text and MARKER_END in text
    return {
        "tui": "codex",
        "installed": installed,
        "config_path": str(path),
        "config_exists": path.is_file(),
        "backup_path": str(_backup_path(path)) if _backup_path(path).is_file() else None,
        "commands": {event: _command(event) for event in _EVENTS} if installed else {},
        "note": TRUST_REMINDER if installed else "",
    }
