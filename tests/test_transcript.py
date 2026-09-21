import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pathsetup  # noqa: F401

from subcortex import transcript


def write(lines, suffix=".jsonl"):
    tmp = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False)
    if isinstance(lines, str):
        tmp.write(lines)
    else:
        tmp.write("\n".join(json.dumps(line) for line in lines))
    tmp.close()
    return tmp.name


class TestShapes(unittest.TestCase):
    def test_claude_code_jsonl(self):
        path = write([
            {"type": "summary", "summary": "x"},
            {"type": "user", "message": {"role": "user", "content": "fix the parser"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "Fixed it."},
                {"type": "tool_use", "name": "Bash", "input": {}}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}},
        ])
        self.assertEqual(transcript.last_messages(path), [
            {"role": "user", "text": "fix the parser"},
            {"role": "assistant", "text": "Fixed it."},
        ])

    def test_codex_rollout_dedupes_and_skips_injected_context(self):
        path = write([
            {"type": "session_meta", "payload": {"id": "abc"}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "add tests"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "add tests"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "Added."}]}},
        ])
        self.assertEqual(transcript.last_messages(path), [
            {"role": "user", "text": "add tests"},
            {"role": "assistant", "text": "Added."},
        ])

    def test_whole_file_json_with_messages_and_parts(self):
        path = write(json.dumps({"sessionId": "s", "messages": [
            {"type": "user", "content": "hello"},
            {"type": "gemini", "content": "hi there"},
            {"role": "model", "parts": [{"text": "part text"}]},
        ]}), suffix=".json")
        self.assertEqual([m["role"] for m in transcript.last_messages(path)],
                         ["user", "assistant", "assistant"])

    def test_limits_and_truncation(self):
        path = write([{"role": "user", "content": f"m{i} " + "x" * 50} for i in range(10)])
        msgs = transcript.last_messages(path, limit=3, max_chars=10)
        self.assertEqual(len(msgs), 3)
        self.assertTrue(all(len(m["text"]) <= 10 for m in msgs))
        self.assertTrue(msgs[-1]["text"].startswith("m9"))

    def test_tail_read_of_huge_file(self):
        lines = [json.dumps({"role": "user", "content": f"line {i} " + "p" * 200}) for i in range(8000)]
        path = write("\n".join(lines))
        self.assertGreater(Path(path).stat().st_size, transcript.TAIL_BYTES)
        with mock.patch.object(transcript, "WHOLE_FILE_MAX_BYTES", 0):
            msgs = transcript.last_messages(path, limit=2)
        self.assertTrue(msgs[-1]["text"].startswith("line 7999"))

    def test_our_own_injected_context_is_never_snapshotted(self):
        from subcortex import policy

        hint = policy.HINT.format(p=0.93)
        restored = policy.RESTORE_HEADER + "\nuser: older question\nassistant: older answer"
        path = write([
            {"type": "user", "content": [{"text": f"remember PELICAN-42\n<hook_context>{hint}</hook_context>"}],
             "displayContent": "remember PELICAN-42"},                       # Gemini CLI
            {"role": "user", "content": f"/compress\n\n{restored}\n\n{hint}"},  # any other TUI
            {"role": "assistant", "content": "Noted."},
        ])
        self.assertEqual(transcript.last_messages(path), [
            {"role": "user", "text": "remember PELICAN-42"},
            {"role": "user", "text": "/compress"},
            {"role": "assistant", "text": "Noted."},
        ])

    def test_garbage_never_raises(self):
        for bad in (None, "", 42, "/nonexistent/file.jsonl", write("not json at all\n{broken")):
            self.assertEqual(transcript.last_messages(bad), [])


if __name__ == "__main__":
    unittest.main()
