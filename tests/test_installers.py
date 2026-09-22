"""Installer round trips for every TUI, in a throwaway HOME."""

import json
import os
import shlex
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import pathsetup  # noqa: F401

from subcortex import installers
from subcortex.installers import base

ISOLATION_VARS = ("XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPILOT_HOME", "KIMI_CODE_HOME",
                  "GEMINI_CLI_HOME", "QWEN_HOME", "CRUSH_GLOBAL_CONFIG", "GOOSE_PATH_ROOT",
                  "OPENHANDS_PERSISTENCE_DIR", "FACTORY_HOME_OVERRIDE")
FOREIGN_HOOK = {"type": "command", "command": "rtk hook claude"}


def foreign_content(path: Path) -> str:
    """Pre-existing user content for a target file (in the installers' own format)."""
    if "subcortex" in path.stem:
        return ""  # files subcortex owns outright (plugins, hook drop-ins)
    if path.suffix == ".json":
        if path.name == "hooks.json" and ".cursor" in path.parts:
            data = {"version": 1, "hooks": {"stop": [{"command": "echo mine"}]}}
        elif path.name == "hooks.json" and ".factory" in path.parts:
            data = {"PreToolUse": [{"matcher": "Execute", "hooks": [FOREIGN_HOOK]}]}
        elif path.name == "hooks.json" and ".openhands" in path.parts:
            data = {"stop": [{"matcher": "*", "hooks": [FOREIGN_HOOK]}]}
        else:
            data = {"theme": "dark", "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [FOREIGN_HOOK]}]},
                    "mcpServers": {"other": {"command": "x", "args": []}}}
            if path.name in ("crush.json",):
                data = {"mcp": {"other": {"type": "stdio", "command": "x"}}, "options": {"debug": False}}
        return json.dumps(data, indent=2) + "\n"
    if path.suffix == ".toml":
        return 'model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "x"\n'
    if path.suffix == ".yaml":
        return "GOOSE_MODEL: x\nextensions:\n  developer:\n    enabled: true\n    type: builtin\n"
    return ""  # plugin files / owned files: nothing foreign to preserve


class InstallerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        env = {"HOME": str(self.home)}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        for var in ISOLATION_VARS:
            os.environ.pop(var, None)
        self.cwd = os.getcwd()
        os.chdir(self.home)

    def tearDown(self):
        os.chdir(self.cwd)
        self.env.stop()
        self.tmp.cleanup()

    def install(self, installer, **kw):
        return installer.install(run_self_test=False, check_version=False, **kw)


class TestVersionDetection(unittest.TestCase):
    """Versions come from the installation; TUI binaries are never executed."""

    def make(self, root, rel, content="#!/bin/sh\n"):
        path = Path(root, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o755)
        return path

    def test_layouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            npm = self.make(tmp, "npm/lib/node_modules/@openai/codex/bin/codex.js")
            Path(tmp, "npm/lib/node_modules/@openai/codex/package.json").write_text(
                json.dumps({"name": "@openai/codex", "version": "0.140.1", "bin": {"codex": "bin/codex.js"}}))
            link = Path(tmp, "npm/bin/codex")
            link.parent.mkdir(parents=True)
            link.symlink_to(npm)
            self.assertEqual(base.installed_version(str(link)), "@openai/codex 0.140.1")
            cellar = self.make(tmp, "opt/homebrew/Cellar/codex/0.34.0/bin/codex")
            self.assertEqual(base.installed_version(str(cellar)), "0.34.0")
            versioned = self.make(tmp, "share/claude/versions/2.1.278")
            self.assertEqual(base.installed_version(str(versioned)), "2.1.278")
            tool = self.make(tmp, "uv/tools/kimi-cli/bin/kimi")
            info = Path(tmp, "uv/tools/kimi-cli/lib/python3.12/site-packages/kimi_cli-1.51.0.dist-info")
            info.mkdir(parents=True)
            (info / "entry_points.txt").write_text("[console_scripts]\nkimi = kimi_cli.cli:main\n")
            self.assertEqual(base.installed_version(str(tool)), "kimi_cli 1.51.0")
            self.assertIn("legacy", installers.get_installer("kimi-code").version_problem("kimi_cli 1.51.0"))
            self.assertIsNone(base.installed_version(str(self.make(tmp, "home/.local/bin/droid"))))

    def test_the_tui_is_never_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp, "EXECUTED")
            fake = self.make(tmp, "bin/codex", f"#!/bin/sh\ntouch {marker}\necho codex-cli 0.1.0\n")
            installer = installers.get_installer("codex")
            with mock.patch.object(installer, "detected", return_value=str(fake)):
                problem, warning = installer.check_version()
            self.assertFalse(marker.exists(), "a TUI binary was executed to read its version")
            self.assertIsNone(problem)
            self.assertIn("without running it", warning)


