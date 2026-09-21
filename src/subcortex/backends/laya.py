"""Laya backend: local typed-decision models.

Ported from laya-hermes (hermes_laya/backend.py), trimmed to the two backends
subcortex supports:

- ``mlx``   — ``pip install laya-mlx`` (Apple Silicon, macOS 14+)
- ``torch`` — ``pip install laya``     (upstream PyTorch, CUDA/CPU; everywhere else)

Model aliases: ``english`` (421M, 512 tok), ``multilingual`` (322M, 100+
languages, 1024 tok — default), ``typed-decisions`` (421M fine-tuned for
typed-decision workflows, 1024 tok).

Agents load lazily on first ``predict`` and are cached per (backend, alias).
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import platform
import sys
import threading
from typing import Any, Dict, Tuple

from ..config import VENV_PIP

_BACKENDS = ("mlx", "torch")
_MODULES = {"mlx": "laya_mlx", "torch": "laya"}
_PACKAGES = {"mlx": "laya-mlx", "torch": "laya"}

# alias -> per-backend Hugging Face checkpoint (torch uses repo + optional subfolder)
_CHECKPOINTS: Dict[str, Dict[str, Any]] = {
    "english": {
        "mlx": "aac6fef/laya-mlx",
        "torch": ("convaiinnovations/laya", None),
    },
    "multilingual": {
        "mlx": "aac6fef/laya-multilingual-mlx",
        "torch": ("convaiinnovations/laya", "multilingual"),
    },
    "typed-decisions": {
        "mlx": "aac6fef/laya-typed-decisions-mlx",
        "torch": ("convaiinnovations/laya", "typed-decisions"),
    },
}

DEFAULT_MODEL = "multilingual"


class BackendUnavailableError(RuntimeError):
    """Raised when the laya backend package is not installed/importable."""


def _is_apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def detect_backend() -> str:
    """mlx on Apple Silicon, torch everywhere else."""
    return "mlx" if _is_apple_silicon() else "torch"


def _module_available(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _install_hint(package: str) -> str:
    if VENV_PIP.exists():
        return f"run: {VENV_PIP} install {package}"
    return f"run: {sys.executable} -m pip install {package}"


class LayaBackend:
    name = "laya"

    def __init__(self, config: Dict[str, Any]) -> None:
        self.model = str(config.get("model") or DEFAULT_MODEL).strip().lower()
        self._agents: Dict[Tuple[str, str], Any] = {}
        self._lock = threading.Lock()

    # -- availability ---------------------------------------------------------

    def available(self) -> Tuple[bool, str]:
        backend = detect_backend()
        module = _MODULES[backend]
        if _module_available(module):
            return True, f"laya backend {backend!r} available ({module}, model {self.model!r})"
        package = _PACKAGES[backend]
        return False, (
            f"{module} is not installed for {sys.executable}. "
            f"{_install_hint(package)}"
        )

    # -- lazy loading ---------------------------------------------------------

    def _checkpoint(self, alias: str, backend: str) -> Any:
        if alias not in _CHECKPOINTS:
            raise ValueError(
                f"Unknown laya model alias {alias!r}; valid: {', '.join(sorted(_CHECKPOINTS))}"
            )
        return _CHECKPOINTS[alias][backend]

    def _load(self, alias: str) -> Any:
        backend = detect_backend()
        key = (backend, alias)
        with self._lock:
            if key in self._agents:
                return self._agents[key]
            module = _MODULES[backend]
            if not _module_available(module):
                raise BackendUnavailableError(
                    f"Laya backend {backend!r} is not available. "
                    f"{_install_hint(_PACKAGES[backend])}"
                )
            try:
                if backend == "torch":
                    # Upstream laya deadlocks on import when TensorFlow is also installed.
                    os.environ.setdefault("USE_TF", "0")
                mod = importlib.import_module(module)
            except ImportError as exc:
                raise BackendUnavailableError(
                    f"Laya backend {backend!r} failed to import ({exc}). "
                    f"{_install_hint(_PACKAGES[backend])}"
                ) from exc
            ref = self._checkpoint(alias, backend)
            if backend == "mlx":
                agent = mod.load(ref)
            else:  # torch
                repo, subfolder = ref
                agent = mod.load(repo, subfolder=subfolder) if subfolder else mod.load(repo)
            self._agents[key] = agent
            return agent

    # -- inference ------------------------------------------------------------

    def predict(self, state: Any, questions: Dict[str, Any]) -> Dict[str, Any]:
        agent = self._load(self.model)
        result = agent.predict(state, questions)
        if not isinstance(result, dict):
            result = {"answers": result}
        return result
