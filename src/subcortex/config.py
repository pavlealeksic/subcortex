"""Configuration for subcortex.

Config file: ``~/.config/subcortex/config.json`` (override the path with
``SUBCORTEX_CONFIG``). Missing file = defaults. ``SUBCORTEX_*`` env vars
override individual keys.

Keys::

    backend    "laya" | "jev"            (default "laya")
    port       daemon port               (default 7707)
    model      laya model alias          (default "multilingual")
    thresholds prompt_simple_confidence  (0.8)
               output_needed_threshold   (0.3)
               min_output_chars          (6000)
    jev        base_url, endpoint_path, api_key_env, model, timeout
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_PATH = Path.home() / ".config" / "subcortex" / "config.json"

# Runtime state: dedicated backend venv (created by a later installer), daemon
# lock/PID/log.
DATA_DIR = Path.home() / ".local" / "share" / "subcortex"
VENV_PYTHON = DATA_DIR / "venv" / "bin" / "python"
VENV_PIP = DATA_DIR / "venv" / "bin" / "pip"
LOCK_PATH = DATA_DIR / "daemon.lock"
PID_PATH = DATA_DIR / "daemon.pid"
LOG_PATH = DATA_DIR / "daemon.log"

DEFAULT_CONFIG: Dict[str, Any] = {
    "backend": "laya",
    "port": 7707,
    "model": "multilingual",
    "thresholds": {
        "prompt_simple_confidence": 0.8,
        "output_needed_threshold": 0.3,
        "min_output_chars": 6000,
    },
    "jev": {
        "base_url": "https://api.typesafe.ai/v1",
        "endpoint_path": "/systemone",
        "api_key_env": "TYPESAFE_API_KEY",
        "model": "jev-latest",
        "timeout": 30,
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
}


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
            value = coerce(raw)
        except (TypeError, ValueError):
            continue
        target = cfg[section] if section else cfg
        target[key] = value
    return cfg
