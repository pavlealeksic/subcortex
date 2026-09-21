"""High-level System-1 verdicts built on typed-decision backends.

Both functions take an optional ``backend`` (defaults to the configured one),
and NEVER raise — they return ``None`` on any failure so callers fail open.

Questions and thresholds are calibrated per backend against the labeled
examples in ``subcortex.evalset`` (``subcortex eval`` reruns it; numbers in
docs/calibration.md). The same question gets very different probability scales
from Jev and from the local Laya model, so each backend has its own rule. A
rule is a primary signal plus a veto: every condition must hold. Each question
is atomic, names the state fields it reads (the model never sees question ids),
and the evidence sits first in the state (Laya truncates the end of it).

What leaves the machine for a hosted backend is bounded and redacted: the
request, the tool call and a head+tail excerpt of the output, with anything
that looks like a secret masked.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from . import metrics
from .backends import get_backend
from .backends.base import DecisionBackend
from .config import load_config


def _noul(instructions: str) -> Dict[str, Any]:
    return {"type": "noul", "instructions": instructions}


PROMPT_QUESTIONS: Dict[str, Any] = {
    "quick": _noul("The request in `prompt` can be fully handled with one fact, one command, "
                   "or an edit of a few lines."),
    "multi_step": _noul("The request in `prompt` needs several dependent steps of work or reasoning."),
    "multi_file": _noul("The request in `prompt` asks for code changes in more than one file."),
}

OUTPUT_QUESTIONS: Dict[str, Any] = {
    "routine": _noul("`output` is routine progress or log output, such as downloads, compilation "
                     "steps, or install messages, with nothing specific to the request in `task`."),
    # Compound on paper, but it is the best-separating veto on both backends.
    "needed": _noul("Is the tool output in `output`, produced by the call in `tool_call`, still "
                    "needed to accomplish the user's request in `task` — i.e. would dropping it "
                    "lose information that cannot be cheaply re-derived by re-running the call?"),
    "depends": _noul("The request in `task` depends on information that appears in `output`."),
}

# (question, inverted?, threshold). Signals are oriented so that high means
# "simple" (prompt rules) or "needed" (output rules). A prompt is simple when
# every oriented signal is >= its threshold; an output is disposable when every
# oriented signal is < its threshold. The first condition is the primary one:
# it is reported, and the config threshold overrides it.
Rule = List[Tuple[str, bool, float]]
PROMPT_RULES: Dict[str, Rule] = {
    # evalset, jev-1.13.0: 16/20 simple prompts hinted, 0/20 complex.
    "jev": [("quick", False, 0.8), ("multi_step", True, 0.5)],
    # evalset, laya multilingual: 6/20 simple, 0/20 complex (weaker model, stricter rule).
    "laya": [("multi_step", True, 0.8), ("multi_file", True, 0.8)],
}
OUTPUT_RULES: Dict[str, Rule] = {
    # evalset, jev-1.13.0: 12/12 disposable outputs trimmed, 0/12 needed ones.
    "jev": [("routine", True, 0.4), ("needed", False, 0.3)],
    # evalset, laya multilingual: 3/12 disposable, 0/12 needed.
    "laya": [("depends", False, 0.9), ("needed", False, 0.75)],
}
DEFAULT_PROFILE = "laya"  # the conservative rule, for backends nobody calibrated

TASK_CHARS = 600
TOOL_CALL_CHARS = 300
OUTPUT_EXCERPT_CHARS = 1500
PROMPT_CHARS = 2000

# -- redaction ------------------------------------------------------------------------------
# Linear-time patterns only (no nested quantifiers): they run on every request.

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----.*?(?:-----END [A-Z ]{0,40}PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"),                   # OpenAI/Anthropic/Stripe-style
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),                   # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                               # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),                          # Google API key
    re.compile(r"\bapikey_[0-9a-f]{16,}_[0-9a-f]{16,}\b"),             # TypeSafe
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),  # JWT
]
_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]{0,40}(?:api[_-]?key|secret|token|passw(?:or)?d|pwd|auth|credential)[A-Z0-9_]{0,40})"
    r"([ \t]*[:=][ \t]*)([\"']?)[^\s\"']{4,}")
_URL_CREDENTIALS = re.compile(r"(\b[a-z][a-z0-9+.-]{1,20}://)[^/\s:@]{1,100}:[^/\s@]{1,200}@")


def redact(text: str) -> str:
    """Mask values that look like credentials; everything else is kept."""
    if not isinstance(text, str) or not text:
        return text
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    text = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[redacted]", text)
    return _URL_CREDENTIALS.sub(r"\1[redacted]@", text)


def excerpt(text: str, room: int = OUTPUT_EXCERPT_CHARS) -> str:
    """Head and tail of ``text`` within ``room`` characters (as calibrated)."""
    if len(text) <= room:
        return text
    return (text[:room * 2 // 3] + f"\n…[{len(text) - room} chars omitted]…\n"
            + text[-(room // 3):])


# -- rules ----------------------------------------------------------------------------------


def _profile(backend: Any) -> str:
    name = getattr(backend, "name", "")
    return name if name in PROMPT_RULES else DEFAULT_PROFILE


def _signals(result: Any, rule: Rule) -> Dict[str, float]:
    answers = result["answers"]
    raw = {}
    for question, _, _ in rule:
        value = float(answers[question]["noul"])
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{question}: not a probability")
        raw[question] = value
    return raw


def _oriented(raw: Dict[str, float], rule: Rule) -> List[float]:
    return [1.0 - raw[q] if inverted else raw[q] for q, inverted, _ in rule]


def _override(rule: Rule, value: Any) -> Rule:
    """The user's threshold replaces the primary condition's, when set."""
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        return rule
    if not 0.0 <= threshold <= 1.0:
        return rule
    question, inverted, _ = rule[0]
    return [(question, inverted, threshold)] + rule[1:]


def canned_answers(questions: Dict[str, Any], simple: bool = True,
                   disposable: bool = True) -> Dict[str, Any]:
    """What a perfectly sure model would answer: for self-tests and stubs."""
    yes = {"quick": simple, "multi_step": not simple, "multi_file": not simple,
           "routine": disposable, "needed": not disposable, "depends": not disposable}
    return {"answers": {q: {"type": "noul", "noul": (1.0 if yes[q] else 0.0) if q in yes else 0.5}
                        for q in questions}}


# -- verdicts -------------------------------------------------------------------------------


def classify_prompt(
    prompt: str,
    backend: Optional[DecisionBackend] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Classify a prompt as simple or complex.

    Returns ``{"label": "simple"|"complex", "confidence": p, "signals": {...}}``
    (``confidence`` is the primary signal oriented toward the label) or None.
    """
    try:
        cfg = config or load_config()
        be = backend or get_backend(cfg)
        rule = _override(PROMPT_RULES[_profile(be)],
                         (cfg.get("thresholds") or {}).get("prompt_simple_confidence"))
        questions = {q: PROMPT_QUESTIONS[q] for q, _, _ in rule}
        raw = _signals(be.predict({"prompt": redact(str(prompt))[:PROMPT_CHARS]}, questions), rule)
        oriented = _oriented(raw, rule)
        metrics.METRICS.record("verdict_prompt")
        simple = all(p >= threshold for p, (_, _, threshold) in zip(oriented, rule))
        primary = oriented[0]
        return {"label": "simple" if simple else "complex",
                "confidence": round(primary if simple else 1.0 - primary, 4),
                "signals": {q: round(v, 4) for q, v in raw.items()}}
    except Exception:
        return None


