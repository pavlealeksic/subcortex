"""Tests for the minimal stdio MCP server."""

import io
import json
import unittest
from unittest import mock

import pathsetup  # noqa: F401  (puts src/ on sys.path)

from subcortex import mcp_server  # noqa: E402


def frame(obj):
    body = json.dumps(obj).encode()
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


class FakeStream:
    """Minimal stand-in for sys.stdin/sys.stdout with a .buffer."""

    def __init__(self, incoming=b""):
        self.buffer = io.BytesIO(incoming)

    def written(self):
        data = self.buffer.getvalue()
        out = []
        while data:
            head, _, rest = data.partition(b"\r\n\r\n")
            length = int(head.split(b":")[1].strip())
            out.append(json.loads(rest[:length]))
            data = rest[length:]
        return out


class McpTests(unittest.TestCase):
    def _run(self, messages):
        stdin, stdout = FakeStream(b"".join(frame(m) for m in messages)), FakeStream()
        import sys as _sys
        real_stdin, real_stdout = _sys.stdin, _sys.stdout
        _sys.stdin, _sys.stdout = stdin, stdout
        try:
            mcp_server.serve()
        finally:
            _sys.stdin, _sys.stdout = real_stdin, real_stdout
        return stdout.written()

    def test_initialize(self):
        out = self._run([{"jsonrpc": "2.0", "id": 1, "method": "initialize"}])
        self.assertEqual(out[0]["result"]["serverInfo"]["name"], "subcortex")
        self.assertIn("tools", out[0]["result"]["capabilities"])

    def test_tools_list(self):
        out = self._run([{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
        names = [t["name"] for t in out[0]["result"]["tools"]]
        self.assertEqual(names, ["subcortex_decide", "subcortex_classify_prompt",
                                 "subcortex_judge_output"])

    def test_tools_call_decide(self):
        with mock.patch.object(mcp_server, "_post",
                               return_value={"success": True, "answers": {"refund": {"noul": 0.9}}}) as post:
            out = self._run(
                [{"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                  "params": {"name": "subcortex_decide",
                             "arguments": {"state": "billed twice", "questions": {"refund": {"type": "noul", "instructions": "refund?"}}}}}])
        payload = json.loads(out[0]["result"]["content"][0]["text"])
        self.assertTrue(payload["success"])
        post.assert_called_once()

    def test_tools_call_daemon_down_is_error_result(self):
        with mock.patch.object(mcp_server, "_post", side_effect=OSError("connection refused")):
            out = self._run(
                [{"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                  "params": {"name": "subcortex_classify_prompt", "arguments": {"prompt": "hi"}}}])
        result = out[0]["result"]
        self.assertTrue(result["isError"])
        self.assertIn("connection refused", result["content"][0]["text"])

    def test_unknown_method(self):
        out = self._run([{"jsonrpc": "2.0", "id": 5, "method": "bogus/method"}])
        self.assertEqual(out[0]["error"]["code"], -32601)

    def test_notifications_get_no_response(self):
        out = self._run([{"jsonrpc": "2.0", "method": "notifications/initialized"}])
        self.assertEqual(out, [])

    def test_unknown_tool_is_error(self):
        with mock.patch.object(mcp_server, "_post"):
            out = self._run(
                [{"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                  "params": {"name": "nope", "arguments": {}}}])
        self.assertTrue(out[0]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
