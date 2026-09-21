"""Backend factory."""

from __future__ import annotations

from typing import Any, Dict, Optional

from .base import DecisionBackend

BACKENDS = ("laya", "jev")


def get_backend(config: Dict[str, Any], override: Optional[str] = None) -> DecisionBackend:
    """Instantiate the configured backend (models/connections load lazily)."""
    name = str(override or config.get("backend") or "laya").strip().lower()
    if name == "laya":
        from .laya import LayaBackend

        return LayaBackend(config)
    if name == "jev":
        from .jev import JevBackend

        return JevBackend(config)
    raise ValueError(f"Unknown backend {name!r}; valid: {', '.join(BACKENDS)}")