class TestGooseRewritesItsConfig(InstallerCase):
    """Goose re-serializes config.yaml (dropping comments, i.e. our markers)."""

    def test_an_unmarked_entry_is_still_ours(self):
        installer = installers.get_installer("goose")
        self.install(installer)
        config = installer.targets()[0].path
        text = config.read_text()
        cmd = next(line for line in text.splitlines() if "cmd:" in line)
        rewritten = ("GOOSE_PROVIDER: openai\nextensions:\n  myext:\n    enabled: true\n    type: builtin\n"
                     f"  subcortex:\n    enabled: true\n{cmd}\n    type: stdio\nGOOSE_MODEL: gpt-5\n")
        config.write_text(rewritten)  # what Goose writes back: same entry, no markers
        self.assertTrue(installer.status()["installed"])
        again = self.install(installer)
        self.assertTrue(again.ok, again.messages)
        keys = [l for l in config.read_text().splitlines() if l.strip() == "subcortex:"]
        self.assertEqual(len(keys), 1)  # never a duplicate key (Goose would reset the whole file)
        self.assertIn("myext:", config.read_text())
        installer.uninstall()
        after = config.read_text()
        self.assertNotIn("subcortex", after)
        for kept in ("GOOSE_PROVIDER: openai", "myext:", "GOOSE_MODEL: gpt-5"):
            self.assertIn(kept, after)


class TestConcurrentEdits(InstallerCase):
    def test_a_change_the_tui_makes_during_install_is_kept(self):
        installer = installers.get_installer("claude-code")
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"model": "sonnet"}))

        def tui_writes_meanwhile():  # e.g. /model and a permission rule, mid-install
            settings.write_text(json.dumps({"model": "opus", "permissions": {"allow": ["Bash(ls)"]}}))
            return []
        with mock.patch.object(installer, "self_test", tui_writes_meanwhile):
            result = installer.install(run_self_test=True, check_version=False)
        self.assertTrue(result.ok, result.messages)
        data = json.loads(settings.read_text())
        self.assertEqual(data["model"], "opus")
        self.assertEqual(data["permissions"], {"allow": ["Bash(ls)"]})
        self.assertIn("UserPromptSubmit", data["hooks"])


class TestRoundTrips(InstallerCase):
    def test_every_installer_round_trips_and_preserves_foreign_content(self):
        for name in installers.names():
            with self.subTest(tui=name):
                os.chdir(self.cwd)
                shutil.rmtree(self.home, ignore_errors=True)
                self.home.mkdir()
                os.chdir(self.home)
                installer = installers.get_installer(name, mcp=True)
                originals = {}
                for target in installer.targets():
                    content = foreign_content(target.path)
                    if content:
                        target.path.parent.mkdir(parents=True, exist_ok=True)
                        target.path.write_text(content)
                    originals[target.path] = content

                result = self.install(installer)
                self.assertTrue(result.ok, result.messages)
                self.assertTrue(installer.status()["installed"], name)
                for path, content in originals.items():
                    after = path.read_text()
                    self.assertTrue("subcortex" in after, f"{name}: {path} has no subcortex entry")
                    if path.suffix == ".json" and content:
                        before, now = json.loads(content), json.loads(after)
                        for key, value in before.items():
                            if key not in ("hooks", "mcpServers", "mcp") and not key[0].isupper():
                                self.assertEqual(now.get(key), value, f"{name}: lost {key}")
                    if path.suffix == ".toml":
                        tomllib.loads(after)

                again = self.install(installer)
                self.assertFalse(again.changed, f"{name}: reinstall changed files")

                self.assertTrue(installer.uninstall().ok)
                for path, content in originals.items():
                    if content:
                        restored = path.read_text()
                        if path.suffix == ".json":
                            self.assertEqual(json.loads(restored), json.loads(content), f"{name}: {path}")
                        else:
                            self.assertEqual(restored, content, f"{name}: {path}")
                    else:
                        self.assertFalse(path.exists(), f"{name}: left {path} behind")
                self.assertFalse(installer.status()["installed"], name)

    def test_installs_are_backed_up(self):
        installer = installers.get_installer("claude-code")
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(foreign_content(settings))
        result = self.install(installer)
        self.assertEqual(len(result.backups), 1)
        self.assertEqual(Path(result.backups[0]).read_text(), foreign_content(settings))

    def test_dry_run_writes_nothing(self):
        installer = installers.get_installer("gemini-cli", mcp=True)
        result = installer.install(dry_run=True, run_self_test=False, check_version=False)
        self.assertTrue(result.ok)
        self.assertFalse((self.home / ".gemini" / "settings.json").exists())


