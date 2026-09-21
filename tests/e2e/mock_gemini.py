"""A tiny fake Gemini API (generateContent / streamGenerateContent / countTokens).

Same contract as mock_llm.MockLLM — every request body is recorded and replies
are scripted (``{"text": ...}`` or ``{"tool": name, "input": {...}}``, then
"OK") — but spoken in the Gemini API's wire format, for Gemini CLI pointed at it
with ``GOOGLE_GEMINI_BASE_URL``. Side requests (routing, next-speaker and loop
checks, titles, compression summaries) carry no ``tools`` and don't consume the
script; JSON-mode side requests get a small object that satisfies the CLI's
known side-query schemas.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, List

from mock_llm import MockLLM

# Routers ("model_choice", "complexity_score"), next-speaker check ("next_speaker"),
# loop check ("unproductive_state_*"): one object answers all of them harmlessly.
_SIDE_JSON = {"reasoning": "mock", "model_choice": "pro", "next_speaker": "user",
              "complexity_reasoning": "mock", "complexity_score": 50,
              "unproductive_state_confidence": 0.0, "unproductive_state_analysis": "none"}


class MockGemini(MockLLM):
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

            def do_GET(self) -> None:
                if self.path.split("?")[0].rstrip("/").endswith("/models"):
                    self._json(200, {"models": [{"name": "models/gemini-2.5-pro", "displayName": "Mock",
                                                 "inputTokenLimit": 1048576, "outputTokenLimit": 65536,
                                                 "supportedGenerationMethods": ["generateContent"]}]})
                else:
                    self._json(404, {"error": {"code": 404, "message": "not found", "status": "NOT_FOUND"}})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {}
                with mock.lock:
                    mock.requests.append({"path": self.path, "body": body})
                method = self.path.split("?")[0].rsplit(":", 1)[-1]
                if method == "countTokens":
                    self._json(200, {"totalTokens": 10})
                elif method in ("generateContent", "streamGenerateContent"):
                    self._generate(body, stream=method == "streamGenerateContent")
                elif method == "embedContent":
                    self._json(200, {"embedding": {"values": [0.0] * 8}})
                else:
                    self._json(404, {"error": {"code": 404, "message": "not mocked", "status": "NOT_FOUND"}})

            def _generate(self, body: Dict[str, Any], stream: bool) -> None:
                turn = mock.next_turn(body)
                config = body.get("generationConfig") or {}
                if "tool" in turn:
                    part: Dict[str, Any] = {"functionCall": {"name": turn["tool"], "args": turn["input"]}}
                elif not body.get("tools") and (config.get("responseMimeType") == "application/json"
                                                or config.get("responseJsonSchema") or config.get("responseSchema")):
                    part = {"text": json.dumps(_SIDE_JSON)}
                else:
                    part = {"text": turn["text"]}
                response = {"candidates": [{"content": {"role": "model", "parts": [part]},
                                            "finishReason": "STOP", "index": 0}],
                            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5,
                                              "totalTokenCount": 15},
                            "modelVersion": "gemini-2.5-pro", "responseId": "mock"}
                if not stream:
                    self._json(200, response)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(f"data: {json.dumps(response)}\r\n\r\n".encode())
                self.wfile.flush()

        return Handler

    # Gemini request bodies carry ``contents``/``tools`` like the others; helpers below
    # make assertions on them readable.

    @staticmethod
    def texts(body: Dict[str, Any]) -> List[str]:
        """Every text and functionResponse (as JSON) in a request's contents, in order."""
        out: List[str] = []
        for content in body.get("contents") or []:
            for part in content.get("parts") or []:
                if "text" in part:
                    out.append(part["text"])
                elif "functionResponse" in part:
                    out.append(json.dumps(part["functionResponse"]))
        return out
