"""MCP stdio server: exercised as a real subprocess speaking the spec's
newline-delimited JSON-RPC, against an in-process stub daemon."""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import pathsetup  # noqa: F401

from subcortex import mcp_server
from subcortex.daemon import create_server

SRC = str(Path(__file__).resolve().parents[1] / "src")


class StubBackend:
    name = "jev"  # a calibrated profile

    def predict(self, state, questions):
        from subcortex.verdicts import canned_answers

        return canned_answers(questions)  # every prompt simple, every output disposable

    def available(self):
        return True, "stub"


class TestMcpSubprocess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        config = Path(cls.tmp.name, "config.json")
        config.write_text("{}")
        cls.server = create_server(0, {"backend": "laya", "port": 0, "model": "x",
                                       "thresholds": {"prompt_simple_confidence": 0.8,
                                                      "output_needed_threshold": 0.3,
                                                      "min_output_chars": 10}},
                                   backend_factory=lambda cfg, name=None: StubBackend())
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.env = dict(os.environ, PYTHONPATH=SRC, SUBCORTEX_CONFIG=str(config),
                       SUBCORTEX_PORT=str(cls.server.server_address[1]))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def converse(self, messages):
        stdin = "".join(json.dumps(m) + "\n" for m in messages)
        proc = subprocess.run([sys.executable, "-m", "subcortex", "mcp"], input=stdin,
                              capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        return [json.loads(line) for line in lines]  # every stdout line must be JSON-RPC

    def test_handshake_list_and_call(self):
        replies = self.converse([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "subcortex_classify_prompt", "arguments": {"prompt": "2+2?"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "subcortex_judge_output",
                        "arguments": {"output": "x" * 50, "context": "Bash: ls", "task": "list files"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
             "params": {"name": "subcortex_decide",
                        "arguments": {"state": "s", "questions": {"q": {"type": "noul", "instructions": "?"}}}}},
        ])
        self.assertEqual([r["id"] for r in replies], [1, 2, 3, 4, 5])  # notification: no reply
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "subcortex")
        self.assertEqual([t["name"] for t in replies[1]["result"]["tools"]],
                         ["subcortex_decide", "subcortex_classify_prompt", "subcortex_judge_output"])
        verdict = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertEqual(verdict["verdict"]["label"], "simple")
        judged = json.loads(replies[3]["result"]["content"][0]["text"])
        self.assertFalse(judged["verdict"]["needed"])
        decided = json.loads(replies[4]["result"]["content"][0]["text"])
        self.assertTrue(decided["success"])

    def test_unknown_version_gets_latest_and_errors_are_reported(self):
        replies = self.converse([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}},
            {"jsonrpc": "2.0", "id": 2, "method": "no/such/method"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
        ])
        self.assertEqual(replies[0]["result"]["protocolVersion"], mcp_server._PROTOCOL_VERSION)
        self.assertEqual(replies[1]["error"]["code"], -32601)
        self.assertTrue(replies[2]["result"]["isError"])


class TestMcpInProcess(unittest.TestCase):
    def run_lines(self, text):
        out = io.StringIO()
        mcp_server.serve(io.StringIO(text), out)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_parse_error_and_blank_lines_do_not_kill_the_loop(self):
        replies = self.run_lines('\n{not json}\n{"jsonrpc":"2.0","id":7,"method":"ping"}\n')
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1], {"jsonrpc": "2.0", "id": 7, "result": {}})

    def test_daemon_down_is_a_tool_error_not_a_crash(self):
        with mock.patch.object(mcp_server, "_post", side_effect=OSError("connection refused")):
            replies = self.run_lines(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                 "params": {"name": "subcortex_classify_prompt",
                                                            "arguments": {"prompt": "x"}}}) + "\n")
        self.assertTrue(replies[0]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
