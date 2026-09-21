import json
import os
import tempfile
import unittest
from unittest import mock

import pathsetup  # noqa: F401

from subcortex.config import DEFAULT_CONFIG, load_config


class TestConfigDefaults(unittest.TestCase):
    def setUp(self):
        # Point at a path that does not exist so only defaults apply.
        self._env = mock.patch.dict(os.environ, {
            "SUBCORTEX_CONFIG": "/nonexistent/subcortex/config.json",
        })
        self._env.start()
        # Clear any SUBCORTEX_* overrides that might leak from the real env.
        for name in list(os.environ):
            if name.startswith("SUBCORTEX_") and name != "SUBCORTEX_CONFIG":
                os.environ.pop(name)

    def tearDown(self):
        self._env.stop()

    def test_defaults(self):
        cfg = load_config()
        self.assertEqual(cfg["backend"], "laya")
        self.assertEqual(cfg["port"], 7707)
        self.assertEqual(cfg["model"], "multilingual")
        self.assertIsNone(cfg["thresholds"]["prompt_simple_confidence"])  # calibrated per backend
        self.assertIsNone(cfg["thresholds"]["output_needed_threshold"])
        self.assertEqual(cfg["thresholds"]["min_output_chars"], 6000)
        self.assertEqual(cfg["jev"]["model"], "jev-latest")
        self.assertEqual(cfg["jev"]["timeout"], 2.5)
        self.assertEqual(cfg["jev"]["api_key_env"], "TYPESAFE_API_KEY")

    def test_env_override(self):
        with mock.patch.dict(os.environ, {
            "SUBCORTEX_BACKEND": "jev",
            "SUBCORTEX_PORT": "9999",
            "SUBCORTEX_JEV_MODEL": "typesafe/jev-1.13",
        }):
            cfg = load_config()
        self.assertEqual(cfg["backend"], "jev")
        self.assertEqual(cfg["port"], 9999)
        self.assertEqual(cfg["jev"]["model"], "typesafe/jev-1.13")

    def test_bad_port_env_ignored(self):
        with mock.patch.dict(os.environ, {"SUBCORTEX_PORT": "not-a-number"}):
            cfg = load_config()
        self.assertEqual(cfg["port"], 7707)

    def test_file_config_merges(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"backend": "jev", "thresholds": {"min_output_chars": 100}}, fh)
            path = fh.name
        try:
            cfg = load_config(path)
        finally:
            os.unlink(path)
        self.assertEqual(cfg["backend"], "jev")
        self.assertEqual(cfg["thresholds"]["min_output_chars"], 100)
        # untouched defaults survive
        self.assertIsNone(cfg["thresholds"]["output_needed_threshold"])
        self.assertEqual(cfg["port"], DEFAULT_CONFIG["port"])

    def test_corrupt_file_degrades_to_defaults(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{not json")
            path = fh.name
        try:
            cfg = load_config(path)
        finally:
            os.unlink(path)
        self.assertEqual(cfg["backend"], "laya")


if __name__ == "__main__":
    unittest.main()
