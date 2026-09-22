"""A stand-in for Cursor's backend (api2.cursor.sh) for e2e runs of the Cursor CLI.

The Cursor CLI has no bring-your-own-model mode in its public build: every turn
goes to Cursor's backend over Connect RPC (protobuf). ``CURSOR_API_ENDPOINT``
points the CLI at any URL, so this mock answers the startup RPCs just enough
for the interactive UI to come up (the default, empty message for every unary
call, plus one model), records every request, and ends every stream with an
error. Nothing is ever generated: the point is to capture what the CLI sends
for a turn — with ``network.useHttp1ForAgent`` the agent request goes out as
``/aiserver.v1.BidiService/BidiAppend`` calls whose ``data`` field is the
hex-encoded ``agent.v1`` client message (prompt, hook additional_context ...).
"""

from __future__ import annotations

import gzip
import json
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, List, Tuple

MODEL_ID = "mock-model"


def _varint(n: int) -> bytes:
    out = b""
    while True:
        byte = n & 0x7F
        n >>= 7
        if not n:
            return out + bytes([byte])
        out += bytes([byte | 0x80])


def _field(number: int, payload: bytes) -> bytes:  # length-delimited
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


_MODEL = b"".join(_field(n, v.encode()) for n, v in ((1, MODEL_ID), (3, MODEL_ID), (4, "Mock"), (5, "Mock")))
UNARY_REPLIES = {  # everything else gets the default (empty) message
    "/aiserver.v1.AiService/GetDefaultModelForCli": _field(1, _MODEL),  # GetDefaultModelForCliResponse.model
    "/aiserver.v1.AiService/GetUsableModels": _field(1, _MODEL),        # GetUsableModelsResponse.models
}


def _read_varint(data: bytes, i: int) -> Tuple[int, int]:
    shift = value = 0
    while True:
        byte = data[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, i
        shift += 7


def _fields(data: bytes) -> Iterator[Tuple[int, Any]]:
    i = 0
    while i < len(data):
        key, i = _read_varint(data, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _read_varint(data, i)
        elif wire == 2:
            size, i = _read_varint(data, i)
            value, i = data[i:i + size], i + size
        elif wire == 1:
            value, i = data[i:i + 8], i + 8
        elif wire == 5:
            value, i = data[i:i + 4], i + 4
        else:
            return
        yield number, value


class MockCursorBackend:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "MockCursorBackend":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def paths(self) -> List[str]:
        with self.lock:
            return [r["path"] for r in self.requests]

    def agent_messages(self) -> List[bytes]:
        """The agent.v1 client messages the CLI streamed for its turns (decoded)."""
        messages = []
        with self.lock:
            appends = [r for r in self.requests if r["path"].endswith(".BidiService/BidiAppend")]
        for request in appends:
            body = request["body"]
            try:
                encoding = request["headers"].get("content-encoding", "")
                if encoding == "gzip":
                    body = gzip.decompress(body)
                elif encoding == "deflate":
                    body = zlib.decompress(body)
                for number, value in _fields(body):
                    if number == 1 and isinstance(value, bytes):    # data: hex string
                        messages.append(bytes.fromhex(value.decode()))
                    elif number == 4 and isinstance(value, bytes):  # data_binary
                        messages.append(value)
            except (ValueError, OSError, IndexError, zlib.error):
                continue
        return messages

    def _handler(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def _body(self) -> bytes:
                length = self.headers.get("Content-Length")
                if length is not None:
                    return self.rfile.read(int(length))
                if (self.headers.get("Transfer-Encoding") or "").lower() != "chunked":
                    return b""
                chunks = []
                while True:
                    size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                    if size == 0:
                        self.rfile.readline()
                        return b"".join(chunks)
                    chunks.append(self.rfile.read(size))
                    self.rfile.readline()

            def _reply(self) -> None:
                body = self._body()
                headers = {k.lower(): v for k, v in self.headers.items()}
                with mock.lock:
                    mock.requests.append({"path": self.path, "headers": headers, "body": body})
                ctype = headers.get("content-type", "")
                if ctype.startswith("application/connect+"):  # a stream: end it with an error
                    end = json.dumps({"error": {"code": "unavailable", "message": "mock backend: no model"}}).encode()
                    data = b"\x02" + len(end).to_bytes(4, "big") + end
                elif ctype.startswith("application/proto"):
                    data = UNARY_REPLIES.get(self.path, b"")
                else:
                    ctype, data = "application/json", b"{}"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = _reply

        return Handler
