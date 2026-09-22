"""A stand-in for Augment's backend (the "tenant URL"), for Auggie e2e runs.

Auggie has no bring-your-own-model mode: every model turn is a POST to
``<tenantURL>/chat-stream``, answered with newline-delimited JSON chunks
(``{"text": ..., "nodes": [...], "stop_reason": ...}``; a tool call is a node
of type 5 carrying ``tool_use``). ``AUGMENT_SESSION_AUTH`` can name any tenant
URL, so a test points it here — no login, no network. Replies are scripted
like MockLLM's: ``{"text": ...}`` or ``{"tool": name, "input": {...}}``; when
the script is empty it says "OK". Every request body is recorded.
"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

TOOL_USE_NODE = 5

# Minimal valid answers for the calls Auggie makes at startup.
CANNED: Dict[str, Any] = {
    "get-models": {"default_model": "mock-model", "models": [], "languages": [], "feature_flags": {}},
    "agents/list-remote-tools": {"tools": []},
    "settings/get-mcp-tenant-configs": {},
    "settings/get-mcp-user-configs": {},
}


class MockAugment:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.script: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/"

    def session_auth(self) -> str:
        """Value for AUGMENT_SESSION_AUTH pointing Auggie at this server."""
        return json.dumps({"accessToken": "e2e-local-only", "tenantURL": self.url, "scopes": ["read", "write"]})

    def __enter__(self) -> "MockAugment":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def chat_requests(self) -> List[Dict[str, Any]]:
        return [r["body"] for r in self.requests if r["path"].strip("/") == "chat-stream"]

    def all_text(self) -> str:
        return "\n".join(json.dumps(r) for r in self.requests)

    def next_turn(self) -> Dict[str, Any]:
        with self.lock:
            return self.script.pop(0) if self.script else {"text": "OK"}

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, data: bytes, ctype: str = "application/json") -> None:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                path = self.path.split("?")[0].strip("/")
                with mock.lock:
                    mock.requests.append({"path": path, "body": body})
                if path != "chat-stream":
                    self._send(json.dumps(CANNED.get(path, {})).encode())
                    return
                turn = mock.next_turn()
                if "tool" in turn:
                    node = {"id": 1, "type": TOOL_USE_NODE, "content": "",
                            "tool_use": {"tool_use_id": "toolu_" + uuid.uuid4().hex[:16], "tool_name": turn["tool"],
                                         "input_json": json.dumps(turn["input"])}}
                    chunks = [{"text": "", "nodes": [node]}, {"text": "", "stop_reason": 3}]
                else:
                    chunks = [{"text": turn["text"]}, {"text": "", "stop_reason": 1}]
                self._send("".join(json.dumps(c) + "\n" for c in chunks).encode(), "application/x-ndjson")

            do_GET = do_POST

        return Handler
