"""A tiny fake LLM API (Anthropic Messages + OpenAI Chat Completions) for e2e runs.

Records every request body so tests can assert what a real TUI actually sent
to the model — e.g. that a hook's context reached the prompt — without any
network access or API cost. Replies are scripted: a queue of turns, each
either plain text or one tool call; when the queue is empty it says "OK".
"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


class MockLLM:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.script: List[Dict[str, Any]] = []   # {"text": ...} or {"tool": name, "input": {...}}
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "MockLLM":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def next_turn(self, body: Dict[str, Any]) -> Dict[str, Any]:
        # Side requests (titles, summaries) carry no tools: they don't consume the script.
        with self.lock:
            if not body.get("tools") or not self.script:
                return {"text": "OK"}
            return self.script.pop(0)

    def agent_requests(self) -> List[Dict[str, Any]]:
        return [r["body"] for r in self.requests if r["body"].get("tools")]

    def all_text(self) -> str:
        return "\n".join(json.dumps(r) for r in self.requests)

    # -- HTTP ------------------------------------------------------------------------

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _json(self, code: int, body: Any) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _sse(self, events: List[Any], anthropic: bool) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for event in events:
                    if anthropic:
                        chunk = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                    else:
                        chunk = f"data: {json.dumps(event) if event != '[DONE]' else '[DONE]'}\n\n"
                    self.wfile.write(chunk.encode())
                    self.wfile.flush()

            def do_GET(self) -> None:
                if self.path.rstrip("/").endswith("/models"):
                    self._json(200, {"data": [{"id": "mock-model", "object": "model", "type": "model",
                                               "display_name": "Mock"}], "object": "list"})
                else:
                    self._json(404, {"error": "not found"})

            def do_HEAD(self) -> None:
                self.send_response(200)
                self.end_headers()

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                with mock.lock:
                    mock.requests.append({"path": self.path, "body": body})
                if "/messages" in self.path and "count_tokens" in self.path:
                    self._json(200, {"input_tokens": 10})
                elif "/messages" in self.path:
                    self._anthropic(body)
                elif "/chat/completions" in self.path:
                    self._openai(body)
                elif "/responses" in self.path:
                    self._json(404, {"error": "responses API not mocked"})
                else:
                    self._json(404, {"error": "not found"})

            def _anthropic(self, body: Dict[str, Any]) -> None:
                turn = mock.next_turn(body)
                msg_id = "msg_" + uuid.uuid4().hex[:20]
                usage = {"input_tokens": 10, "output_tokens": 5}
                if "tool" in turn:
                    block = {"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20],
                             "name": turn["tool"], "input": turn["input"]}
                    stop = "tool_use"
                else:
                    block = {"type": "text", "text": turn["text"]}
                    stop = "end_turn"
                message = {"id": msg_id, "type": "message", "role": "assistant",
                           "model": body.get("model", "mock"), "content": [block],
                           "stop_reason": stop, "stop_sequence": None, "usage": usage}
                if not body.get("stream"):
                    self._json(200, message)
                    return
                start = dict(message, content=[], stop_reason=None)
                if block["type"] == "text":
                    events = [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                              {"type": "content_block_delta", "index": 0,
                               "delta": {"type": "text_delta", "text": block["text"]}}]
                else:
                    events = [{"type": "content_block_start", "index": 0,
                               "content_block": dict(block, input={})},
                              {"type": "content_block_delta", "index": 0,
                               "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}}]
                self._sse([{"type": "message_start", "message": start}, *events,
                           {"type": "content_block_stop", "index": 0},
                           {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                            "usage": {"output_tokens": 5}},
                           {"type": "message_stop"}], anthropic=True)

            def _openai(self, body: Dict[str, Any]) -> None:
                turn = mock.next_turn(body)
                cid = "chatcmpl-" + uuid.uuid4().hex[:20]
                model = body.get("model", "mock-model")
                if "tool" in turn:
                    call = {"id": "call_" + uuid.uuid4().hex[:12], "type": "function",
                            "function": {"name": turn["tool"], "arguments": json.dumps(turn["input"])}}
                    message = {"role": "assistant", "content": None, "tool_calls": [call]}
                    finish = "tool_calls"
                else:
                    message = {"role": "assistant", "content": turn["text"]}
                    finish = "stop"
                usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
                if not body.get("stream"):
                    self._json(200, {"id": cid, "object": "chat.completion", "created": 0, "model": model,
                                     "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                                     "usage": usage})
                    return
                delta: Dict[str, Any] = {"role": "assistant"}
                if "tool_calls" in message:
                    delta["tool_calls"] = [dict(message["tool_calls"][0], index=0)]
                else:
                    delta["content"] = message["content"]
                base = {"id": cid, "object": "chat.completion.chunk", "created": 0, "model": model}
                self._sse([dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": None}]),
                           dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": finish}], usage=usage),
                           "[DONE]"], anthropic=False)

        return Handler
