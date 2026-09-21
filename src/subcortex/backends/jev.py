"""Jev backend: hosted typed-decision API client (TypeSafe System One, v1).

Pure stdlib (``http.client`` with kept-alive connections), zero dependencies. Wire shape, as specified by the
TypeSafe OpenAPI schema and ``typesafe-sdk``: ``POST {base}/v1/systemone`` with
``{"model", "state", "questions"}`` returns ``{"model", "answers", "usage"}``;
every answer carries its ``type``, and ``usage.input_tokens`` is what is billed.

Endpoints that speak it: TypeSafe (``https://api.typesafe.ai``, key in
``TYPESAFE_API_KEY``, model ``jev-latest``), OpenRouter (``https://openrouter.ai/api``,
``OPENROUTER_API_KEY``) and the Vercel AI Gateway
(``https://ai-gateway.vercel.sh/typesafe``, model ``typesafe-ai/jev``). A base
URL is used as pasted from their docs; older ``.../v1`` + ``/systemone`` configs
keep working.

Fail-closed URL policy: https anywhere, cleartext http only for
loopback/private LAN addresses, no userinfo, no redirects (a redirect would
carry the bearer key somewhere unvetted), response capped at 1 MB, endpoint
path restricted to ``[A-Za-z0-9/._-~]+``. No retries: a decision that isn't
back within the hook budget is worthless, so it fails open instead.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import math
import re
import ssl
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from .. import __version__
from ..config import read_secret
from ..metrics import METRICS

Transport = Callable[[str, bytes, Dict[str, str], float], Tuple[int, str]]

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_ENDPOINT_PATH = "/v1/systemone"
DEFAULT_API_KEY_ENV = "TYPESAFE_API_KEY"
DEFAULT_MODEL = "jev-latest"
# Typical decisions take ~100 ms; hooks give up after hooks.http_timeout_s (3 s).
DEFAULT_TIMEOUT = 2.5
# USD per input token (output tokens are free), per docs.typesafe.ai/models.
PRICE_PER_INPUT_TOKEN = 0.042e-6

# Answers are small probability maps (~hundreds of bytes/question). Anything
# past this is a broken/compromised endpoint — fail closed.
MAX_RESPONSE_BYTES = 1_000_000


class BackendUnavailableError(RuntimeError):
    """The Jev API is unreachable, misconfigured, or answered garbage."""


# -- URL policy ----------------------------------------------------------------


def _is_loopback_host(host: str) -> bool:
    # localhost is the one permitted DNS alias; canonical v4/v6 loopbacks match
    # via is_loopback — but NOT v4-mapped ::ffff:127.x.
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return False
    return ip.is_loopback


# Cleartext http is allowed exactly for loopback + these non-routed ranges:
# RFC1918 LANs, Tailscale CGNAT (100.64/10 — NOT covered by is_private), ULA +
# link-local IPv6. Checked against explicit networks, never is_private — that
# flag also matches 169.254/16 link-local, i.e. the cloud metadata endpoint.
_CLEAR_NETWORKS_V4 = tuple(
    ipaddress.ip_network(c)
    for c in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "127.0.0.0/8")
)
_CLEAR_NETWORKS_V6 = tuple(
    ipaddress.ip_network(c) for c in ("::1/128", "fc00::/7", "fe80::/10", "fec0::/10")
)


def _allows_cleartext(host: str) -> bool:
    if _is_loopback_host(host):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # DNS names must use https — no resolution at request time
    nets = _CLEAR_NETWORKS_V6 if ip.version == 6 else _CLEAR_NETWORKS_V4
    return any(ip in net for net in nets)


def _check_url(url: str) -> Tuple[str, str]:
    """Fail-closed URL policy: https anywhere, http LAN-only, no userinfo."""
    try:
        parts = urlsplit(url)
    except ValueError:
        raise BackendUnavailableError("invalid jev url") from None
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    if scheme not in {"http", "https"}:
        raise BackendUnavailableError(f"refusing non-http jev url: {scheme or '(none)'}")
    # "@" anywhere in netloc means userinfo was present — even an empty
    # username risks parser/request-library disagreement downstream.
    if "@" in parts.netloc:
        raise BackendUnavailableError("refusing jev url with embedded credentials")
    if scheme == "http" and not _allows_cleartext(host):
        raise BackendUnavailableError(
            f"refusing cleartext jev url for non-LAN host: {host or '(none)'}"
        )
    return scheme, host


# Conservative endpoint-path charset: percent-escapes like %2f/%3f and unicode
# look-alikes must not reach the URL — a proxy/router could reinterpret them as
# delimiters. Real decisions paths (/systemone, /api/alpha/decisions) are all
# in this set.
_ENDPOINT_PATH_CHARS = re.compile(r"[A-Za-z0-9/._\-~]+")


def _normalize_endpoint_path(endpoint_path: str) -> str:
    """Validate a configured endpoint path (fail closed to default).

    The path is operator config, not remote input — but it flows into the
    request URL, so keep it to a plain absolute path of unreserved chars. No
    scheme, host, query, fragment, credentials, percent-escapes, or non-ASCII
    bytes. Anything else degrades to the default path.
    """
    candidate = (endpoint_path or "").strip()
    if not candidate.startswith("/") or " " in candidate:
        return DEFAULT_ENDPOINT_PATH
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return DEFAULT_ENDPOINT_PATH
    if parts.scheme or parts.netloc or parts.query or parts.fragment or "@" in candidate:
        return DEFAULT_ENDPOINT_PATH
    # Match against the raw candidate, not the parsed path: urlsplit strips
    # ASCII newlines/tabs before parsing, so validating parts.path would let
    # "/x\ny" through as "/xy" — a control byte that must fail closed.
    if "%" in candidate or _ENDPOINT_PATH_CHARS.fullmatch(candidate) is None:
        return DEFAULT_ENDPOINT_PATH
    return candidate.rstrip("/") or DEFAULT_ENDPOINT_PATH


def _join_url(base_url: str, endpoint_path: str = DEFAULT_ENDPOINT_PATH) -> str:
    """base_url + endpoint path without mangling query/fragment.

    Official docs give a bare base (``https://api.typesafe.ai``,
    ``https://openrouter.ai/api``) and the path ``/v1/systemone``; subcortex
    <= 0.2 saved ``https://api.typesafe.ai/v1`` + ``/systemone``. Both, and any
    mix of the two, resolve to one ``/v1/systemone``.
    """
    try:
        parts = urlsplit(base_url)
    except ValueError:
        raise BackendUnavailableError("invalid jev base url") from None
    if parts.fragment:
        raise BackendUnavailableError("refusing jev base url with fragment")
    base = parts.path.rstrip("/")
    endpoint = _normalize_endpoint_path(endpoint_path)
    if base.endswith("/v1") and endpoint.startswith("/v1/"):
        endpoint = endpoint[3:]
    elif endpoint == "/systemone" and not base.endswith("/v1"):
        endpoint = "/v1/systemone"
    return urlunsplit((parts.scheme, parts.netloc, base + endpoint, parts.query, ""))


def _read_capped(resp: Any) -> str:
    raw: bytes = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise BackendUnavailableError(f"jev response over {MAX_RESPONSE_BYTES} byte cap")
    return raw.decode("utf-8", "replace")


# -- keep-alive transport ----------------------------------------------------------
#
# A fresh HTTPS connection costs three round trips (TCP, TLS, request); a reused
# one costs one. Measured to api.typesafe.ai at ~225 ms RTT: ~650 ms per decision
# fresh, ~250 ms reused. So the daemon keeps a few idle connections per origin.
# http.client never follows redirects (a 3xx fails closed in _parse_response,
# so the bearer key is never re-sent elsewhere).

_IDLE_MAX_AGE_S = 30.0     # servers drop idle keep-alive connections; retire ours first
_IDLE_PER_ORIGIN = 4
_idle: Dict[Tuple[str, str, int], list] = {}
_idle_lock = threading.Lock()


def _proxy_for(scheme: str, host: str) -> Optional[Tuple[str, int]]:
    """The HTTPS proxy to tunnel through (HTTPS_PROXY / system settings, honoring
    NO_PROXY), so corporate networks can still reach the hosted API."""
    import urllib.request

    if _is_loopback_host(host) or urllib.request.proxy_bypass(host):
        return None
    proxy = urllib.request.getproxies().get(scheme)
    if not proxy:
        return None
    parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    if not parts.hostname:
        return None
    return parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def _connect(scheme: str, host: str, port: int, timeout_s: float) -> Any:
    proxy = _proxy_for(scheme, host)
    if scheme == "https":
        context = ssl.create_default_context()
        if proxy:
            conn = http.client.HTTPSConnection(proxy[0], proxy[1], timeout=timeout_s, context=context)
            conn.set_tunnel(host, port)
            return conn
        return http.client.HTTPSConnection(host, port, timeout=timeout_s, context=context)
    return http.client.HTTPConnection(host, port, timeout=timeout_s)  # LAN only (_check_url)


def _take(origin: Tuple[str, str, int]) -> Any:
    now = time.monotonic()
    with _idle_lock:
        pool = _idle.get(origin) or []
        while pool:
            conn, since = pool.pop()
            if now - since < _IDLE_MAX_AGE_S:
                return conn
            conn.close()
    return None


def _give_back(origin: Tuple[str, str, int], conn: Any) -> None:
    with _idle_lock:
        pool = _idle.setdefault(origin, [])
        if len(pool) < _IDLE_PER_ORIGIN:
            pool.append((conn, time.monotonic()))
            return
    conn.close()


def _default_transport(
    url: str, body: bytes, headers: Dict[str, str], timeout_s: float
) -> Tuple[int, str]:
    scheme, host = _check_url(url)
    parts = urlsplit(url)
    port = parts.port or (443 if scheme == "https" else 80)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    origin = (scheme, host, port)
    reused = _take(origin)
    for conn in ([reused] if reused else []) + [None]:
        fresh = conn is None
        if fresh:
            conn = _connect(scheme, host, port, timeout_s)
        else:
            conn.timeout = timeout_s
            if conn.sock is not None:
                conn.sock.settimeout(timeout_s)
        try:
            conn.request("POST", target, body=body, headers=headers)
            resp = conn.getresponse()
            text = _read_capped(resp)
        except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError,
                http.client.CannotSendRequest, http.client.BadStatusLine):
            conn.close()
            if fresh:
                raise
            continue  # the server had closed the idle connection; nothing was processed
        except BaseException:
            conn.close()
            raise
        if resp.will_close:
            conn.close()
        else:
            _give_back(origin, conn)
        return int(resp.status), text
    raise BackendUnavailableError("jev transport failed")


# Module-level so tests can stub the network with mock.patch.
_transport: Transport = _default_transport


# -- request / response shaping -------------------------------------------------


def _build_body(model: str, state: Any, questions: Dict[str, Any]) -> bytes:
    try:
        # allow_nan=False: NaN/Infinity are not valid JSON — a state carrying
        # them is corrupt input, not a request (strict endpoints 400 it).
        return json.dumps(
            {"model": model, "state": state, "questions": questions}, allow_nan=False
        ).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise BackendUnavailableError(
            f"jev request is not JSON-serializable: {type(exc).__name__}"
        ) from exc


# Status -> what the user should do. Upstream error bodies are
# attacker-influenced: report a category, never the body.
_STATUS_HINTS = {
    401: "API key rejected (401): check the key",
    403: "API key missing or not allowed (403): check the key",
    404: "endpoint not found (404): check jev.base_url",
    408: "request timed out upstream (408)",
    413: "request too large (413)",
    422: "request rejected as invalid (422)",
    429: "rate limited (429)",
    529: "Jev is overloaded (529)",
}


def _status_error(status: int) -> BackendUnavailableError:
    hint = _STATUS_HINTS.get(status) or (
        f"Jev server error ({status})" if status >= 500 else f"http {status}")
    return BackendUnavailableError(f"jev request failed: {hint}")


def _parse_response(status: int, text: str) -> Dict[str, Any]:
    if not 200 <= status < 300:
        raise _status_error(status)
    try:
        parsed = json.loads(text)
    except ValueError:
        raise BackendUnavailableError("jev returned malformed JSON") from None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("answers"), dict):
        raise BackendUnavailableError("jev response is missing answers")
    return parsed


def _probability(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and 0.0 <= float(value) <= 1.0)


def _validate_answers(answers: Dict[str, Any], questions: Dict[str, Any]) -> None:
    """Every question answered, with its own type and well-formed values.

    Answers to questions we didn't ask are ignored (the SDK does the same, for
    forward compatibility); a missing or mistyped answer fails the whole call.
    """
    for name, question in questions.items():
        answer = answers.get(name)
        qtype = question.get("type") if isinstance(question, dict) else None
        if not isinstance(answer, dict):
            raise BackendUnavailableError(f"jev did not answer {name}")
        # Laya-compatible servers omit the discriminator; when present it must match.
        if "type" in answer and answer["type"] != qtype:
            raise BackendUnavailableError(f"jev answered {name} with the wrong type")
        if qtype == "noul":
            if not _probability(answer.get("noul")):
                raise BackendUnavailableError(f"invalid jev answer for {name}: not a probability")
        elif qtype == "choice":
            criteria = question.get("criteria") or {}
            if answer.get("choice") not in criteria:
                raise BackendUnavailableError(f"invalid jev answer for {name}: unknown choice")
        elif qtype == "score":
            score = answer.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                raise BackendUnavailableError(f"invalid jev answer for {name}: not a score")


def _record_usage(result: Dict[str, Any]) -> None:
    """Billable tokens and cost, for ``subcortex stats`` (OpenRouter reports cost itself)."""
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return
    tokens = usage.get("input_tokens")
    if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
        METRICS.add("jev_input_tokens", tokens)
        cost = usage.get("cost")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
            cost = tokens * PRICE_PER_INPUT_TOKEN
        METRICS.add("jev_cost_usd", float(cost))
        from .. import ledger

        ledger.record("jev", tokens=tokens, usd=round(float(cost), 9))
    if isinstance(result.get("model"), str):
        METRICS.note("jev_model", result["model"][:64])


# -- backend --------------------------------------------------------------------


class JevBackend:
    name = "jev"

    def __init__(self, config: Dict[str, Any]) -> None:
        jev = config.get("jev") or {}
        self.base_url = str(jev.get("base_url") or DEFAULT_BASE_URL)
        self.endpoint_path = str(jev.get("endpoint_path") or DEFAULT_ENDPOINT_PATH)
        self.api_key_env = str(jev.get("api_key_env") or DEFAULT_API_KEY_ENV).strip()
        self.model = str(jev.get("model") or DEFAULT_MODEL).strip()
        try:
            timeout = float(jev.get("timeout", DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        self.timeout = timeout if math.isfinite(timeout) and timeout > 0 else DEFAULT_TIMEOUT

    # -- availability -----------------------------------------------------------

    def available(self) -> Tuple[bool, str]:
        try:
            url = _join_url(self.base_url, self.endpoint_path)
            _check_url(url)
        except BackendUnavailableError as exc:
            return False, f"jev misconfigured: {exc}"
        if not read_secret(self.api_key_env):
            return False, (
                f"jev API key not configured ({self.api_key_env} is not set). "
                f"Fix: export {self.api_key_env}=... or run: subcortex setup"
            )
        return True, f"jev configured ({url}, model {self.model!r})"

    # -- inference ----------------------------------------------------------------

    def _api_key(self) -> str:
        key = (read_secret(self.api_key_env) or "").strip()
        if not key:
            raise BackendUnavailableError(
                f"jev API key not configured ({self.api_key_env} is not set). "
                f"Fix: export {self.api_key_env}=... or run: subcortex setup"
            )
        # A pasted key with a stray newline or space would fail inside http.client
        # with an opaque error (or split the header): refuse it clearly.
        if not key.isascii() or not key.isprintable() or any(c.isspace() for c in key):
            raise BackendUnavailableError(
                f"jev API key in {self.api_key_env} contains spaces or control characters. "
                "Fix: paste it again with: subcortex setup"
            )
        return key

    def predict(self, state: Any, questions: Dict[str, Any]) -> Dict[str, Any]:
        """One typed-decision call. No retries — fail closed on any error."""
        url = _join_url(self.base_url, self.endpoint_path)
        _check_url(url)  # fail closed before the key is attached to anything
        body = _build_body(self.model, state, questions)
        headers = {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"subcortex/{__version__}",
        }
        try:
            status, text = _transport(url, body, headers, self.timeout)
        except BackendUnavailableError:
            raise
        except Exception as exc:
            # Transport exceptions can echo the request URL; never surface it raw.
            raise BackendUnavailableError(
                f"jev transport failed: {type(exc).__name__}"
            ) from exc
        result = _parse_response(status, text)
        _validate_answers(result["answers"], questions)
        _record_usage(result)
        return result
