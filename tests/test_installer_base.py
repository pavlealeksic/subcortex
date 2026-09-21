import json
import os
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path

import pathsetup  # noqa: F401

from subcortex.installers import base


OURS = {"type": "command", "command": "/usr/bin/subcortex-hook claude-code UserPromptSubmit"}
LEGACY = {"type": "command", "command": "/venv/bin/python -m subcortex hook claude UserPromptSubmit"}
THEIRS = {"type": "command", "command": "rtk hook claude"}


class TestCommandMarkers(unittest.TestCase):
    def test_ours_current_and_legacy(self):
        self.assertTrue(base.is_our_command(OURS["command"]))
        self.assertTrue(base.is_our_command(LEGACY["command"]))
        self.assertTrue(base.is_our_command("/x/python -m subcortex.hook codex PostToolUse"))
        self.assertFalse(base.is_our_command(THEIRS["command"]))
        self.assertFalse(base.is_our_command(None))

    def test_hook_command_is_absolute_and_guarded(self):
        cmd = base.hook_command("claude-code", "UserPromptSubmit")
        self.assertTrue(cmd.split()[0].startswith("/"))
        self.assertTrue(cmd.endswith("claude-code UserPromptSubmit 2>/dev/null || true"))


class TestGroupedTable(unittest.TestCase):
    def test_add_uses_its_own_group_is_idempotent_and_keeps_foreign_entries(self):
        table = {"PreToolUse": [{"matcher": "Bash", "hooks": [THEIRS]}]}
        self.assertTrue(base.add_grouped(table, "PreToolUse", "Bash", OURS))
        self.assertFalse(base.add_grouped(table, "PreToolUse", "Bash", OURS))
        self.assertEqual(table["PreToolUse"], [{"matcher": "Bash", "hooks": [THEIRS]},
                                               {"matcher": "Bash", "hooks": [OURS]}])

    def test_new_group_without_matcher(self):
        table = {}
        base.add_grouped(table, "UserPromptSubmit", None, OURS)
        self.assertEqual(table, {"UserPromptSubmit": [{"hooks": [OURS]}]})

    def test_remove_prunes_only_what_we_emptied(self):
        table = {
            "UserPromptSubmit": [{"hooks": [LEGACY]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [THEIRS, OURS]}],
            "Stop": [{"hooks": [THEIRS]}],
            "PreCompact": [],
        }
        removed = base.remove_grouped(table)
        self.assertEqual(sorted(removed), ["PreToolUse", "UserPromptSubmit"])
        self.assertEqual(table, {
            "PreToolUse": [{"matcher": "Bash", "hooks": [THEIRS]}],
            "Stop": [{"hooks": [THEIRS]}],
            "PreCompact": [],
        })


class TestFlatTable(unittest.TestCase):
    def test_add_remove(self):
        table = {"beforeSubmitPrompt": [{"command": "other"}]}
        self.assertTrue(base.add_flat(table, "beforeSubmitPrompt", {"command": "subcortex-hook cursor x"}))
        self.assertFalse(base.add_flat(table, "beforeSubmitPrompt", {"command": "subcortex-hook cursor x"}))
        base.add_flat(table, "afterShellExecution", {"command": "subcortex-hook cursor y"})
        self.assertEqual(sorted(base.remove_flat(table)), ["afterShellExecution", "beforeSubmitPrompt"])
        self.assertEqual(table, {"beforeSubmitPrompt": [{"command": "other"}]})

    def test_custom_command_keys(self):
        table = {}
        base.add_flat(table, "userPromptSubmitted", {"bash": "subcortex-hook copilot x"}, ("bash",))
        self.assertEqual(base.remove_flat(table, ("bash",)), ["userPromptSubmitted"])


class TestMarkedBlock(unittest.TestCase):
    USER = 'model = "gpt-5"\n\n[mcp_servers.x]\ncommand = "x"\n'

    def test_append_strip_round_trip_preserves_foreign_bytes(self):
        merged = base.append_block(self.USER, '[hooks]\nfoo = 1\n', "codex")
        tomllib.loads(merged)
        self.assertTrue(base.has_block(merged))
        self.assertEqual(base.strip_block(merged), self.USER)

    def test_append_replaces_existing_block(self):
        once = base.append_block(self.USER, "a = 1", "codex")
        twice = base.append_block(once, "a = 2", "codex")
        self.assertEqual(twice.count(">>> subcortex"), 1)
        self.assertIn("a = 2", twice)

    def test_empty_file(self):
        merged = base.append_block("", "a = 1", "kimi")
        self.assertTrue(merged.startswith("# >>> subcortex"))
        self.assertEqual(base.strip_block(merged), "")


class TestUserBytesSurvive(unittest.TestCase):
    def test_strip_block_touches_nothing_but_the_block(self):
        user = 'rules = """Rule 1.\n\n\n\nRule 2."""\n\n\n[profiles.work]\nmodel = "x"\n'
        merged = base.append_block(user, "a = 1", "kimi")
        self.assertEqual(base.strip_block(merged), user)
        middle = user + base.append_block("", "a = 1", "kimi") + "\n[after]\nkept = true\n"
        self.assertEqual(base.strip_block(middle), user + "\n[after]\nkept = true\n")

    def test_a_block_without_its_end_marker_is_refused_not_truncated(self):
        broken = "a = 1\n# >>> subcortex (managed)\nb = 2\n[profiles.work]\nmodel = 'keep me'\n"
        with self.assertRaises(base.InstallError):
            base.strip_block(broken)
        from subcortex.installers import goose

        with self.assertRaises(base.InstallError):
            goose.strip_entry("extensions:\n  # >>> subcortex (managed)\n  x: 1\nother: keep\n")

    def test_symlinked_config_stays_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp, "dotfiles", "settings.json")
            real.parent.mkdir()
            real.write_text('{"model": "opus"}')
            link = Path(tmp, "settings.json")
            link.symlink_to(real)
            base.atomic_write(link, '{"model": "opus", "hooks": {}}')
            self.assertTrue(link.is_symlink())
            self.assertEqual(json.loads(real.read_text()), {"model": "opus", "hooks": {}})


class TestFiles(unittest.TestCase):
    def test_atomic_write_keeps_mode_and_backup_is_timestamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("{}")
            os.chmod(path, 0o600)
            first = base.backup(path)
            second = base.backup(path)
            self.assertNotEqual(first, second)
            self.assertIn(".subcortex-", first.name)
            base.atomic_write(path, '{"a": 1}')
            self.assertEqual(json.loads(path.read_text()), {"a": 1})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir() if p.name.startswith(".")), [])

    def test_load_json_object_refuses_jsonc_and_non_objects(self):
        with self.assertRaises(base.InstallError):
            base.load_json_object('{"a": 1, // comment\n}', Path("x.json"))
        with self.assertRaises(base.InstallError):
            base.load_json_object("[1, 2]", Path("x.json"))
        self.assertEqual(base.load_json_object("  ", Path("x.json")), {})


if __name__ == "__main__":
    unittest.main()
