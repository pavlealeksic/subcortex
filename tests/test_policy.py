import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pathsetup  # noqa: F401

from subcortex import policy, state
from subcortex.config import DEFAULT_CONFIG, load_config

BIG = "compiling module\n" * 800  # ~13.6k chars, no failure markers


def cfg(**overrides):
    c = json.loads(json.dumps(DEFAULT_CONFIG))
    for section, values in overrides.items():
        c[section].update(values)
    return c


def simple(confidence=0.95):
    return lambda prompt: {"label": "simple", "confidence": confidence}


def disposable(p_needed=0.05):
    return lambda output, context, task: {"needed": False, "p_needed": p_needed}


class TestPromptHint(unittest.TestCase):
    def test_simple_prompt_gets_hint(self):
        hint = policy.prompt_hint("what is 2+2?", cfg(), simple(0.95))
        self.assertIn("simple", hint)
        self.assertIn("0.95", hint)

    def test_complex_or_missing_verdict_gets_nothing(self):
        # Thresholds are the verdict's business (calibrated per backend in verdicts.py).
        self.assertIsNone(policy.prompt_hint("x", cfg(), lambda p: {"label": "complex", "confidence": 0.99}))
        self.assertIsNone(policy.prompt_hint("x", cfg(), lambda p: None))
        self.assertIsNone(policy.prompt_hint("x", cfg(), lambda p: "simple"))

    def test_empty_prompt_never_calls_the_daemon(self):
        called = []
        policy.prompt_hint("   ", cfg(), lambda p: called.append(p))
        policy.prompt_hint(None, cfg(), lambda p: called.append(p))
        self.assertEqual(called, [])

    def test_feature_switch(self):
        self.assertIsNone(policy.prompt_hint("hi", cfg(features={"prompt_hint": False}), simple()))

    def test_classifier_exceptions_fail_open(self):
        def boom(prompt):
            raise RuntimeError("daemon exploded")
        self.assertIsNone(policy.prompt_hint("hi", cfg(), boom))
        self.assertIsNone(policy.prompt_hint("hi", cfg(), lambda p: {"label": "simple", "confidence": "nan?"}))


class TestTrimOutput(unittest.TestCase):
    def test_disposable_output_is_truncated(self):
        out = policy.trim_output(BIG, cfg(), disposable(), tool="Bash", tool_input={"command": "make"})
        self.assertIsNotNone(out)
        self.assertTrue(out.startswith(BIG[:1000]))
        self.assertTrue(out.endswith(BIG[-450:]))
        self.assertIn("[subcortex: truncated", out)
        self.assertLess(len(out), len(BIG))

    def test_needed_output_is_kept(self):
        self.assertIsNone(policy.trim_output(BIG, cfg(), lambda o, c, t: {"needed": True, "p_needed": 0.9}))
        self.assertIsNone(policy.trim_output(BIG, cfg(), lambda o, c, t: {"p_needed": 0.0}))  # no verdict
        self.assertIsNone(policy.trim_output(BIG, cfg(), lambda o, c, t: None))

    def test_short_output_never_judged(self):
        called = []
        self.assertIsNone(policy.trim_output("short", cfg(), lambda o, c, t: called.append(o)))
        self.assertEqual(called, [])

    def test_min_chars_never_below_what_truncation_saves(self):
        c = cfg(thresholds={"min_output_chars": 10})
        text = "y" * 1600  # head+tail = 1500: truncating would barely save anything
        self.assertIsNone(policy.trim_output(text, c, disposable()))

    def test_failures_are_never_trimmed(self):
        for marker in ("Traceback (most recent call last):\n", "error: linker failed\n",
                       "npm ERR! code 1\n", "3 passed, 1 FAILED\n", "Process exited: exit code 2\n",
                       "src/main.c:10:5: error: expected ';'\n", "app.ts(3,1): error TS2322: nope\n",
                       "--- FAIL: TestLogin (0.00s)\n", "make: *** [Makefile:12: all] Error 1\n",
                       "  1 failing\n", "2 failures\n", "Segmentation fault (core dumped)\n",
                       "thread 'main' panicked at src/lib.rs:4:5\n", "1 error generated.\n"):
            text = BIG + marker + BIG
            self.assertIsNone(policy.trim_output(text, cfg(), disposable()), marker)
        self.assertIsNone(policy.trim_output(BIG, cfg(), disposable(), failed=True))

    def test_passing_summaries_are_not_failures(self):
        for line in ("test result: ok. 70 passed; 0 failed; 0 ignored\n", "0 errors, 0 warnings\n",
                     "Tests: 70 passed, 70 total\n", "no failures\n"):
            self.assertFalse(policy.looks_like_failure(BIG + line), line)

    def test_failure_detection_is_linear_on_pathological_input(self):
        for text in ("\r\n" * 40000 + "x", "\n" * 200000, " " * 200000 + "error", "\t\n" * 50000):
            started = time.perf_counter()
            policy.looks_like_failure(text)
            self.assertLess(time.perf_counter() - started, 0.5, repr(text[:6]))

    def test_task_lines_and_warnings_survive_a_trim(self):
        middle = "".join(f"compiling crate_{i}\n" for i in range(600))
        text = (middle[:3000] + "src/api/upload.py:88: def parse_csv(file):\n"
                + "warning: unused variable `rows`\n" + middle[3000:])
        out = policy.trim_output(text, cfg(), disposable(), task="add a size limit to parse_csv in upload.py")
        self.assertIn("src/api/upload.py:88: def parse_csv(file):", out)
        self.assertIn("warning: unused variable `rows`", out)
        self.assertIn("kept 2 lines", out)
        self.assertLess(len(out), len(text) * 0.8)

    def test_terms_found_everywhere_do_not_rescue_everything(self):
        text = "".join(f"tests/test_mod_{i}.py::test_case PASSED\n" for i in range(800))
        out = policy.trim_output(text, cfg(), disposable(), task="run the test suite")
        self.assertIsNotNone(out)
        self.assertNotIn("kept", out)

    def test_cuts_land_on_line_boundaries(self):
        out = policy.trim_output(BIG, cfg(), disposable())
        head, _, rest = out.partition("\n[subcortex:")
        self.assertTrue(head.endswith("compiling module\n"))
        self.assertTrue(rest.split("]\n", 1)[1].startswith("compiling module"))

    def test_zero_or_negative_head_tail_never_grow_the_output(self):
        for head, tail in ((1000, 0), (0, 0), (-5, -5), (0, 500)):
            out = policy.trim_output(BIG, cfg(hooks={"head_chars": head, "tail_chars": tail}), disposable())
            self.assertIsNotNone(out, (head, tail))
            self.assertLess(len(out), len(BIG))

    def test_context_carries_tool_and_input(self):
        seen = []
        policy.trim_output(BIG, cfg(), lambda o, c, t: seen.append(c), tool="Bash",
                           tool_input={"command": "npm install"})
        self.assertEqual(seen, ['Bash: {"command": "npm install"}'])

    def test_task_evidence_reaches_the_judge(self):
        seen = []
        policy.trim_output(BIG, cfg(), lambda o, c, t: seen.append(t), task="fix the login bug")
        self.assertEqual(seen, ["fix the login bug"])

    def test_non_string_and_feature_switch(self):
        self.assertIsNone(policy.trim_output({"stdout": BIG}, cfg(), disposable()))
        self.assertIsNone(policy.trim_output(BIG, cfg(features={"trim_output": False}), disposable()))