class TestSafetyRefusals(InstallerCase):
    def test_jsonc_and_invalid_files_are_refused_not_rewritten(self):
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{\n  // my comment\n  "theme": "dark"\n}\n')
        result = self.install(installers.get_installer("claude-code"))
        self.assertFalse(result.ok)
        self.assertIn("not plain JSON", " ".join(result.messages))
        self.assertIn("// my comment", settings.read_text())

    def test_foreign_plugin_file_is_never_overwritten(self):
        plugin = self.home / ".config" / "opencode" / "plugins" / "subcortex.ts"
        plugin.parent.mkdir(parents=True)
        plugin.write_text("// someone else's plugin\n")
        result = self.install(installers.get_installer("opencode"))
        self.assertFalse(result.ok)
        self.assertEqual(plugin.read_text(), "// someone else's plugin\n")
        installers.get_installer("opencode").uninstall()
        self.assertTrue(plugin.exists())

    def test_version_gate_refuses_old_binaries(self):
        installer = installers.get_installer("codex")
        with mock.patch.object(type(installer), "check_version", return_value=("too old", None)):
            result = installer.install(run_self_test=False)
        self.assertFalse(result.ok)
        self.assertIn("too old", result.messages)
        self.assertFalse((self.home / ".codex" / "hooks.json").exists())

    def test_version_parsing(self):
        codex = installers.get_installer("codex")
        self.assertIsNotNone(codex.version_problem("codex-cli 0.34.0"))
        self.assertIsNone(codex.version_problem("codex-cli 0.155.1"))
        kimi = installers.get_installer("kimi-code")
        self.assertIn("legacy", kimi.version_problem("kimi, version 1.51.0"))
        self.assertIsNone(kimi.version_problem("2.0.2"))
        self.assertIsNone(installers.get_installer("copilot").version_problem("GitHub Copilot CLI 1.0.87."))


class TestLegacyCleanup(InstallerCase):
    def test_0_1_0_claude_entries_are_removed(self):
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        legacy = "/venv/bin/python -m subcortex hook claude UserPromptSubmit"
        settings.write_text(json.dumps({"hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": legacy, "timeout": 10}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [FOREIGN_HOOK]}]}}))
        installers.get_installer("claude-code").uninstall()
        data = json.loads(settings.read_text())
        self.assertEqual(data, {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [FOREIGN_HOOK]}]}})

    def test_0_1_0_codex_block_is_removed_on_install(self):
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('model = "gpt-5"\n\n# >>> subcortex\n[[hooks.PostToolUse]]\nmatcher = "Bash"\n'
                          '[[hooks.PostToolUse.hooks]]\ntype = "command"\n'
                          'command = "/x/python -m subcortex hook codex PostToolUse"\n# <<< subcortex\n')
        self.install(installers.get_installer("codex"))
        self.assertEqual(config.read_text(), 'model = "gpt-5"\n')
        self.assertTrue((self.home / ".codex" / "hooks.json").is_file())



class TestDoctorSupport(InstallerCase):
    def test_installed_executables_are_found_and_dangling_ones_reported(self):
        installer = installers.get_installer("claude-code")
        self.install(installer)
        exes = installer.installed_executables()
        self.assertEqual(len(exes), 1)
        self.assertEqual(exes, [sys.executable])
        settings = self.home / ".claude" / "settings.json"
        settings.write_text(settings.read_text().replace(exes[0], "/gone/venv/bin/python"))
        self.assertEqual(installer.installed_executables(), ["/gone/venv/bin/python"])

    def test_hook_commands_are_isolated_from_the_users_python_environment(self):
        cmd = base.hook_command("claude-code", "PostToolUse")
        self.assertEqual(shlex.split(cmd.replace(base.SHELL_GUARD, ""))[1:],
                         ["-I", "-m", "subcortex.hook", "claude-code", "PostToolUse"])

def _hook_script_available() -> bool:
    return (Path(sys.executable).parent / base.HOOK_SCRIPT).exists() or shutil.which(base.HOOK_SCRIPT)


@unittest.skipUnless(_hook_script_available(), "needs `pip install -e .` so subcortex-hook exists")
class TestSelfTests(InstallerCase):
    def test_every_hook_installer_passes_its_self_test(self):
        for name in installers.names():
            installer = installers.get_installer(name)
            if not installer.hook_events():
                continue
            with self.subTest(tui=name):
                self.assertEqual(installer.self_test(), [])


if __name__ == "__main__":
    unittest.main()
