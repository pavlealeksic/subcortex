"""Per-session state files, shared by hook processes and the daemon.

Many sessions of many TUIs run at once against one data dir, and a single
session's hooks can overlap (e.g. a restore on SessionStart and on the next
prompt). So every file here is:

- keyed by (TUI, session id): collision-free across TUIs and across ids that
  sanitize alike, and never able to escape its directory;
- written atomically (temp file + rename), so readers never see a torn file;
- private (dirs 0700, files 0600), since they hold prompts and conversation text;
- changed under a per-session lock (``locked``), so read-modify-write and
  consume-once operations don't race across threads or processes.

Everything fails open: errors surface as None/False/TimeoutError for the
caller to swallow, never as a changed TUI outcome.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from .config import data_dir

LOCK_WAIT_S = 1.0          # a hook has a few seconds in total; give up well before that
_LOCK_STRIPES = 64         # fixed lock files, never deleted (deleting a lock file breaks flock)
_PRUNE_EVERY_S = 600


def key(tui: Any, session_id: Any) -> Optional[str]:
    """Filename-safe, collision-free key for one TUI session (None without a session id)."""
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    sid = session_id.strip()
    ns = tui.strip() if isinstance(tui, str) and tui.strip() else "_"
    digest = hashlib.sha256(f"{ns}\0{sid}".encode("utf-8", "replace")).hexdigest()[:24]
    readable = "".join(c if c.isalnum() or c in "-_" else "_" for c in f"{ns}-{sid}")[:64]
    return f"{readable}-{digest}"


def _private(directory: Path) -> Path:
    """Create ``directory`` (and the data dir) owner-only. Only writers call this."""
    root = data_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for d in (root, directory):  # mkdir's mode is masked by umask and skipped when it exists
        try:
            if d.stat().st_mode & 0o077:
                os.chmod(d, 0o700)
        except OSError:
            pass
    return directory


def path(kind: str, tui: Any, session_id: Any) -> Optional[Path]:
    """Where this session's ``kind`` file lives. Creates nothing."""
    k = key(tui, session_id)
    return data_dir() / kind / f"{k}.json" if k else None


def write_json(target: Path, data: Dict[str, Any]) -> None:
    _private(target.parent)
    tmp = target.parent / f".tmp-{os.getpid()}-{os.urandom(6).hex()}.json"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_json(target: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@contextlib.contextmanager
def locked(k: str, wait_s: Optional[float] = None) -> Iterator[None]:
    """Exclusive per-session lock across threads and processes; TimeoutError if busy."""
    wait_s = LOCK_WAIT_S if wait_s is None else wait_s
    stripe = int(hashlib.sha256(k.encode()).hexdigest()[:8], 16) % _LOCK_STRIPES
    lock_path = _private(data_dir() / "locks") / f"{stripe:02d}.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"state lock busy: {k}") from None
                time.sleep(0.01)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def prune(kind: str, max_age_s: float) -> None:
    """Delete this kind's files older than ``max_age_s``; runs at most every few minutes."""
    try:
        directory = data_dir() / kind
        if not directory.is_dir():
            return
        stamp = directory / ".pruned"
        now = time.time()
        if stamp.exists() and now - stamp.stat().st_mtime < _PRUNE_EVERY_S:
            return
        stamp.touch()
        for old in [*directory.glob("*.json"), *directory.glob("*.claim")]:
            with contextlib.suppress(OSError):
                if now - old.stat().st_mtime > max_age_s:
                    old.unlink()
        for tmp in directory.glob(".tmp-*"):  # left behind by a killed writer
            with contextlib.suppress(OSError):
                if now - tmp.stat().st_mtime > 60:
                    tmp.unlink()
    except OSError:
        pass
