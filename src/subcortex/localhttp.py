"""HTTP to the local daemon over a direct socket to 127.0.0.1, and nothing else.

Not urllib: urllib honors HTTP_PROXY and the system proxy settings, so on a
machine with a proxy configured, requests for 127.0.0.1 (carrying prompts and
tool output) went to the proxy, or failed. It is also the slowest import in a
hook process (~15 ms). The daemon answers HTTP/1.0: one request per connection,
closed after the reply, which is all this client needs to handle.
"""

from __future__ import annotations

import json
import socket
import time
from typing import Any, Dict, Optional, Tuple

MAX_RESPONSE_BYTES = 8_000_000


class LocalHTTPError(OSError):
    """The daemon answered something that is not a well-formed HTTP reply."""


def request(port: int, method: str, path: str, payload: Optional[Dict[str, Any]] = None,
            timeout: float = 3.0) -> Tuple[int, Any]:
    """``(status, parsed JSON body)``. Raises OSError (incl. ConnectionRefusedError,
    TimeoutError) or ValueError; the whole exchange is bounded by ``timeout``."""
    from .auth import HEADER, read_token

    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    head = (f"{method} {path} HTTP/1.0\r\nHost: 127.0.0.1:{int(port)}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            f"{HEADER}: {read_token()}\r\nX-Subcortex-Timeout-Ms: {int(timeout * 1000)}\r\n\r\n")
    deadline = time.monotonic() + timeout
    chunks, size = [], 0
    # One deadline for the whole exchange: connect, send and every read share it.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(("127.0.0.1", int(port)))
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        sock.sendall(head.encode("ascii") + body)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("daemon reply timed out")
            sock.settimeout(remaining)
            chunk = sock.recv(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise LocalHTTPError("daemon reply too large")
            chunks.append(chunk)
    raw = b"".join(chunks)
    header, sep, rest = raw.partition(b"\r\n\r\n")
    status_line = header.split(b"\r\n", 1)[0].split(b" ", 2)
    if not sep or len(status_line) < 2 or not status_line[0].startswith(b"HTTP/"):
        raise LocalHTTPError("malformed daemon reply")
    return int(status_line[1]), (json.loads(rest) if rest.strip() else None)
