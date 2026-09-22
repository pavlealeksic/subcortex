"""Daemon lifecycle with real processes: autostart, stop, SIGTERM, service units."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pathsetup  # noqa: F401

from subcortex import cli, localhttp, provision, service

SRC = str(Path(__file__).resolve().parents[1] / "src")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data = root / "data"
        self.port = free_port()
        (root / "config.json").write_text("{}")
        self.env = dict(os.environ, SUBCORTEX_DATA_DIR=str(self.data), SUBCORTEX_PORT=str(self.port),
                        SUBCORTEX_CONFIG=str(root / "config.json"), PYTHONPATH=SRC)
        self.patch = mock.patch.dict(os.environ, {k: self.env[k] for k in
                                                  ("SUBCORTEX_DATA_DIR", "SUBCORTEX_PORT", "SUBCORTEX_CONFIG")})
        self.patch.start()

    def tearDown(self):
        self.stop_daemon()
        self.patch.stop()
        self.tmp.cleanup()

    def health(self):
        try:
            return localhttp.request(self.port, "GET", "/health", timeout=1)[1]
        except OSError:
            return None

    def wait_health(self, seconds=30):
        deadline = time.time() + seconds
        while time.time() < deadline:
            health = self.health()
            if health:
                return health
            time.sleep(0.2)
        return None

    def diagnostics(self):
        """What the daemon said, for a failure message."""
        parts = []
        for name in ("daemon.log", "daemon.stderr"):
            path = self.data / name if name == "daemon.log" else Path(self.tmp.name, name)
            try:
                parts.append(f"--- {name}:\n{path.read_text()[-3000:]}")
            except OSError:
                parts.append(f"--- {name}: (none)")
        return "\n".join(parts)

    def stop_daemon(self):
        """Whatever a test started must not outlive it (health pid or sandbox PID file)."""
        pids = set()
        health = self.health()
        if health and isinstance(health.get("pid"), int):
            pids.add(health["pid"])
        try:
            pids.add(int((self.data / "daemon.pid").read_text().strip()))
        except (OSError, ValueError):
            pass
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        for _ in range(50):
            if not self.health():
                break
            time.sleep(0.1)


class TestAutostart(Sandbox):
    def test_a_project_with_its_own_json_py_neither_breaks_nor_runs_in_the_daemon(self):
        project = Path(self.tmp.name, "project")
        project.mkdir()
        marker = Path(self.tmp.name, "PWNED")
        for name in ("json.py", "socket.py", "fcntl.py"):
            (project / name).write_text(f"open({str(marker)!r}, 'w').write('ran')\nraise SystemExit(3)\n")
        payload = json.dumps({"session_id": "s", "transcript_path": "/tmp/x.jsonl",
                              "hook_event_name": "UserPromptSubmit", "prompt": "hi"})
        env = dict(self.env, SUBCORTEX_AUTOSTART="1", PYTHONPATH=f"{project}{os.pathsep}{SRC}")
        # The installed hook command (python -I -m subcortex.hook), run from inside the project.
        proc = subprocess.run([sys.executable, "-I", "-m", "subcortex.hook", "claude-code", "UserPromptSubmit"],
                              input=payload, capture_output=True, text=True, cwd=project, timeout=30, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNotNone(self.wait_health(), "the autostarted daemon never came up\n" + self.diagnostics())
        self.assertFalse(marker.exists(), "code from the project ran inside subcortex")


class TestStop(Sandbox):
    def start(self):
        stderr = open(Path(self.tmp.name, "daemon.stderr"), "wb")
        self.addCleanup(stderr.close)
        proc = subprocess.Popen(provision.daemon_argv(sys.executable), cwd=self.tmp.name, env=self.env,
                                stdout=stderr, stderr=subprocess.STDOUT)
        self.assertIsNotNone(self.wait_health(), f"daemon never came up (exit {proc.poll()})\n"
                             + self.diagnostics())
        return proc

    def test_sigterm_exits_cleanly_and_removes_the_pid_file(self):
        proc = self.start()
        pid_file = self.data / "daemon.pid"
        self.assertTrue(pid_file.exists())
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertFalse(pid_file.exists())

    def test_stop_never_signals_a_process_it_cannot_confirm(self):
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.data.mkdir(parents=True, exist_ok=True)
            (self.data / "daemon.pid").write_text(str(bystander.pid))  # stale PID, reused by someone else
            self.assertEqual(cli._stop_daemon(), 0)
            time.sleep(0.2)
            self.assertIsNone(bystander.poll(), "an unrelated process was killed")
            self.assertFalse((self.data / "daemon.pid").exists())
        finally:
            bystander.kill()
            bystander.wait()

    def test_stop_stops_the_real_daemon(self):
        proc = self.start()
        self.assertEqual(cli._stop_daemon(), 0)
        self.assertEqual(proc.wait(timeout=10), 0)


class TestCurrentCodeOnly(unittest.TestCase):
    def test_an_older_subcortex_in_the_backend_venv_is_never_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            venv = Path(tmp, "venv")
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=60)
            site = next(venv.glob("lib/python*/site-packages"))
            (site / "subcortex").mkdir()
            (site / "subcortex" / "__init__.py").write_text('__version__ = "0.0.1-stale"\n')
            argv = provision.daemon_argv(str(venv / "bin" / "python"))
            self.assertEqual(argv[1], "-I")
            prefix = argv[-1].split("from subcortex.cli")[0]
            probe = prefix + "import subcortex.policy, sys; print(subcortex.__version__, subcortex.__file__)"
            out = subprocess.run([argv[0], "-I", "-c", probe], capture_output=True, text=True, timeout=60,
                                 cwd=tmp).stdout
        from subcortex import __version__

        self.assertTrue(out.startswith(__version__ + " "), out)
        self.assertNotIn(str(site), out)


class TestServiceUnits(unittest.TestCase):
    def test_units_start_the_daemon_isolated_from_the_data_dir(self):
        for kind in ("launchd", "systemd"):
            with mock.patch.object(service, "platform_kind", return_value=kind):
                unit = service.render("/opt/venv/bin/python")
            self.assertIn("-I", unit, kind)
            self.assertNotIn("PYTHONPATH", unit, kind)
            self.assertIn("WorkingDirectory", unit, kind)


if __name__ == "__main__":
    unittest.main()
