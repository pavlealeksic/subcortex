"""A stand-in for a vendor's Connect-RPC (protobuf) API server, for e2e runs.

Devin CLI talks to Cognition's private ``exa.*`` protobuf services
(``/exa.api_server_pb.ApiServerService/GetChatMessage`` for model turns).
Their schemas aren't public, so this server doesn't script replies: it answers
every call with an empty message (HTTP 200, zero-length ``application/proto``
body — the protobuf default of any message type) and records the raw request
bytes. Protobuf carries strings verbatim, so tests can still assert on what
the TUI sent to the model (e.g. that a hook's context reached the prompt).
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, List, Tuple


class MockConnect:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, bytes]] = []   # (path, content type, raw body)
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "MockConnect":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def bodies(self, method: str) -> List[bytes]:
        """Raw request bodies of every call to ``.../<method>``."""
        return [body for path, _, body in self.calls if path.rstrip("/").endswith("/" + method)]

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _empty(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                ctype = self.headers.get("Content-Type") or "application/proto"
                with mock.lock:
                    mock.calls.append((self.path, ctype, body))
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_POST = _empty
            do_GET = _empty

        return Handler
