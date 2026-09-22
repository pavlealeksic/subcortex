"""A stand-in for the Amp server's internal API (``$AMP_URL/api/internal?<method>``).

Amp runs its agent loop on the Amp server (the thread "actor"); the CLI is only an
executor for tools and plugins, so no model turn can run against a local mock.
What does run locally is the plugin host: ``amp plugins list`` loads plugins after
asking the server for workspace ("global") plugins via ``loadPlugins``. This mock
answers that with an empty list (envelope ``{"ok": true, "result": ...}``) and
every other method with ``auth-required``. Requests are recorded.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

REPLIES: Dict[str, Any] = {"loadPlugins": {"ok": True, "result": []}}
AUTH_REQUIRED = {"ok": False, "error": {"code": "auth-required", "message": "mock Amp server: not logged in"}}


class MockAmpServer:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "MockAmpServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def methods(self) -> List[str]:
        with self.lock:
            return [r["path"].split("?", 1)[-1] for r in self.requests]

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _reply(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with mock.lock:
                    mock.requests.append({"path": self.path, "body": body})
                method = self.path.split("?", 1)[-1] if self.path.startswith("/api/internal?") else ""
                data = json.dumps(REPLIES.get(method, AUTH_REQUIRED)).encode()
                self.send_response(200 if method in REPLIES else 401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = _reply

        return Handler
