"""Configuration for subcortex.

Config file: ``~/.config/subcortex/config.json`` (override the path with
``SUBCORTEX_CONFIG``). Missing file = defaults. ``SUBCORTEX_*`` env vars
override individual keys.

Keys::

    backend    "laya" | "jev"            (default "laya")
    port       daemon port               (default 7707)
    model      laya model alias          (default "multilingual")
    thresholds prompt_simple_confidence  (null = calibrated per backend)
               output_needed_threshold   (null = calibrated per backend)
               min_output_chars          (6000)
    features   prompt_hint, trim_output, compaction_snapshot  (all true)
    hooks      autostart_daemon (true)  a hook that finds the daemon down starts it
               budget_s (4.0)  hard wall-clock cap for one hook invocation
               http_timeout_s (3.0), head_chars (1000), tail_chars (500),
               snapshot_messages (5), snapshot_chars (500)
    jev        base_url, endpoint_path, api_key_env, model, timeout

Runtime state lives in ``~/.local/share/subcortex`` (``SUBCORTEX_DATA_DIR``
overrides; resolved at call time via ``data_dir()``).
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_PATH = Path.home() / ".config" / "subcortex" / "config.json"


DEFAULT_CONFIG: Dict[str, Any] = {
    "backend": "laya",
    "port": 7707,
    "model": "multilingual",
    "thresholds": {
        # null = the calibrated rule of the active backend (verdicts.py); a
        # number overrides that rule's primary threshold.
        "prompt_simple_confidence": None,
        "output_needed_threshold": None,
        "min_output_chars": 6000,
    },
    "features": {
        "prompt_hint": True,
        "trim_output": True,
        "compaction_snapshot": True,
    },
    "hooks": {
        "autostart_daemon": True,
        "budget_s": 4.0,
        "http_timeout_s": 3.0,
        "head_chars": 1000,
        "tail_chars": 500,
        "snapshot_messages": 5,
        "snapshot_chars": 500,
    },
    "jev": {
        "base_url": "https://api.typesafe.ai",
        "endpoint_path": "/v1/systemone",
        "api_key_env": "TYPESAFE_API_KEY",
        "model": "jev-latest",
        "timeout": 2.5,
    },
}

# env var -> (section or None, key, coerce)
_ENV_OVERRIDES = {
    "SUBCORTEX_BACKEND": (None, "backend", str),
    "SUBCORTEX_PORT": (None, "port", int),
    "SUBCORTEX_MODEL": (None, "model", str),
    "SUBCORTEX_JEV_BASE_URL": ("jev", "base_url", str),
    "SUBCORTEX_JEV_ENDPOINT_PATH": ("jev", "endpoint_path", str),
    "SUBCORTEX_JEV_API_KEY_ENV": ("jev", "api_key_env", str),
    "SUBCORTEX_JEV_MODEL": ("jev", "model", str),
    "SUBCORTEX_JEV_TIMEOUT": ("jev", "timeout", float),
    "SUBCORTEX_HOOK_BUDGET": ("hooks", "budget_s", float),
    "SUBCORTEX_AUTOSTART": ("hooks", "autostart_daemon", "bool"),
}


def _bool(raw: str) -> bool:
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def data_dir() -> Path:
    """Runtime state dir, resolved per call so tests can redirect HOME."""
    override = os.environ.get("SUBCORTEX_DATA_DIR", "").strip()
    return Path(override) if override else Path.home() / ".local" / "share" / "subcortex"


def venv_dir() -> Path:
    """The dedicated backend environment (laya), under the data dir."""
    return data_dir() / "venv"


def venv_python() -> Path:
    return venv_dir() / "bin" / "python"


def venv_pip() -> Path:
    return venv_dir() / "bin" / "pip"


def lock_path() -> Path:
    return data_dir() / "daemon.lock"


def pid_path() -> Path:
    return data_dir() / "daemon.pid"


def log_path() -> Path:
    return data_dir() / "daemon.log"


def config_path() -> Path:
    return Path(os.environ.get("SUBCORTEX_CONFIG", "") or CONFIG_PATH)


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load config: defaults < config.json < SUBCORTEX_* env. Never raises."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    file_path = Path(path) if path else config_path()
    try:
        if file_path.is_file():
            data = json.loads(file_path.read_text())
            if isinstance(data, dict):
                _merge(cfg, data)
    except (OSError, ValueError):
        pass  # corrupt/unreadable config degrades to defaults
    for env_name, (section, key, coerce) in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        try:
            value = _bool(raw) if coerce == "bool" else coerce(raw)
        except (TypeError, ValueError):
            continue
        target = cfg[section] if section else cfg
        target[key] = value
    return cfg


def unset_config(dotted: str) -> bool:
    """Remove one key (``section.key``) from config.json, reverting it to its default."""
    path = config_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return False
    if parts[-1] not in node:
        return False
    del node[parts[-1]]
    _atomic_write(path, json.dumps(data, indent=2) + "\n")
    return True


def save_config(updates: Dict[str, Any]) -> Path:
    """Merge *updates* into config.json (preserving other keys) and return the path."""
    path = config_path()
    data: Dict[str, Any] = {}
    try:
        if path.is_file():
            existing = json.loads(path.read_text())
            if isinstance(existing, dict):
                data = existing
    except (OSError, ValueError):
        pass
    _merge(data, updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(data, indent=2) + "\n")
    return path


def _atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    """Hooks read config.json concurrently: a torn file would silently mean
    defaults (re-enabling what the user switched off), so replace it whole."""
    import tempfile  # only writers pay for it; hooks only read

    if path.is_symlink():  # dotfiles: change the file, keep the link
        path = Path(os.path.realpath(path))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# -- secrets ---------------------------------------------------------------------------------
#
# API keys may live in the environment (preferred) or, for daemons started
# outside the user's shell (by a TUI hook or at login), in secrets.json next to
# the config file — created with mode 0600 and never printed.


def secrets_path() -> Path:
    return config_path().parent / "secrets.json"


def _read_secrets() -> Dict[str, str]:
    try:
        data = json.loads(secrets_path().read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def read_secret(name: str) -> Optional[str]:
    """``$name`` if set, else the value stored in secrets.json, else None."""
    value = os.environ.get(name, "").strip()
    return value or (_read_secrets().get(name) or "").strip() or None


def _write_secrets(data: Dict[str, str]) -> Path:
    path = secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(data, indent=2) + "\n", mode=0o600)
    return path


def save_secret(name: str, value: str) -> Path:
    data = _read_secrets()
    data[name] = value
    return _write_secrets(data)


def delete_secret(name: str) -> bool:
    data = _read_secrets()
    if name not in data:
        return False
    del data[name]
    if data:
        _write_secrets(data)
    else:
        secrets_path().unlink()
    return True
