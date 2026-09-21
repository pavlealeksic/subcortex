"""Backend protocol shared by all subcortex decision backends."""

from __future__ import annotations

from typing import Any, Dict, Protocol, Tuple


class DecisionBackend(Protocol):
    """A typed-decision backend.

    ``predict`` runs one typed-decision call and returns the raw result dict
    (with an ``"answers"`` key). ``available`` reports whether the backend can
    be used right now, with a human-readable reason/fix when not.
    """

    name: str

    def predict(self, state: Any, questions: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def available(self) -> Tuple[bool, str]:
        ...
