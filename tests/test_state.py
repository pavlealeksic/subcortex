"""Session state under concurrency: many TUIs, many sessions, overlapping hooks.

These are the guarantees production use depends on, exercised with real
threads and real processes rather than asserted."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import pathsetup  # noqa: F401

from subcortex import policy, state
from subcortex.backends import laya
from subcortex.config import DEFAULT_CONFIG
from subcortex.daemon import create_server

SRC = str(Path(__file__).resolve().parents[1] / "src")
MSGS = [{"role": "user", "text": "remember PELICAN-42"}]


def cfg():
    return json.loads(json.dumps(DEFAULT_CONFIG))


class StateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"SUBCORTEX_DATA_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def ready_snapshot(self, sid="s", tui="claude-code"):
        self.assertTrue(policy.save_snapshot(sid, MSGS, cfg(), tui=tui))
        self.assertTrue(policy.mark_compacted(sid, tui=tui))


class TestConsumeOnce(StateCase):
    def test_racing_threads_restore_exactly_once(self):
        for round_ in range(20):
            self.ready_snapshot(f"s{round_}")
            barrier, got = threading.Barrier(12), []

            def restore():
                barrier.wait()
                got.append(policy.restore_snapshot(f"s{round_}", cfg(), require_ready=True,
                                                   tui="claude-code"))
            threads = [threading.Thread(target=restore) for _ in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sum(1 for g in got if g), 1, f"round {round_}: {got}")

    def test_racing_processes_restore_exactly_once(self):
        self.ready_snapshot("proc")
        start = time.time() + 1.0
        script = ("import time,sys; from subcortex import policy; from subcortex.config import DEFAULT_CONFIG;"
                  f"time.sleep(max(0, {start} - time.time()));"
                  "r = policy.restore_snapshot('proc', DEFAULT_CONFIG, require_ready=True, tui='claude-code');"
                  "print('HIT' if r and 'PELICAN-42' in r else 'MISS')")
        env = dict(os.environ, PYTHONPATH=SRC, SUBCORTEX_DATA_DIR=self.tmp.name)
        procs = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=env)
                 for _ in range(8)]
        results = [p.communicate(timeout=30)[0].strip() for p in procs]
        self.assertEqual(results.count("HIT"), 1, results)

    def test_racing_daemon_requests_restore_exactly_once(self):
        server = create_server(0, cfg(), backend_factory=lambda c, name=None: None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            self.assertTrue(policy.save_snapshot("d1", MSGS, cfg(), tui="opencode"))
            barrier, got = threading.Barrier(10), []

            def restore():
                barrier.wait()
                req = urllib.request.Request(f"{url}/v1/restore", method="POST",
                                             data=json.dumps({"session_id": "d1", "tui": "opencode"}).encode(),
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    got.append(json.loads(resp.read())["context"])
            threads = [threading.Thread(target=restore) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(got), 10, "every request must be answered")
            self.assertEqual(sum(1 for g in got if g), 1, got)
        finally:
            server.shutdown()
            server.server_close()


class TestClaimThenCommit(StateCase):
    def test_nothing_is_lost_when_delivery_never_happens(self):
        self.ready_snapshot("s", tui="t")
        commits = []
        self.assertIsNotNone(policy.restore_snapshot("s", cfg(), tui="t", commits=commits))
        # The hook dies before writing: commits never run. The claim hides the
        # snapshot from racing restores for a while...
        self.assertIsNone(policy.restore_snapshot("s", cfg(), tui="t"))
        claim = state.path("compact", "t", "s").with_suffix(".claim")
        stale = time.time() - policy.CLAIM_RECOVER_S - 1
        os.utime(claim, (stale, stale))
        # ...and then it is delivered by the next restore.
        self.assertIn("PELICAN-42", policy.restore_snapshot("s", cfg(), tui="t"))
        self.assertFalse(claim.exists())

    def test_committed_claims_are_gone(self):
        self.ready_snapshot("s", tui="t")
        commits = []
        policy.restore_snapshot("s", cfg(), tui="t", commits=commits)
        for commit in commits:
            commit()
        claim = state.path("compact", "t", "s").with_suffix(".claim")
        os.utime(Path(self.tmp.name, "compact"), None)
        self.assertFalse(claim.exists())

    def test_a_hook_killed_mid_run_keeps_the_snapshot(self):
        self.ready_snapshot("sess", tui="grok-build")
        script = ("import sys, os; from subcortex import hook, policy;"
                  "real = hook.run\n"
                  "def slow(*a, **k):\n"
                  "    out = real(*a, **k); import time; time.sleep(30); return out\n"
                  "hook.run = slow; hook.main(['grok-build', 'PostToolUse'])")
        payload = json.dumps({"hookEventName": "PostToolUse", "sessionId": "sess", "toolName": "run_terminal_command",
                              "toolResult": {"type": "Bash", "output_for_prompt": "exit: 0\nok", "exit_code": 0}})
        env = dict(os.environ, PYTHONPATH=SRC, SUBCORTEX_DATA_DIR=self.tmp.name, SUBCORTEX_HOOK_BUDGET="0.5",
                   SUBCORTEX_PORT="1")
        started = time.monotonic()
        proc = subprocess.run([sys.executable, "-c", script], input=payload, capture_output=True, text=True,
                              env=env, timeout=30)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        claim = state.path("compact", "grok-build", "sess").with_suffix(".claim")
        stale = time.time() - policy.CLAIM_RECOVER_S - 1
        os.utime(claim, (stale, stale))
        self.assertIn("PELICAN-42", policy.restore_snapshot("sess", cfg(), require_ready=True, tui="grok-build"))


class TestWatchdog(unittest.TestCase):
    def test_gil_holding_work_cannot_outlive_the_budget(self):
        # A pathological regex holds the GIL, so a Python timer can't fire; the
        # C-level backstop must still end the process.
        script = ("import re; from subcortex import hook\n"
                  "hook.run = lambda *a, **k: re.match(r'(a+)+$', 'a' * 40 + 'b')\n"
                  "hook.main(['claude-code', 'UserPromptSubmit'])")
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, PYTHONPATH=SRC, SUBCORTEX_DATA_DIR=tmp, SUBCORTEX_HOOK_BUDGET="0.5")
            started = time.monotonic()
            proc = subprocess.run(["sh", "-c", f"'{sys.executable}' -c \"$S\" 2>/dev/null || true"],
                                  input="{}", capture_output=True, text=True, env=dict(env, S=script), timeout=30)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, "", ""))

    def test_invalid_utf8_on_stdin_is_still_handled(self):
        payload = json.dumps({"session_id": "s", "transcript_path": "/tmp/x.jsonl",
                              "hook_event_name": "UserPromptSubmit", "prompt": "hi"}).encode()
        payload = payload.replace(b"hi", b"hi \xff\xfe")
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, PYTHONPATH=SRC, SUBCORTEX_DATA_DIR=tmp, SUBCORTEX_PORT="1", SUBCORTEX_DEBUG="1")
            proc = subprocess.run([sys.executable, "-I", "-m", "subcortex.hook", "claude-code", "UserPromptSubmit"],
                                  input=payload, capture_output=True, env=env, timeout=30)
            self.assertEqual(proc.returncode, 0)
            log = Path(tmp, "hooks.log").read_text()
        self.assertIn(" ok ", log)  # processed, not rejected


class TestDaemonUnderLoad(StateCase):
    def test_daemon_answers_a_burst(self):
        server = create_server(0, cfg(), backend_factory=lambda c, name=None: None)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/health"
        results, barrier = [], threading.Barrier(64)

        def hit():
            barrier.wait()
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    results.append(resp.status)
            except Exception as exc:
                results.append(repr(exc))
        try:
            threads = [threading.Thread(target=hit) for _ in range(64)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(results, [200] * 64)


class TestIsolation(StateCase):
    def test_same_session_id_in_two_tuis_never_mixes(self):
        policy.remember_prompt("default", "codex task", tui="codex")
        policy.remember_prompt("default", "gemini task", tui="gemini")
        self.assertEqual(policy.last_prompt("default", tui="codex"), "codex task")
        self.assertEqual(policy.last_prompt("default", tui="gemini"), "gemini task")
        self.ready_snapshot("default", tui="codex")
        self.assertIsNone(policy.restore_snapshot("default", cfg(), tui="gemini"))
        self.assertIsNotNone(policy.restore_snapshot("default", cfg(), tui="codex"))

    def test_ids_that_sanitize_alike_stay_distinct(self):
        ids = ["a/b", "a_b", "a:b", "a b", "../a_b", "x" * 300, "x" * 301]
        paths = {state.path("tasks", "t", i) for i in ids}
        self.assertEqual(len(paths), len(ids))
        for p in paths:
            self.assertEqual(p.parent, Path(self.tmp.name, "tasks"))
        self.assertIsNone(state.path("tasks", "t", ""))
        self.assertIsNone(state.path("tasks", "t", None))

    def test_reads_create_nothing(self):
        policy.last_prompt("s", tui="x")
        policy.restore_snapshot("s", cfg(), tui="x")
        policy.mark_compacted("s", tui="x")
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])


class TestAtomicity(StateCase):
    def test_readers_never_see_a_torn_file(self):
        prompts = [f"task {i} " + "y" * (i * 997 % 1900) for i in range(40)]
        stop, bad = threading.Event(), []

        def reader():
            allowed = {p.strip() for p in prompts} | {""}
            while not stop.is_set():
                value = policy.last_prompt("s", tui="t")
                if value not in allowed:
                    bad.append(value[:40])
        readers = [threading.Thread(target=reader) for _ in range(4)]
        for t in readers:
            t.start()
        for _ in range(10):
            for p in prompts:
                policy.remember_prompt("s", p, tui="t")
        stop.set()
        for t in readers:
            t.join()
        self.assertEqual(bad, [])
        self.assertEqual(list(Path(self.tmp.name, "tasks").glob(".tmp-*")), [])

    def test_state_is_private_even_with_a_permissive_umask(self):
        old = os.umask(0o000)
        try:
            policy.remember_prompt("s", "secret prompt", tui="t")
            self.ready_snapshot("s", tui="t")
        finally:
            os.umask(old)
        for p in Path(self.tmp.name).rglob("*"):
            mode = stat.S_IMODE(p.stat().st_mode)
            if p.is_dir():
                self.assertEqual(mode, 0o700, p)
            elif p.suffix == ".json":
                self.assertEqual(mode, 0o600, p)


class TestLockFailsOpen(StateCase):
    def test_a_stuck_lock_costs_at_most_the_wait_and_changes_nothing(self):
        self.ready_snapshot("s", tui="t")
        with state.locked(state.key("t", "s")), mock.patch.object(state, "LOCK_WAIT_S", 0.2):
            started = time.monotonic()
            self.assertIsNone(policy.restore_snapshot("s", cfg(), tui="t"))
            self.assertFalse(policy.mark_compacted("s", tui="t"))
            self.assertLess(time.monotonic() - started, 2.0)
        self.assertIsNotNone(policy.restore_snapshot("s", cfg(), tui="t"))  # still there afterwards


class TestLayaSerializesInference(unittest.TestCase):
    def test_concurrent_predicts_never_overlap(self):
        active, overlaps = [0], []

        class Agent:
            def predict(self, state_, questions):
                active[0] += 1
                if active[0] > 1:
                    overlaps.append(True)
                time.sleep(0.005)
                active[0] -= 1
                return {"answers": {}}
        backend = laya.LayaBackend({"model": "multilingual"})
        backend._load = lambda alias: Agent()
        threads = [threading.Thread(target=backend.predict, args=({}, {})) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(overlaps, [])


if __name__ == "__main__":
    unittest.main()
