import copy
import time
import unittest

import pathsetup  # noqa: F401

from subcortex import verdicts
from subcortex.config import DEFAULT_CONFIG


class FakeBackend:
    """Answers each question with a canned probability (0.5 when not given)."""

    def __init__(self, name="jev", **probs):
        self.name = name
        self.probs = probs
        self.calls = []

    def predict(self, state, questions):
        self.calls.append((state, questions))
        return {"answers": {q: {"type": "noul", "noul": self.probs.get(q, 0.5)} for q in questions}}


class ExplodingBackend:
    name = "jev"

    def predict(self, state, questions):
        raise RuntimeError("boom")


def make_config(**thresholds):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["thresholds"].update(thresholds)
    return cfg


def classify(backend, **thresholds):
    return verdicts.classify_prompt("what is 2+2?", backend=backend, config=make_config(**thresholds))


def judge(backend, output="x" * 7000, task="fix the login bug", **thresholds):
    return verdicts.judge_output(output, "Bash: make", backend=backend, config=make_config(**thresholds),
                                 task=task)


class TestPromptRules(unittest.TestCase):
    def test_jev_needs_quick_and_not_multi_step(self):
        self.assertEqual(classify(FakeBackend("jev", quick=0.9, multi_step=0.1))["label"], "simple")
        self.assertEqual(classify(FakeBackend("jev", quick=0.9, multi_step=0.6))["label"], "complex")  # veto
        self.assertEqual(classify(FakeBackend("jev", quick=0.7, multi_step=0.0))["label"], "complex")

    def test_laya_needs_both_single_step_and_single_file(self):
        self.assertEqual(classify(FakeBackend("laya", multi_step=0.1, multi_file=0.1))["label"], "simple")
        self.assertEqual(classify(FakeBackend("laya", multi_step=0.1, multi_file=0.5))["label"], "complex")
        self.assertEqual(classify(FakeBackend("laya", multi_step=0.3, multi_file=0.0))["label"], "complex")

    def test_uncalibrated_backends_get_the_conservative_rule(self):
        backend = FakeBackend("somebody-else", multi_step=0.1, multi_file=0.1)
        self.assertEqual(classify(backend)["label"], "simple")
        self.assertEqual(set(backend.calls[0][1]), {"multi_step", "multi_file"})

    def test_verdict_shape_and_confidence(self):
        verdict = classify(FakeBackend("jev", quick=0.93, multi_step=0.1))
        self.assertEqual(verdict, {"label": "simple", "confidence": 0.93,
                                   "signals": {"quick": 0.93, "multi_step": 0.1}})
        self.assertAlmostEqual(classify(FakeBackend("jev", quick=0.2, multi_step=0.1))["confidence"], 0.8)

    def test_user_threshold_overrides_the_primary_condition(self):
        backend = FakeBackend("jev", quick=0.7, multi_step=0.1)
        self.assertEqual(classify(backend)["label"], "complex")
        self.assertEqual(classify(backend, prompt_simple_confidence=0.6)["label"], "simple")
        self.assertEqual(classify(backend, prompt_simple_confidence="junk")["label"], "complex")

    def test_questions_name_what_they_read_and_the_prompt_is_redacted(self):
        backend = FakeBackend("jev")
        verdicts.classify_prompt("use key sk-abcdefghijklmnopqrstuvwxyz " + "y" * 5000, backend=backend,
                                 config=make_config())
        state, questions = backend.calls[0]
        self.assertNotIn("sk-abcdefghijklmnop", state["prompt"])
        self.assertLessEqual(len(state["prompt"]), verdicts.PROMPT_CHARS)
        for question in questions.values():
            self.assertIn("`prompt`", question["instructions"])

    def test_never_raises(self):
        self.assertIsNone(classify(ExplodingBackend()))
        self.assertIsNone(classify(FakeBackend("jev", quick=1.5)))  # not a probability


