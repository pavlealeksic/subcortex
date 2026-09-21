"""Jev backend: hosted typed-decision API client.

Pure stdlib ``urllib`` — zero dependencies. Ported from jev-hermes
(jev_hermes/client.py). The wire shape: ``POST {base_url}{endpoint_path}`` with
JSON ``{"model", "state", "questions"}`` returns ``{"answers": {...}}``.
Reference deployment: TypeSafe (``https://api.typesafe.ai/v1``, ``/systemone``,
key in ``TYPESAFE_API_KEY``, model ``jev-latest``). OpenRouter speaks the same
shape (``https://openrouter.ai``, ``/api/alpha/decisions``,
``OPENROUTER_API_KEY``, model ``typesafe/jev-1.13``).

Fail-closed URL policy: https anywhere, cleartext http only for
loopback/private LAN addresses, no userinfo, no redirects (a redirect would
carry the bearer key somewhere unvetted), response capped at 1 MB, endpoint
path restricted to ``[A-Za-z0-9/._-~]+``. No retries.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import ssl
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Tuple
from urllib.parse import urlsplit, urlunsplit

from ..config import read_secret

Transport = Callable[[str, bytes, Dict[str, str], float], Tuple[int, str]]

DEFAULT_BASE_URL = "https://api.typesafe.ai/v1"
DEFAULT_ENDPOINT_PATH = "/systemone"
DEFAULT_API_KEY_ENV = "TYPESAFE_API_KEY"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT = 30.0

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
    """base_url + endpoint path without mangling query/fragment."""
    try:
        parts = urlsplit(base_url)
    except ValueError:
        raise BackendUnavailableError("invalid jev base url") from None
    if parts.fragment:
        raise BackendUnavailableError("refusing jev base url with fragment")
    path = parts.path.rstrip("/") + _normalize_endpoint_path(endpoint_path)
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib follows 3xx automatically — a redirect would bypass _check_url.

    Raise instead: decisions endpoints answer POST in place; a redirect means
    the base_url is wrong, not that we should chase it with the bearer key.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BackendUnavailableError(
            f"refusing redirect to {urlsplit(newurl).scheme or '(none)'}://…"
        )


def _read_capped(resp: Any) -> str:
    raw: bytes = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise BackendUnavailableError(f"jev response over {MAX_RESPONSE_BYTES} byte cap")
    return raw.decode("utf-8", "replace")


def _default_transport(
    url: str, body: bytes, headers: Dict[str, str], timeout_s: float
) -> Tuple[int, str]:
    scheme, _ = _check_url(url)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    handlers = [_NoRedirect()]
    if scheme == "https":
        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout_s) as resp:
            return (int(resp.status), _read_capped(resp))
    except urllib.error.HTTPError as exc:
        try:
            text = _read_capped(exc)
        except Exception:
            text = ""
        return (int(exc.code or 0), text)


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


def _parse_response(status: int, text: str) -> Dict[str, Any]:
    if not 200 <= status < 300:
        # Upstream error bodies are attacker-influenced: report a category,
        # not the body — no echoed creds or control bytes in the log.
        raise BackendUnavailableError(f"jev request failed (http {status})")
    try:
        parsed = json.loads(text)
    except ValueError:
        raise BackendUnavailableError("jev returned malformed JSON") from None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("answers"), dict):
        raise BackendUnavailableError("jev response is missing answers")
    return parsed


def _validate_answers(answers: Dict[str, Any]) -> None:
    """Every noul probability must be a finite number in [0, 1]."""
    for name, answer in answers.items():
        if not isinstance(answer, dict) or "noul" not in answer:
            continue
        prob = answer["noul"]
        if isinstance(prob, bool) or not isinstance(prob, (int, float)) or not math.isfinite(prob):
            raise BackendUnavailableError(f"invalid jev answer for {name}")
        if not 0.0 <= float(prob) <= 1.0:
            raise BackendUnavailableError(f"invalid jev answer for {name}: not a probability")


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
        key = read_secret(self.api_key_env)
        if not key:
            raise BackendUnavailableError(
                f"jev API key not configured ({self.api_key_env} is not set). "
                f"Fix: export {self.api_key_env}=... or run: subcortex setup"
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
        _validate_answers(result["answers"])
        return result
