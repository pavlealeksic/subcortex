"""High-level System-1 verdicts built on typed-decision backends.

Both functions take an optional ``backend`` (defaults to the configured one),
honor config thresholds, and NEVER raise — they return ``None`` on any failure
so callers can fail open.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import metrics
from .backends import get_backend
from .backends.base import DecisionBackend
from .config import load_config

PROMPT_QUESTIONS: Dict[str, Any] = {
    "simple": {
        "type": "noul",
        "instructions": (
            "Is `prompt` a simple request: a lookup, one-liner, or short answer "
            "with no multi-step reasoning, no code changes across files, and no "
            "specialist knowledge?"
        ),
    },
}

OUTPUT_QUESTIONS: Dict[str, Any] = {
    "needed": {
        "type": "noul",
        "instructions": (
            "Is the tool output in `output` still needed to continue the task in "
            "`context` — i.e. would dropping it lose information that cannot be "
            "cheaply re-derived?"
        ),
    },
}


def classify_prompt(
    prompt: str,
    backend: Optional[DecisionBackend] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Classify a prompt as simple or complex.

    Returns ``{"label": "simple"|"complex", "confidence": float}`` or ``None``.
    """
    try:
        cfg = config or load_config()
        be = backend or get_backend(cfg)
        result = be.predict({"prompt": prompt}, PROMPT_QUESTIONS)
        p_simple = float(result["answers"]["simple"]["noul"])
        threshold = float(cfg["thresholds"]["prompt_simple_confidence"])
        metrics.METRICS.record("verdict_prompt")
        if p_simple >= threshold:
            return {"label": "simple", "confidence": round(p_simple, 4)}
        return {"label": "complex", "confidence": round(1.0 - p_simple, 4)}
    except Exception:
        return None


def judge_output(
    output: str,
    context: str = "",
    backend: Optional[DecisionBackend] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Judge whether a tool output is still needed or can be dropped.

    Outputs shorter than ``min_output_chars`` are cheap to keep and are not
    judged. Returns ``{"needed": bool, "p_needed": float}`` or ``None``.
    """
    try:
        cfg = config or load_config()
        min_chars = int(cfg["thresholds"]["min_output_chars"])
        if len(output) < min_chars:
            return {"needed": True, "p_needed": 1.0}
        be = backend or get_backend(cfg)
        result = be.predict({"output": output, "context": context}, OUTPUT_QUESTIONS)
        p_needed = float(result["answers"]["needed"]["noul"])
        threshold = float(cfg["thresholds"]["output_needed_threshold"])
        metrics.METRICS.record("verdict_output")
        return {"needed": p_needed >= threshold, "p_needed": round(p_needed, 4)}
    except Exception:
        return None
