"""Interactive setup: the UI toolkit, the wizard (line-input mode), and a real
pseudo-terminal session driven with arrow keys."""

import argparse
import io
import json
import os
import re
import select
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pathsetup  # noqa: F401

from subcortex import config, service, wizard
from subcortex.ui import CANCEL, DOWN, ENTER, SPACE, TOGGLE_ALL, UP, UI, Cancelled


def line_ui(text):
    out = io.StringIO()
    return UI(stdin=io.StringIO(text), stdout=out, interactive=False, color=False), out


def key_ui(keys):
    out = io.StringIO()
    return UI(stdin=io.StringIO(""), stdout=out, interactive=True, color=False, keys=iter(keys)), out


OPTIONS = [("a", "Alpha", ""), ("b", "Beta", "hint"), ("c", "Gamma", "")]


class TestLineMode(unittest.TestCase):
    def test_confirm(self):
        self.assertTrue(line_ui("y\n")[0].confirm("ok?", default=False))
        self.assertFalse(line_ui("no\n")[0].confirm("ok?", default=True))
        self.assertTrue(line_ui("\n")[0].confirm("ok?", default=True))
        self.assertFalse(line_ui("")[0].confirm("ok?", default=False))  # EOF -> default
        self.assertTrue(line_ui("maybe\nyes\n")[0].confirm("ok?", default=False))

    def test_choose(self):
        self.assertEqual(line_ui("2\n")[0].choose("pick", OPTIONS), "b")
        self.assertEqual(line_ui("\n")[0].choose("pick", OPTIONS, default=2), "c")
        self.assertEqual(line_ui("9\nx\n1\n")[0].choose("pick", OPTIONS), "a")

    def test_checklist(self):
        self.assertEqual(line_ui("\n")[0].checklist("pick", OPTIONS, {"b"}), ["b"])
        self.assertEqual(line_ui("1 3\n\n")[0].checklist("pick", OPTIONS, {"b"}), ["a", "b", "c"])
        self.assertEqual(line_ui("2\n\n")[0].checklist("pick", OPTIONS, {"b"}), [])
        self.assertEqual(line_ui("all\n\n")[0].checklist("pick", OPTIONS, set()), ["a", "b", "c"])
        self.assertEqual(line_ui("none\n\n")[0].checklist("pick", OPTIONS, {"a"}), [])

    def test_ask_with_validation_and_default(self):
        ui, _ = line_ui("bad\ngood\n")
        self.assertEqual(ui.ask("v", validate=lambda v: None if v == "good" else "nope"), "good")
        self.assertEqual(line_ui("\n")[0].ask("v", default="d"), "d")
        with self.assertRaises(Cancelled):  # EOF on an invalid default: don't loop forever
            line_ui("")[0].ask("v", validate=lambda v: "never valid")


class TestKeyMode(unittest.TestCase):
    def test_choose_with_arrows(self):
        self.assertEqual(key_ui([DOWN, DOWN, ENTER])[0].choose("pick", OPTIONS), "c")
        self.assertEqual(key_ui([UP, ENTER])[0].choose("pick", OPTIONS), "c")  # wraps

    def test_checklist_toggles(self):
        ui, out = key_ui([SPACE, DOWN, DOWN, SPACE, ENTER])
        self.assertEqual(ui.checklist("pick", OPTIONS, {"b"}), ["a", "b", "c"])
        self.assertIn("◉", out.getvalue())
        self.assertEqual(key_ui([TOGGLE_ALL, TOGGLE_ALL, ENTER])[0].checklist("pick", OPTIONS, {"a"}), [])

    def test_cancel(self):
        with self.assertRaises(Cancelled):
            key_ui([DOWN, CANCEL])[0].choose("pick", OPTIONS)

    def test_long_menus_scroll(self):
        many = [(i, f"item {i}", "") for i in range(30)]
        ui, out = key_ui([DOWN] * 20 + [SPACE, ENTER])
        with mock.patch.object(UI, "_max_visible", return_value=6):
            self.assertEqual(ui.checklist("pick", many, set()), [20])
        frames = out.getvalue()
        self.assertIn("↓ 24 more", frames)  # first frame: rows 0-5 shown
        self.assertIn("↑ 15 more", frames)  # after scrolling to row 20
        self.assertNotIn("item 29", frames.split("\x1b[")[1])  # never drew the whole list at once


class WizardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
            "SUBCORTEX_CONFIG": str(self.home / "subcortex.json"),
            "SUBCORTEX_DATA_DIR": str(self.home / "data"), "SUBCORTEX_PORT": "1"})
        self.env.start()
        for var in ("TYPESAFE_API_KEY", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "KIMI_CODE_HOME"):
            os.environ.pop(var, None)
        # No real daemon, network, pip or login service in unit tests.
        self.patches = [mock.patch.object(wizard.Wizard, "restart_daemon", return_value=True),
                        mock.patch.object(wizard.Wizard, "test_jev"),
                        mock.patch.object(service, "status", return_value={"supported": False})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()
        self.tmp.cleanup()

    def args(self, **kw):
        base = dict(yes=False, backend=None, model=None, tuis=None, mcp=False, service=None,
                    skip_backend_install=False, ignore_version=True)
        base.update(kw)
        return argparse.Namespace(**base)


class TestWizard(WizardCase):
    def test_interactive_line_mode_jev_flow(self):
        answers = "\n".join([
            "2",          # backend: Jev
            "",           # default endpoint: yes
            "sk-secret",  # key (line mode: not hidden)
            "",           # test decision: yes (patched)
            "2", "",      # behaviors: untoggle output trimming, accept
            "none", "",   # TUIs: clear preselection …
            "",           # (checklist redraw) accept empty
        ]) + "\n"
        ui, out = line_ui(answers)
        with mock.patch.object(wizard, "tui_rows", return_value=[]):
            code = wizard.run(self.args(), ui)
        self.assertEqual(code, 0, out.getvalue())
        cfg = config.load_config()
        self.assertEqual(cfg["backend"], "jev")
        self.assertFalse(cfg["features"]["trim_output"])
        self.assertEqual(config.read_secret("TYPESAFE_API_KEY"), "sk-secret")
        self.assertEqual(os.stat(config.secrets_path()).st_mode & 0o777, 0o600)
        self.assertNotIn("sk-secret", out.getvalue())

    def test_unattended_install_of_named_tuis(self):
        ui, out = line_ui("")
        code = wizard.run(self.args(yes=True, backend="jev", tuis=["claude-code", "cursor"]), ui)
        self.assertEqual(code, 0, out.getvalue())
        self.assertTrue((self.home / ".claude" / "settings.json").is_file())
        self.assertTrue((self.home / ".cursor" / "hooks.json").is_file())
        self.assertIn("provide the jev API key", out.getvalue())

    def test_declining_apply_writes_nothing(self):
        # jev, default endpoint, key, skip test, behaviors (accept), TUIs (accept), diffs: n, apply: n
        ui, out = line_ui("2\n\nsk\nn\n\n\nn\nn\n")
        rows = [{"name": "claude-code", "display": "Claude Code", "seam": "hooks", "detected": "/bin/claude",
                 "installed": False, "mcp": False}]
        with mock.patch.object(wizard, "tui_rows", return_value=rows):
            wizard.run(self.args(), ui)
        self.assertFalse((self.home / ".claude" / "settings.json").exists())
        self.assertIn("nothing written", out.getvalue())

    def test_cancel_is_reported(self):
        ui, _ = key_ui([CANCEL])
        ui.stdout = io.StringIO()
        self.assertEqual(wizard.run(self.args(), ui), 130)
        self.assertIn("setup cancelled", ui.stdout.getvalue())

    def test_resolve_tuis(self):
        rows = [{"name": "claude-code", "detected": "/x"}, {"name": "codex", "detected": None}]
        self.assertEqual(wizard._resolve(["detected"], rows), ["claude-code"])
        self.assertEqual(wizard._resolve(["claude,gemini", "claude-code"], rows), ["claude-code", "gemini-cli"])
        with self.assertRaises(SystemExit):
            wizard._resolve(["nope"], rows)

    def test_capabilities_summary(self):
        self.assertEqual(wizard.capabilities("claude-code"), "hint · trim · compaction")
        self.assertEqual(wizard.capabilities("junie"), "hint")
        self.assertEqual(wizard.capabilities("crush"), "on-demand tools")
        self.assertEqual(wizard.capabilities("amp"), "hint · trim · compaction")


class TestSecretsAndService(WizardCase):
    def test_secret_store(self):
        self.assertIsNone(config.read_secret("X_KEY"))
        config.save_secret("X_KEY", "v1")
        self.assertEqual(config.read_secret("X_KEY"), "v1")
        with mock.patch.dict(os.environ, {"X_KEY": "from-env"}):
            self.assertEqual(config.read_secret("X_KEY"), "from-env")
        self.assertTrue(config.delete_secret("X_KEY"))
        self.assertFalse(config.secrets_path().exists())

    def test_service_units_render(self):
        for kind in ("launchd", "systemd"):
            with mock.patch.object(service, "platform_kind", return_value=kind):
                text = service.render("/opt/venv/bin/python")
            self.assertIn("/opt/venv/bin/python", text)
            self.assertIn("serve", text)
            self.assertIn("--foreground", text)

    def test_service_install_uses_the_runner(self):
        calls = []

        def runner(argv):
            calls.append(list(argv))
            return mock.Mock(returncode=0, stderr="")

        with mock.patch.object(service, "platform_kind", return_value="systemd"):
            service.install("/opt/venv/bin/python", runner=runner)
            self.assertTrue(service.unit_path().is_file())
            service.uninstall(runner=runner)
            self.assertFalse(service.unit_path().exists())
        self.assertIn(["systemctl", "--user", "enable", "--now", "subcortex.service"], calls)


@unittest.skipUnless(os.name == "posix", "needs a pseudo-terminal")
class TestRealTerminal(unittest.TestCase):
    """`subcortex setup` in a pty, driven with real escape sequences."""

    def test_arrow_key_session(self):
        import pty

        with tempfile.TemporaryDirectory() as home:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            env = {k: v for k, v in os.environ.items() if not k.startswith(("SUBCORTEX_", "TYPESAFE"))}
            env.update(HOME=home, XDG_CONFIG_HOME=f"{home}/.config", SUBCORTEX_PORT=str(port),
                       SUBCORTEX_AUTOSTART="0", TERM="xterm-256color",
                       PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
            env.pop("NO_COLOR", None)
            script = [
                ("Which decision backend?", "\x1b[B\r"),        # ↓ Jev
                ("Use the default endpoint", "\r"),
                ("input hidden", "sk-test\r"),
                ("Check the key", "n\r"),
                ("What should subcortex do?", "\r"),
                ("Wire subcortex into which TUIs?", "aa \r"),   # all on, all off, first on
                ("Also register the on-demand MCP tools", "\r"),  # only if that TUI has MCP too
                ("Show the full diffs?", "\r"),
                ("Apply these changes?", "\r"),
                ("Start (or restart) the daemon now?", "n\r"),
                ("Check health any time", ""),
            ]
            pid, fd = pty.fork()
            if pid == 0:
                os.execvpe(sys.executable, [sys.executable, "-m", "subcortex", "setup", "--no-service"], env)
            out = b""
            try:
                optional = {"Also register the on-demand MCP tools"}
                for i, (fragment, keys) in enumerate(script):
                    # an optional question may be skipped: stop waiting once the next one shows
                    wanted = [fragment] + ([script[i + 1][0]] if fragment in optional else [])
                    deadline = time.time() + 60
                    while not any(w.encode() in out for w in wanted) and time.time() < deadline:
                        if select.select([fd], [], [], 0.2)[0]:
                            try:
                                chunk = os.read(fd, 65536)
                            except OSError:
                                break
                            if not chunk:
                                break
                            out += chunk
                    if fragment in optional and fragment.encode() not in out:
                        continue
                    self.assertIn(fragment.encode(), out, _clean(out)[-2000:])
                    time.sleep(0.2)
                    if keys:
                        os.write(fd, keys.encode())
            finally:
                for _ in range(100):
                    if os.waitpid(pid, os.WNOHANG)[0]:
                        break
                    time.sleep(0.1)
                else:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
            text = _clean(out)
            self.assertNotIn("sk-test", text)  # hidden input
            cfg = json.loads(Path(home, ".config", "subcortex", "config.json").read_text())
            self.assertEqual(cfg["backend"], "jev")
            secrets = Path(home, ".config", "subcortex", "secrets.json")
            self.assertEqual(json.loads(secrets.read_text())["TYPESAFE_API_KEY"], "sk-test")
            self.assertIn("installed", text)


def _clean(raw: bytes) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw.decode(errors="replace")).replace("\r", "")


if __name__ == "__main__":
    unittest.main()
