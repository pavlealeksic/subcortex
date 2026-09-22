"""A tiny fake OpenAI Responses API (``POST /v1/responses``, streaming SSE) for e2e runs.

Newer Codex CLI releases speak only the Responses API. This mock records
every request so tests can assert what Codex actually sent to the model, with
no network access or API cost. Replies are scripted like ``mock_llm.MockLLM``:
a queue of turns, each ``{"text": ...}`` or one function call
``{"tool": name, "input": {...}}``; when the queue is empty it says "OK".
"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List


class MockResponses:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.script: List[Dict[str, Any]] = []   # {"text": ...} or {"tool": name, "input": {...}}
        # Reported input tokens per reply; a turn may override it with {"input_tokens": N}
        # (a big number pushes Codex over model_auto_compact_token_limit).
        self.input_tokens = 10
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "MockResponses":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def next_turn(self, body: Dict[str, Any]) -> Dict[str, Any]:
        # Side requests (titles, summaries, compaction) carry no tools: they don't consume the script.
        with self.lock:
            if not body.get("tools") or not self.script:
                return {"text": "OK"}
            turn = self.script.pop(0)
        # A callable turn sees the request first (e.g. to pick the shell tool this version offers).
        return turn(body) if callable(turn) else turn

    def agent_requests(self) -> List[Dict[str, Any]]:
        return [r["body"] for r in self.requests if r["body"].get("tools")]

    def all_text(self) -> str:
        return "\n".join(json.dumps(r) for r in self.requests)

    # -- HTTP ------------------------------------------------------------------------

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _json(self, code: int, body: Any) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if self.path.split("?")[0].rstrip("/").endswith("/models"):
                    self._json(200, {"object": "list", "models": [],
                                     "data": [{"id": "mock-model", "object": "model"}]})
                else:
                    self._json(404, {"error": {"message": "not found"}})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {"_raw": raw[:2000].decode("utf-8", "replace"),
                            "_encoding": self.headers.get("Content-Encoding")}
                with mock.lock:
                    mock.requests.append({"path": self.path, "body": body})
                if self.path.split("?")[0].rstrip("/").endswith("/responses"):
                    self._responses(body)
                else:
                    self._json(404, {"error": {"message": f"{self.path} not mocked"}})

            def _responses(self, body: Dict[str, Any]) -> None:
                turn = mock.next_turn(body)
                rid = "resp_" + uuid.uuid4().hex[:24]
                if "tool" in turn:
                    item = {"type": "function_call", "id": "fc_" + uuid.uuid4().hex[:24], "status": "completed",
                            "call_id": "call_" + uuid.uuid4().hex[:24], "name": turn["tool"],
                            "arguments": json.dumps(turn["input"])}
                else:
                    item = {"type": "message", "id": "msg_" + uuid.uuid4().hex[:24], "status": "completed",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": turn["text"], "annotations": []}]}
                tokens = int(turn.get("input_tokens", mock.input_tokens))
                usage = {"input_tokens": tokens, "input_tokens_details": {"cached_tokens": 0},
                         "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 0},
                         "total_tokens": tokens + 5}
                base = {"id": rid, "object": "response", "created_at": 0, "model": body.get("model", "mock-model")}
                events: List[Dict[str, Any]] = [
                    {"type": "response.created", "response": dict(base, status="in_progress", output=[])},
                    {"type": "response.output_item.added", "output_index": 0,
                     "item": dict(item, status="in_progress")},
                ]
                if item["type"] == "message":
                    events.append({"type": "response.output_text.delta", "item_id": item["id"],
                                   "output_index": 0, "content_index": 0, "delta": turn["text"]})
                events += [
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed",
                     "response": dict(base, status="completed", output=[item], usage=usage)},
                ]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for seq, event in enumerate(events):
                    event["sequence_number"] = seq
                    self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()
                self.close_connection = True

        return Handler
