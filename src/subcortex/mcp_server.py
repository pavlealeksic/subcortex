"""Minimal stdio MCP server exposing subcortex decisions as tools.

This is the generic seam for any MCP-capable TUI (Gemini CLI, Crush, Amp, Cursor…)
where we don't ship a native hook adapter: the agent can call these tools on demand.
Automatic interception (prompt classify, output filtering) lives in the hook
adapters — MCP tools are model-invoked, so they serve on-demand decisions only.

Zero dependencies: MCP speaks JSON-RPC over stdio with Content-Length framing.
"""

from __future__ import annotations

import json
import logging
import sys
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2025-06-18"
_SERVER_INFO = {"name": "subcortex", "version": "0.1.0"}

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
    from . import config

    cfg = config.load()
    return f"http://127.0.0.1:{cfg.get('port', 7707)}"


def _post(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    req = urllib.request.Request(
        _daemon_url() + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


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
        return result({
            "protocolVersion": _PROTOCOL_VERSION,
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


def _read_message(stream) -> Optional[Dict[str, Any]]:
    """Read one Content-Length framed JSON-RPC message; None on clean EOF."""
    headers = {}
    while True:
        line = stream.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        key, _, value = line.decode("ascii", "replace").partition(":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = stream.buffer.read(length)
    return json.loads(body.decode("utf-8"))


def _write_message(stream, message: Dict[str, Any]) -> None:
    body = json.dumps(message).encode("utf-8")
    stream.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    stream.buffer.flush()


def serve() -> None:
    """Run the stdio MCP server loop until EOF."""
    while True:
        try:
            request = _read_message(sys.stdin)
        except Exception as exc:
            logger.debug("mcp: bad message: %s", exc)
            continue
        if request is None:
            return
        try:
            response = _handle(request)
        except Exception as exc:  # never die on one bad request
            logger.debug("mcp: handler error: %s", exc)
            if request.get("id") is not None:
                response = {"jsonrpc": "2.0", "id": request["id"],
                            "error": {"code": -32603, "message": str(exc)}}
            else:
                continue
        if response is not None:
            _write_message(sys.stdout, response)