def judge_output(
    output: str,
    context: str = "",
    backend: Optional[DecisionBackend] = None,
    config: Optional[Dict[str, Any]] = None,
    task: str = "",
) -> Optional[Dict[str, Any]]:
    """Judge whether a tool output is still needed or can be trimmed.

    ``needed`` is False only when every condition of the backend's rule says
    so. Without the user's request there is no evidence to judge by (and no
    calibration), so the output is kept. Outputs shorter than
    ``min_output_chars`` are cheap to keep and are not judged.
    Returns ``{"needed": bool, "p_needed": float|None, "signals": {...}}`` or None.
    """
    try:
        cfg = config or load_config()
        min_chars = int(cfg["thresholds"]["min_output_chars"])
        if len(output) < min_chars:
            return {"needed": True, "p_needed": 1.0}
        if not task or not task.strip():
            return {"needed": True, "p_needed": None, "reason": "no request to judge against"}
        be = backend or get_backend(cfg)
        rule = _override(OUTPUT_RULES[_profile(be)],
                         (cfg.get("thresholds") or {}).get("output_needed_threshold"))
        questions = {q: OUTPUT_QUESTIONS[q] for q, _, _ in rule}
        state = {"task": redact(task.strip())[:TASK_CHARS],
                 "tool_call": redact(str(context))[:TOOL_CALL_CHARS],
                 "output": redact(excerpt(output))}
        raw = _signals(be.predict(state, questions), rule)
        oriented = _oriented(raw, rule)
        metrics.METRICS.record("verdict_output")
        disposable = all(p < threshold for p, (_, _, threshold) in zip(oriented, rule))
        return {"needed": not disposable, "p_needed": round(oriented[0], 4),
                "signals": {q: round(v, 4) for q, v in raw.items()}}
    except Exception:
        return None