class TestSnapshots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"SUBCORTEX_DATA_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_round_trip_consumes_snapshot(self):
        msgs = [{"role": "user", "text": f"message {i}"} for i in range(8)]
        self.assertTrue(policy.save_snapshot("sess-1", msgs, cfg(), "auto"))
        ctx = policy.restore_snapshot("sess-1", cfg())
        self.assertTrue(ctx.startswith(policy.RESTORE_HEADER))
        self.assertIn("message 7", ctx)
        self.assertNotIn("message 2", ctx)  # only the last 5 are kept
        self.assertIsNone(policy.restore_snapshot("sess-1", cfg()))

    def test_session_ids_cannot_escape_the_snapshot_dir(self):
        policy.save_snapshot("../../etc/passwd", [{"role": "user", "text": "x"}], cfg())
        files = list(Path(self.tmp.name, "compact").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].parent, Path(self.tmp.name, "compact"))

    def test_missing_ids_and_empty_messages_do_nothing(self):
        self.assertFalse(policy.save_snapshot("", [{"role": "user", "text": "x"}], cfg()))
        self.assertFalse(policy.save_snapshot("s", [], cfg()))
        self.assertFalse(policy.save_snapshot("s", [{"role": "user", "text": "  "}], cfg()))
        self.assertIsNone(policy.restore_snapshot("", cfg()))
        self.assertIsNone(policy.restore_snapshot("never-saved", cfg()))

    def test_old_snapshots_are_pruned(self):
        policy.save_snapshot("old", [{"role": "user", "text": "x"}], cfg())
        old = state.path("compact", "", "old")
        stale = time.time() - policy.SNAPSHOT_MAX_AGE_S - 60
        os.utime(old, (stale, stale))
        Path(self.tmp.name, "compact", ".pruned").unlink(missing_ok=True)  # prunes every few minutes
        policy.save_snapshot("new", [{"role": "user", "text": "y"}], cfg())
        self.assertFalse(old.exists())

    def test_corrupt_snapshot_fails_open(self):
        path = Path(self.tmp.name, "compact", "bad.json")
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        self.assertIsNone(policy.restore_snapshot("bad", cfg()))

    def test_snapshot_from_transcript(self):
        transcript = Path(self.tmp.name, "t.jsonl")
        transcript.write_text("\n".join(json.dumps(
            {"type": role, "message": {"role": role, "content": text}})
            for role, text in [("user", "fix the bug"), ("assistant", "done")]))
        self.assertTrue(policy.snapshot_transcript("s2", str(transcript), cfg()))
        self.assertIn("user: fix the bug", policy.restore_snapshot("s2", cfg()))


class TestTaskMemory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"SUBCORTEX_DATA_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_remember_and_expire(self):
        self.assertEqual(policy.last_prompt("s1"), "")
        policy.remember_prompt("s1", "  refactor the parser  ")
        self.assertEqual(policy.last_prompt("s1"), "refactor the parser")
        policy.remember_prompt("s1", "")  # empty prompts don't overwrite
        self.assertEqual(policy.last_prompt("s1"), "refactor the parser")
        path = state.path("tasks", "", "s1")
        stale = time.time() - policy.TASK_MAX_AGE_S - 60
        os.utime(path, (stale, stale))
        self.assertEqual(policy.last_prompt("s1"), "")
        policy.remember_prompt("../../evil", "x")
        self.assertTrue(all(p.parent.name == "tasks" for p in Path(self.tmp.name).rglob("*.json")))


class TestConfigDefaults(unittest.TestCase):
    def test_new_sections_have_defaults_and_env_budget_override(self):
        with mock.patch.dict(os.environ, {"SUBCORTEX_CONFIG": "/nonexistent.json",
                                          "SUBCORTEX_HOOK_BUDGET": "1.5"}):
            c = load_config()
        self.assertTrue(c["features"]["trim_output"])
        self.assertEqual(c["hooks"]["budget_s"], 1.5)


if __name__ == "__main__":
    unittest.main()
