"""Installer for the OpenCode adapter.

Copies ``adapters/opencode/subcortex.ts`` into
``~/.config/opencode/plugins/subcortex.ts`` where the OpenCode runtime (Bun)
loads it at startup. All paths are resolved lazily so tests can redirect HOME.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, Optional

PLUGIN_NAME = "subcortex.ts"
BACKUP_SUFFIX = ".bak"


def _adapter_source() -> Optional[Path]:
    """Locate the bundled ``adapters/opencode/subcortex.ts``.

    Handles the repo ``src/`` layout (``src/subcortex/installers/opencode.py``)
    as well as installed layouts where the adapters tree ships next to or
    inside the package. Returns ``None`` when the file cannot be found.
    """
    here = Path(__file__).resolve()
    candidates = [
        # repo: <repo>/src/subcortex/installers/opencode.py -> <repo>/adapters/...
        here.parents[3] / "adapters" / "opencode" / PLUGIN_NAME,
        # installed: adapters shipped as package data inside subcortex/
        here.parents[1] / "adapters" / "opencode" / PLUGIN_NAME,
        # installed: adapters tree alongside site-packages/subcortex/
        here.parents[2] / "adapters" / "opencode" / PLUGIN_NAME,
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _plugin_dir() -> Path:
    return Path.home() / ".config" / "opencode" / "plugins"


def _dest() -> Path:
    return _plugin_dir() / PLUGIN_NAME


def _backup_path() -> Path:
    return _dest().with_name(PLUGIN_NAME + BACKUP_SUFFIX)


def _identical(a: Path, b: Path) -> bool:
    try:
        return a.is_file() and b.is_file() and a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def install() -> Dict[str, Any]:
    """Install the OpenCode plugin; backs up a differing pre-existing copy."""
    src = _adapter_source()
    if src is None:
        return {"installed": False, "error": "bundled adapter source not found",
                "path": str(_dest())}
    plugin_dir = _plugin_dir()
    plugin_dir.mkdir(parents=True, exist_ok=True)
    dest = _dest()
    backup: Optional[str] = None
    if dest.is_file() and not _identical(src, dest):
        bak = _backup_path()
        shutil.copyfile(dest, bak)
        backup = str(bak)
    shutil.copyfile(src, dest)
    return {"installed": True, "path": str(dest), "backup": backup}


def uninstall() -> Dict[str, Any]:
    """Remove only the installed plugin file; never touches anything else."""
    dest = _dest()
    removed = False
    if dest.is_file():
        try:
            dest.unlink()
            removed = True
        except OSError as exc:
            return {"removed": False, "path": str(dest), "error": str(exc)}
    return {"removed": removed, "path": str(dest)}


def status() -> Dict[str, Any]:
    """Report whether the plugin is present and identical to the bundled copy."""
    dest = _dest()
    src = _adapter_source()
    installed = dest.is_file()
    return {
        "installed": installed,
        "identical": bool(src and _identical(src, dest)),
        "source_found": src is not None,
        "path": str(dest),
        "backup": str(_backup_path()) if _backup_path().is_file() else None,
    }