class TestOutputRules(unittest.TestCase):
    def test_short_output_and_missing_task_never_reach_the_model(self):
        backend = FakeBackend("jev", routine=1.0, needed=0.0)
        self.assertEqual(verdicts.judge_output("tiny", backend=backend, config=make_config()),
                         {"needed": True, "p_needed": 1.0})
        self.assertTrue(judge(backend, task="")["needed"])
        self.assertTrue(judge(backend, task="   ")["needed"])
        self.assertEqual(backend.calls, [])

    def test_jev_trims_routine_output_unless_the_veto_objects(self):
        self.assertFalse(judge(FakeBackend("jev", routine=0.9, needed=0.1))["needed"])
        self.assertTrue(judge(FakeBackend("jev", routine=0.9, needed=0.4))["needed"])  # veto
        self.assertTrue(judge(FakeBackend("jev", routine=0.5, needed=0.0))["needed"])

    def test_laya_does_not_trim_unless_the_user_opts_in(self):
        # Held out, Laya trimmed needed outputs: no model call, output kept.
        backend = FakeBackend("laya", depends=0.0, needed=0.0)
        verdict = judge(backend)
        self.assertTrue(verdict["needed"])
        self.assertIn("does not trim by default", verdict["reason"])
        self.assertEqual(backend.calls, [])
        # An explicit threshold opts in to the (conservative) Laya rule.
        self.assertFalse(judge(FakeBackend("laya", depends=0.5, needed=0.5), output_needed_threshold=0.9)["needed"])
        self.assertTrue(judge(FakeBackend("laya", depends=0.5, needed=0.8), output_needed_threshold=0.9)["needed"])

    def test_user_threshold_overrides_the_primary_condition(self):
        backend = FakeBackend("laya", depends=0.95, needed=0.1)
        self.assertTrue(judge(backend, output_needed_threshold=0.9)["needed"])
        self.assertFalse(judge(backend, output_needed_threshold=0.97)["needed"])

    def test_state_is_evidence_first_bounded_and_redacted(self):
        backend = FakeBackend("jev")
        secret_output = "export API_TOKEN=abcd1234secret\n" + "compiling\n" * 3000
        verdicts.judge_output(secret_output, "Bash: make " + "z" * 900, backend=backend, config=make_config(),
                              task="fix the build " + "q" * 900)
        state, questions = backend.calls[0]
        self.assertEqual(list(state), ["task", "tool_call", "output"])  # Laya truncates the end
        self.assertLessEqual(len(state["task"]), verdicts.TASK_CHARS)
        self.assertLessEqual(len(state["tool_call"]), verdicts.TOOL_CALL_CHARS)
        self.assertLess(len(state["output"]), verdicts.OUTPUT_EXCERPT_CHARS + 100)
        self.assertNotIn("abcd1234secret", state["output"])
        texts = " ".join(q["instructions"] for q in questions.values())
        for key in state:  # every field the model gets is referenced by name
            self.assertIn(f"`{key}`", texts)

    def test_never_raises(self):
        self.assertIsNone(judge(ExplodingBackend()))


class TestRedaction(unittest.TestCase):
    def test_common_secret_shapes_are_masked(self):
        secrets = [
            "sk-ant-api03-abcdefghijklmnopqrstuv", "ghp_" + "a" * 36, "github_pat_" + "b" * 40,
            "xoxb-1234567890-abcdefghij", "AKIAABCDEFGHIJKLMNOP", "AIza" + "c" * 35,
            "apikey_21544d2de9142d6149ea_b5ec7d756b6ba5fabde6", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcdefghijk",
        ]
        for secret in secrets:
            self.assertNotIn(secret, verdicts.redact(f"value: {secret} end"), secret)
        text = verdicts.redact("password=hunter2hunter DB_PASSWORD: 's3cr3tv4lue' postgres://app:pw1234@db/x")
        for leaked in ("hunter2hunter", "s3cr3tv4lue", "pw1234"):
            self.assertNotIn(leaked, text)
        key = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
        self.assertNotIn("MIIabc", verdicts.redact(key))

    def test_ordinary_text_is_untouched(self):
        for text in ("src/api/upload.py:88: def parse_csv(file):", "DATABASE_URL=postgres://app@db:5432/app",
                     "token count: 42", "Compiling crate_12 v0.3.1"):
            self.assertEqual(verdicts.redact(text), text)

    def test_linear_time(self):
        for text in ("a" * 300000, "password=" * 20000, "eyJ" * 50000, "-----BEGIN PRIVATE KEY-----" + "x" * 300000):
            started = time.perf_counter()
            verdicts.redact(text)
            self.assertLess(time.perf_counter() - started, 1.0, text[:12])


if __name__ == "__main__":
    unittest.main()
