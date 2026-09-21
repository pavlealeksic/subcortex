"""Minimal stdio MCP server exposing subcortex decisions as tools.

This is the generic seam for any MCP-capable TUI (Gemini CLI, Crush, Amp, Cursor…)
where we don't ship a native hook adapter: the agent can call these tools on demand.
Automatic interception (prompt classify, output filtering) lives in the hook
adapters — MCP tools are model-invoked, so they serve on-demand decisions only.

Zero dependencies. Transport per the MCP spec's stdio transport: one JSON-RPC
message per line (UTF-8, newline-delimited, no embedded newlines) on
stdin/stdout; nothing but protocol messages is ever written to stdout.
"""

from __future__ import annotations

import json
import logging
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from . import __version__

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2025-06-18"
_SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
_SERVER_INFO = {"name": "subcortex", "version": __version__}

_TOOLS = [
    {
        "name": "subcortex_decide",
        "description": (
            "Fast typed decision (choice/score/boolean with calibrated probabilities) "
            "from the local subcortex service — routing, triage, gating, moderation. "
            "Milliseconds, no LLM tokens."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {"description": "The situation to judge (text or JSON)."},
                "questions": {
                    "type": "object",
                    "description": "Map of name -> {type: choice|score|noul, instructions, criteria?}",
                },
            },
            "required": ["state", "questions"],
        },
    },
    {
        "name": "subcortex_classify_prompt",
        "description": "Classify a user prompt as simple or complex (with confidence).",
        "inputSchema": {
            "type": "object",
            "properties": {"prompt": {"type": "string"}},
            "required": ["prompt"],
        },
    },
    {
        "name": "subcortex_judge_output",
        "description": "Judge whether a tool/command output is still needed or disposable.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "output": {"type": "string"},
                "context": {"type": "string", "description": "What produced the output."},
            },
            "required": ["output"],
        },
    },
]


def _daemon_url() -> str:
    from .config import load_config

    return f"http://127.0.0.1:{int(load_config()['port'])}"


def _ensure_daemon() -> None:
    """Start the daemon on first use if it isn't running (best effort)."""
    from . import cli
    from .config import load_config

    cfg = load_config()
    if cli._health(cfg) is None:
        cli._spawn_daemon(cfg)
        cli._wait_for_health(cfg)


def _post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    def once() -> Dict[str, Any]:
        req = urllib.request.Request(
            _daemon_url() + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    try:
        return once()
    except urllib.error.URLError:
        _ensure_daemon()
        return once()


def _call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if name == "subcortex_decide":
        return _post("/decide", {"state": arguments.get("state"),
                                 "questions": arguments.get("questions") or {}})
    if name == "subcortex_classify_prompt":
        return _post("/verdict/prompt", {"prompt": arguments.get("prompt", "")})
    if name == "subcortex_judge_output":
        return _post("/verdict/output", {"output": arguments.get("output", ""),
                                         "context": arguments.get("context", "")})
    raise ValueError(f"unknown tool {name!r}")


def _handle(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One JSON-RPC request -> response (None for notifications)."""
    method = request.get("method", "")
    req_id = request.get("id")

    def result(value: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": value}

    def error(code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        requested = (request.get("params") or {}).get("protocolVersion")
        return result({
            "protocolVersion": requested if requested in _SUPPORTED_VERSIONS else _PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": _SERVER_INFO,
        })
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return result({})
    if method == "tools/list":
        return result({"tools": _TOOLS})
    if method == "tools/call":
        params = request.get("params") or {}
        try:
            outcome = _call_tool(params.get("name", ""), params.get("arguments") or {})
            return result({"content": [{"type": "text", "text": json.dumps(outcome)}]})
        except Exception as exc:
            return result({"content": [{"type": "text", "text": f"subcortex error: {exc}"}],
                           "isError": True})
    if req_id is None:
        return None
    return error(-32601, f"method not found: {method}")


def _respond(message: Any) -> Optional[Any]:
    """Response for one parsed message (a request object or a legacy batch)."""
    if isinstance(message, list):
        replies = [r for r in (_respond(m) for m in message) if r is not None]
        return replies or None
    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    try:
        return _handle(message)
    except Exception as exc:  # never die on one bad request
        logger.debug("mcp: handler error: %s", exc)
        if message.get("id") is None:
            return None
        return {"jsonrpc": "2.0", "id": message["id"],
                "error": {"code": -32603, "message": str(exc)}}


def serve(stdin=None, stdout=None) -> None:
    """Run the stdio MCP server loop until EOF."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply: Any = {"jsonrpc": "2.0", "id": None,
                          "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = _respond(message)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
