import copy
import unittest

import pathsetup  # noqa: F401

from subcortex import verdicts
from subcortex.config import DEFAULT_CONFIG


class FakeBackend:
    """Returns a canned noul probability for whichever question it is given."""

    name = "fake"

    def __init__(self, prob):
        self.prob = prob
        self.calls = []

    def predict(self, state, questions):
        self.calls.append((state, questions))
        return {"answers": {name: {"noul": self.prob} for name in questions}}


class ExplodingBackend:
    name = "exploding"

    def predict(self, state, questions):
        raise RuntimeError("boom")


def make_config(**thresholds):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["thresholds"].update(thresholds)
    return cfg


class TestClassifyPrompt(unittest.TestCase):
    def test_simple(self):
        backend = FakeBackend(0.95)
        verdict = verdicts.classify_prompt("what is 2+2?", backend=backend, config=make_config())
        self.assertEqual(verdict, {"label": "simple", "confidence": 0.95})

    def test_complex(self):
        backend = FakeBackend(0.2)
        verdict = verdicts.classify_prompt("refactor the compiler", backend=backend, config=make_config())
        self.assertEqual(verdict, {"label": "complex", "confidence": 0.8})

    def test_threshold_edge_is_simple(self):
        backend = FakeBackend(0.8)  # exactly at the default threshold
        verdict = verdicts.classify_prompt("x", backend=backend, config=make_config())
        self.assertEqual(verdict["label"], "simple")
        self.assertAlmostEqual(verdict["confidence"], 0.8)

    def test_just_below_threshold_is_complex(self):
        backend = FakeBackend(0.79)
        verdict = verdicts.classify_prompt("x", backend=backend, config=make_config())
        self.assertEqual(verdict["label"], "complex")

    def test_custom_threshold(self):
        backend = FakeBackend(0.6)
        cfg = make_config(prompt_simple_confidence=0.5)
        self.assertEqual(
            verdicts.classify_prompt("x", backend=backend, config=cfg)["label"], "simple")
        cfg = make_config(prompt_simple_confidence=0.7)
        self.assertEqual(
            verdicts.classify_prompt("x", backend=backend, config=cfg)["label"], "complex")

    def test_never_raises(self):
        self.assertIsNone(verdicts.classify_prompt("x", backend=ExplodingBackend(),
                                                   config=make_config()))
        # malformed backend result
        class Weird:
            name = "weird"
            def predict(self, state, questions):
                return {"answers": {}}
        self.assertIsNone(verdicts.classify_prompt("x", backend=Weird(), config=make_config()))


class TestJudgeOutput(unittest.TestCase):
    def test_short_output_kept_without_backend(self):
        backend = FakeBackend(0.0)
        verdict = verdicts.judge_output("tiny", backend=backend, config=make_config())
        self.assertEqual(verdict, {"needed": True, "p_needed": 1.0})
        self.assertEqual(backend.calls, [])  # model never consulted

    def test_long_output_needed(self):
        backend = FakeBackend(0.9)
        verdict = verdicts.judge_output("x" * 7000, backend=backend, config=make_config())
        self.assertEqual(verdict, {"needed": True, "p_needed": 0.9})

    def test_long_output_disposable(self):
        backend = FakeBackend(0.1)
        verdict = verdicts.judge_output("x" * 7000, backend=backend, config=make_config())
        self.assertEqual(verdict, {"needed": False, "p_needed": 0.1})

    def test_threshold_edge_is_needed(self):
        backend = FakeBackend(0.3)  # exactly at the default threshold
        verdict = verdicts.judge_output("x" * 7000, backend=backend, config=make_config())
        self.assertTrue(verdict["needed"])

    def test_min_output_chars_boundary(self):
        backend = FakeBackend(0.0)
        cfg = make_config(min_output_chars=10)
        # exactly at the boundary -> judged by the backend
        verdict = verdicts.judge_output("x" * 10, backend=backend, config=cfg)
        self.assertEqual(verdict, {"needed": False, "p_needed": 0.0})
        self.assertEqual(len(backend.calls), 1)

    def test_never_raises(self):
        self.assertIsNone(verdicts.judge_output("x" * 7000, backend=ExplodingBackend(),
                                                config=make_config()))


if __name__ == "__main__":
    unittest.main()
