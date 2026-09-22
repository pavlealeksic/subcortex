"""`subcortex config`: the interactive settings editor and schema validation."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pathsetup  # noqa: F401

from subcortex import cli, config, settings, wizard
from subcortex.ui import UI

# Top level: Decisions, Jev API key, Behaviors, Thresholds, Hooks, Trimming,
# Compaction, Daemon, Jev endpoint, Done
TOP = {name: str(i + 1) for i, name in enumerate(
    ["Decisions", "key", "Behaviors", "Thresholds", "Hooks", "Trimming", "Compaction", "Daemon",
     "Jev endpoint", "Done"])}


class EditorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name, "config.json")
        self.env = mock.patch.dict(os.environ, {"SUBCORTEX_CONFIG": str(self.config)})
        self.env.start()
        for var in ("SUBCORTEX_PORT", "SUBCORTEX_BACKEND", "TYPESAFE_API_KEY"):
            os.environ.pop(var, None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def edit(self, *lines):
        out = io.StringIO()
        ui = UI(stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out, interactive=False, color=False)
        self.assertEqual(settings.edit(ui), 0)
        return out.getvalue()

    def saved(self):
        return json.loads(self.config.read_text()) if self.config.is_file() else {}


class TestEditor(EditorCase):
    def test_on_off_settings_toggle_in_place(self):
        self.edit(TOP["Behaviors"], "2", "4", TOP["Done"])
        self.assertIs(config.load_config()["features"]["trim_output"], False)

    def test_numbers_are_validated(self):
        out = self.edit(TOP["Thresholds"], "3", "500", "8000", "4", TOP["Done"])
        self.assertIn("between 1000 and 1e+06", out)
        self.assertEqual(config.load_config()["thresholds"]["min_output_chars"], 8000)

    def test_a_custom_trim_threshold_with_laya_needs_consent(self):
        config.save_config({"backend": "laya"})
        out = self.edit(TOP["Thresholds"], "2", "2", "0.35", "y", "4", TOP["Done"])
        self.assertIn("cut needed output", out)
        self.assertEqual(config.load_config()["thresholds"]["output_needed_threshold"], 0.35)
        self.edit(TOP["Thresholds"], "2", "1", "4", TOP["Done"])  # back to calibrated
        self.assertIsNone(config.load_config()["thresholds"]["output_needed_threshold"])

    def test_setting_the_default_again_removes_it_from_the_file(self):
        self.edit(TOP["Thresholds"], "3", "8000", "4", TOP["Done"])
        self.assertIn("min_output_chars", self.saved()["thresholds"])
        self.edit(TOP["Thresholds"], "3", "6000", "4", TOP["Done"])
        self.assertNotIn("min_output_chars", self.saved().get("thresholds", {}))

    def test_choices_and_free_values(self):
        self.edit(TOP["Decisions"], "2", "3", "jev-1.14.0", "4", TOP["Done"])  # Jev model: another value
        self.assertEqual(config.load_config()["jev"]["model"], "jev-1.14.0")

    def test_settings_forced_by_the_environment_say_so(self):
        with mock.patch.dict(os.environ, {"SUBCORTEX_PORT": "7799"}):
            out = self.edit(TOP["Daemon"], "2", TOP["Done"])
        self.assertIn("forced by $SUBCORTEX_PORT", out)

    def test_replacing_the_key_checks_it_first(self):
        key = "apikey_new_0123456789abcdef"
        with mock.patch.object(wizard.Wizard, "check_jev", return_value=(True, "key works")):
            self.edit(TOP["key"], "1", key, TOP["Done"])
        self.assertEqual(config.read_secret("TYPESAFE_API_KEY"), key)
        with mock.patch.object(wizard.Wizard, "check_jev", return_value=(False, "API key rejected (401)")):
            out = self.edit(TOP["key"], "1", "apikey_bad_0123456789abcdef", "n", TOP["Done"])
        self.assertIn("rejected", out)
        self.assertEqual(config.read_secret("TYPESAFE_API_KEY"), key)  # the bad one was not saved
        self.edit(TOP["key"], "2", "y", TOP["Done"])  # remove
        self.assertIsNone(config.read_secret("TYPESAFE_API_KEY"))


class TestConfigCommand(EditorCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_set_validates_through_the_schema(self):
        code, _, err = self.run_cli("config", "set", "hooks.budget_s", "100")
        self.assertEqual(code, 2)
        self.assertIn("between 0.5 and 30", err)
        self.assertEqual(self.run_cli("config", "set", "features.trim_output", "off")[0], 0)
        self.assertIs(config.load_config()["features"]["trim_output"], False)
        self.assertEqual(self.run_cli("config", "set", "thresholds.output_needed_threshold", "calibrated")[0], 0)

    def test_without_a_terminal_config_lists_settings(self):
        code, out, _ = self.run_cli("config")
        self.assertEqual(code, 0)
        self.assertIn("backend = ", out)

    def test_every_setting_parses_its_own_default(self):
        for key, *_ in settings.SETTINGS:
            default = settings._get(config.DEFAULT_CONFIG, key)
            self.assertEqual(settings.parse(key, default), default, key)


if __name__ == "__main__":
    unittest.main()
