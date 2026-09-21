"""Tests for the OpenCode adapter: installer round-trip (temp HOME) and a
static contract check of the TS plugin. No network, no TS execution."""

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pathsetup  # noqa: F401

from subcortex.installers import opencode

REPO_ROOT = Path(__file__).resolve().parents[1]
TS_PATH = REPO_ROOT / "adapters" / "opencode" / "subcortex.ts"


class TempHomeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._env = mock.patch.dict(os.environ, {"HOME": self.tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.plugin_dir = Path(self.tmp.name) / ".config" / "opencode" / "plugins"
        self.dest = self.plugin_dir / "subcortex.ts"

    def write_foreign(self, name="other-plugin.ts", content="// foreign\n"):
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        foreign = self.plugin_dir / name
        foreign.write_text(content)
        return foreign


class TestInstaller(TempHomeTest):
    def test_install_status_uninstall_round_trip(self):
        result = opencode.install()
        self.assertTrue(result["installed"])
        self.assertEqual(Path(result["path"]), self.dest)
        self.assertIsNone(result["backup"])
        self.assertTrue(self.dest.is_file())
        self.assertEqual(self.dest.read_bytes(), TS_PATH.read_bytes())

        st = opencode.status()
        self.assertTrue(st["installed"])
        self.assertTrue(st["identical"])
        self.assertTrue(st["source_found"])

        out = opencode.uninstall()
        self.assertTrue(out["removed"])
        self.assertFalse(self.dest.exists())
        self.assertFalse(opencode.status()["installed"])

    def test_install_backs_up_existing_different_file(self):
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        self.dest.write_text("// old version\n")

        result = opencode.install()
        self.assertTrue(result["installed"])
        backup = Path(result["backup"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(), "// old version\n")
        self.assertEqual(self.dest.read_bytes(), TS_PATH.read_bytes())
        self.assertEqual(opencode.status()["backup"], str(backup))

    def test_install_identical_existing_creates_no_backup(self):
        opencode.install()
        result = opencode.install()
        self.assertTrue(result["installed"])
        self.assertIsNone(result["backup"])
        self.assertFalse((self.plugin_dir / "subcortex.ts.bak").exists())

    def test_uninstall_leaves_foreign_files_untouched(self):
        foreign = self.write_foreign()
        opencode.install()
        opencode.uninstall()
        self.assertTrue(foreign.is_file())
        self.assertEqual(foreign.read_text(), "// foreign\n")
        self.assertFalse(self.dest.exists())

    def test_uninstall_when_not_installed(self):
        out = opencode.uninstall()
        self.assertFalse(out["removed"])

    def test_status_not_installed(self):
        st = opencode.status()
        self.assertFalse(st["installed"])
        self.assertFalse(st["identical"])
        self.assertTrue(st["source_found"])
        self.assertIsNone(st["backup"])


class TestTsContract(unittest.TestCase):
    """Static contract checks on adapters/opencode/subcortex.ts — the file is
    never executed, only verified structurally."""

    @classmethod
    def setUpClass(cls):
        cls.ts = TS_PATH.read_text()

    def test_adapter_source_resolvable_by_installer(self):
        self.assertEqual(opencode._adapter_source(), TS_PATH)

    def test_plugin_shape(self):
        self.assertIn('from "@opencode-ai/plugin"', self.ts)
        self.assertIn("Plugin", self.ts)
        self.assertRegex(self.ts, r"export\s+const\s+Subcortex\s*:\s*Plugin\s*=\s*async")

    def test_hooks_present(self):
        self.assertIn('"chat.message"', self.ts)
        self.assertIn('"tool.execute.after"', self.ts)
        self.assertIn('"experimental.session.compacting"', self.ts)

    def test_fail_open_structure(self):
        self.assertIn("AbortController", self.ts)
        self.assertIn("controller.abort()", self.ts)
        self.assertIn("catch", self.ts)
        # every fetch call sits inside a try block in the same function
        for m in re.finditer(r"await fetch\(", self.ts):
            prefix = self.ts[: m.start()]
            self.assertGreater(prefix.rfind("try {"), prefix.rfind("function"),
                               "fetch must occur inside a try block")
        # no bare throws anywhere — the plugin must never throw
        self.assertIsNone(re.search(r"^\s*throw\b", self.ts, re.MULTILINE))

    def test_config_and_thresholds(self):
        self.assertIn("SUBCORTEX_URL", self.ts)
        self.assertIn("http://127.0.0.1:7707", self.ts)
        self.assertIn("3000", self.ts)  # request timeout ms
        self.assertIn("0.8", self.ts)   # simple-confidence threshold
        self.assertIn("6000", self.ts)  # min output chars
        self.assertIn("0.3", self.ts)   # p_needed disposable threshold

    def test_mutation_markers(self):
        self.assertIn("synthetic: true", self.ts)
        self.assertIn("/verdict/prompt", self.ts)
        self.assertIn("/verdict/output", self.ts)
        self.assertIn("[subcortex: truncated", self.ts)


if __name__ == "__main__":
    unittest.main()
