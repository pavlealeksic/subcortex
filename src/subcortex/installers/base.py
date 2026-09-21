"""Shared installer machinery.

An installer knows one TUI's config file and how to merge/remove subcortex's
entries in it. This module supplies everything else, identically for every TUI:

- ``plan`` computes the new file contents without touching disk (``--dry-run``
  prints its diff);
- ``install`` SELF-TESTS the exact command strings it is about to write before
  writing anything: each is executed through ``/bin/sh`` with a recorded
  payload, once with the daemon unreachable and once against an in-process
  stub daemon, and must exit 0, stay silent on stderr, emit nothing or valid
  JSON (never a blocking response), and finish within budget. Any failure
  aborts the install;
- writes are atomic, and every pre-existing file is backed up with a
  timestamp first;
- ``uninstall`` removes exactly subcortex's entries (including those written by
  pre-0.2 installers) and leaves everything else byte-identical where the
  format allows.
"""

from __future__ import annotations

import difflib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

HOOK_SCRIPT = "subcortex-hook"
# Substrings identifying a subcortex hook command, current and legacy
# ("subcortex hook <tui> <event>" was written by 0.1.0 installers).
OUR_COMMAND_MARKERS = ("subcortex-hook", "-m subcortex.hook", "subcortex hook ", "-m subcortex hook ")
SELF_TEST_TIMEOUT_S = 15.0


# Appended to every hook command. If the subcortex executable disappears
# (uninstalled package, deleted venv) the shell would exit 127 with a "not
# found" message on stderr — and some TUIs (Gemini CLI) block the prompt on
# any exit status other than 0/1. The guard keeps a dangling hook harmless.
SHELL_GUARD = " 2>/dev/null || true"


def hook_command(tui: str, event: Optional[str] = None) -> str:
    """Absolute, shell-guarded command a TUI should run for ``event``.

    Prefers the ``subcortex-hook`` console script installed next to the running
    interpreter (or on PATH); falls back to ``<python> -m subcortex.hook``.
    """
    bin_dir = Path(sys.executable).parent
    candidates = [bin_dir / HOOK_SCRIPT]
    found = shutil.which(HOOK_SCRIPT)
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            argv = [str(candidate), tui]
            break
    else:
        argv = [sys.executable, "-m", "subcortex.hook", tui]
    if event:
        argv.append(event)
    return shlex.join(argv) + SHELL_GUARD


def is_our_command(command: Any) -> bool:
    return isinstance(command, str) and any(m in command for m in OUR_COMMAND_MARKERS)


# -- results ------------------------------------------------------------------------------


@dataclass
class Plan:
    path: Path
    before: str
    after: str

    @property
    def changed(self) -> bool:
        return self.before != self.after

    def diff(self) -> str:
        return "".join(difflib.unified_diff(
            self.before.splitlines(keepends=True), self.after.splitlines(keepends=True),
            fromfile=f"{self.path} (current)", tofile=f"{self.path} (after)"))


@dataclass
class Result:
    ok: bool
    tui: str
    action: str
    paths: List[str] = field(default_factory=list)
    changed: bool = False
    backups: List[str] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "tui": self.tui, "action": self.action, "paths": self.paths,
                "changed": self.changed, "backups": self.backups, "messages": self.messages}


class InstallError(Exception):
    pass


# -- file helpers ---------------------------------------------------------------------------


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def backup(path: Path) -> Optional[Path]:
    if not path.is_file():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.name}.subcortex-{stamp}.bak")
    n = 1
    while dest.exists():
        dest = path.with_name(f"{path.name}.subcortex-{stamp}-{n}.bak")
        n += 1
    shutil.copy2(path, dest)
    return dest


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else None
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json_object(text: str, path: Path) -> Dict[str, Any]:
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise InstallError(
            f"{path} is not plain JSON ({exc}); refusing to rewrite it. "
            "Add the entries shown by --dry-run by hand.") from exc
    if not isinstance(data, dict):
        raise InstallError(f"{path} does not contain a JSON object; refusing to rewrite it.")
    return data


def dump_json(data: Dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# -- JSON hook-table helpers ------------------------------------------------------------------
#
# "Grouped" tables (Claude Code and its imitators):
#   {"hooks": {"Event": [{"matcher": "...", "hooks": [{"type": "command", "command": ...}]}]}}
# "Flat" tables (Cursor, Copilot, ...):
#   {"hooks": {"event": [{"command": ...}, ...]}}


def _entry_is_ours(entry: Any, command_keys: Tuple[str, ...]) -> bool:
    return isinstance(entry, dict) and any(is_our_command(entry.get(k)) for k in command_keys)


def add_grouped(table: Dict[str, Any], event: str, matcher: Optional[str], entry: Dict[str, Any],
                command_keys: Tuple[str, ...] = ("command",)) -> bool:
    """Add ``entry`` under ``event`` in a group of its own (never inside a
    user's group), unless one of ours is already there."""
    groups = table.get(event)
    if not isinstance(groups, list):
        groups = table[event] = []
    for group in groups:
        if isinstance(group, dict) and any(
                _entry_is_ours(h, command_keys) for h in (group.get("hooks") or [])):
            return False
    group: Dict[str, Any] = {"matcher": matcher} if matcher is not None else {}
    group["hooks"] = [entry]
    groups.append(group)
    return True


def remove_grouped(table: Dict[str, Any], command_keys: Tuple[str, ...] = ("command",)) -> List[str]:
    """Drop our entries from every event; prune groups/events we emptied."""
    removed: List[str] = []
    for event in list(table):
        groups = table.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups = []
        touched = False
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                kept = [h for h in group["hooks"] if not _entry_is_ours(h, command_keys)]
                if len(kept) != len(group["hooks"]):
                    touched = True
                    if not kept:
                        continue
                    group["hooks"] = kept
            kept_groups.append(group)
        if touched:
            removed.append(event)
            if kept_groups:
                table[event] = kept_groups
            else:
                del table[event]
    return removed


def add_flat(table: Dict[str, Any], event: str, entry: Dict[str, Any],
             command_keys: Tuple[str, ...] = ("command",)) -> bool:
    entries = table.get(event)
    if not isinstance(entries, list):
        entries = table[event] = []
    if any(_entry_is_ours(e, command_keys) for e in entries):
        return False
    entries.append(entry)
    return True


def remove_flat(table: Dict[str, Any], command_keys: Tuple[str, ...] = ("command",)) -> List[str]:
    removed: List[str] = []
    for event in list(table):
        entries = table.get(event)
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not _entry_is_ours(e, command_keys)]
        if len(kept) != len(entries):
            removed.append(event)
            if kept:
                table[event] = kept
            else:
                del table[event]
    return removed


# -- marked text blocks (TOML/YAML files we can't round-trip with the stdlib) ------------------

BLOCK_BEGIN = ">>> subcortex (managed block; remove with: subcortex uninstall {tui})"
BLOCK_END = "<<< subcortex"


def has_block(text: str, comment: str = "#") -> bool:
    return any(line.strip().startswith(f"{comment} >>> subcortex") for line in text.splitlines())


def strip_block(text: str, comment: str = "#") -> str:
    out: List[str] = []
    inside = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith(f"{comment} >>> subcortex"):
            inside = True
            continue
        if inside and stripped.startswith(f"{comment} {BLOCK_END}"):
            inside = False
            continue
        if not inside:
            out.append(line)
    cleaned = "".join(out)
    while "\n\n\n" in cleaned:
        cleaned = cleaned.replace("\n\n\n", "\n\n")
    return cleaned.strip("\n") + "\n" if cleaned.strip() else ""


def append_block(text: str, body: str, tui: str, comment: str = "#") -> str:
    base = strip_block(text, comment)
    block = (f"{comment} {BLOCK_BEGIN.format(tui=tui)}\n" + body.rstrip("\n") + "\n"
             + f"{comment} {BLOCK_END}\n")
    return (base + "\n" if base else "") + block


# -- self-test ----------------------------------------------------------------------------------


class _StubBackend:
    """Deterministic backend for the self-test: every prompt is simple, every
    output disposable, so each hook path produces a response."""

    name = "self-test"

    def predict(self, state: Any, questions: Dict[str, Any]) -> Dict[str, Any]:
        if "simple" in questions:
            return {"answers": {"simple": {"noul": 0.99}}}
        return {"answers": {"needed": {"noul": 0.01}}}

    def available(self) -> Tuple[bool, str]:
        return True, "stub"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_command(command: str, payload: Dict[str, Any], env: Dict[str, str]) -> Tuple[int, str, str, float]:
    started = time.perf_counter()
    proc = subprocess.run(["/bin/sh", "-c", command], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=SELF_TEST_TIMEOUT_S)
    return proc.returncode, proc.stdout, proc.stderr, time.perf_counter() - started


SAMPLE_TRANSCRIPT = [
    {"type": "user", "message": {"role": "user", "content": "rename getUser to fetchUser"}},
    {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Renamed in 3 files."}]}},
]
BIG_OUTPUT = "compiling module\n" * 1200  # large, successful, no failure markers


def _fill(value: Any, transcript: str) -> Any:
    """Substitute ``{transcript}`` / ``{big_output}`` placeholders in a sample payload."""
    if isinstance(value, str):
        if value == "{big_output}":
            return BIG_OUTPUT
        return value.replace("{transcript}", transcript)
    if isinstance(value, dict):
        return {k: _fill(v, transcript) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, transcript) for v in value]
    return value


def self_test(tui: str, cases: List[Tuple[str, str, Dict[str, Any]]],
              expect_output: Callable[[str], bool] = lambda event: False) -> List[str]:
    """Run each ``(event, command, payload)`` case through /bin/sh; return failures.

    Two passes, each with its own empty data dir: the daemon unreachable, then
    an in-process stub daemon. Every run must exit 0, stay silent on stderr,
    finish within budget, and print nothing or a valid, non-blocking response.
    ``expect_output(event)`` marks events that MUST respond in the stub pass
    (so a silently broken pipeline fails too). Cases run in order, so a
    PreCompact case before a SessionStart case exercises snapshot → restore.
    """
    from ..adapters import get_adapter
    from ..adapters.base import HookAdapter
    from ..config import load_config
    from ..daemon import create_server

    adapter = get_adapter(tui) or HookAdapter()
    problems: List[str] = []
    with tempfile.TemporaryDirectory(prefix="subcortex-selftest-") as tmp:
        # Pristine defaults: the user's own config must not change the verdict.
        config_file = Path(tmp) / "config.json"
        config_file.write_text("{}")
        transcript = Path(tmp) / "transcript.jsonl"
        transcript.write_text("\n".join(json.dumps(e) for e in SAMPLE_TRANSCRIPT) + "\n")
        cfg = load_config(str(config_file))
        budget = float(cfg["hooks"]["budget_s"])
        base_env = {k: v for k, v in os.environ.items()
                    if k not in ("PYTHONPATH", "SUBCORTEX_DEBUG") and not k.startswith("SUBCORTEX_")}
        base_env["SUBCORTEX_CONFIG"] = str(config_file)
        base_env["SUBCORTEX_AUTOSTART"] = "0"  # the daemon-down pass must not spawn a daemon

        def run_pass(label: str, port: int, must_respond: bool) -> None:
            env = dict(base_env, SUBCORTEX_PORT=str(port),
                       SUBCORTEX_DATA_DIR=str(Path(tmp) / label))
            for event, command, payload in cases:
                try:
                    code, out, err, secs = _run_command(command, _fill(payload, str(transcript)), env)
                except subprocess.TimeoutExpired:
                    problems.append(f"{event} ({label}): timed out after {SELF_TEST_TIMEOUT_S:.0f}s")
                    continue
                if code != 0:
                    problems.append(f"{event} ({label}): exit {code}, must always be 0: {err.strip()[:300]}")
                if err.strip():
                    problems.append(f"{event} ({label}): wrote to stderr: {err.strip()[:300]}")
                if secs > budget + 2.0:
                    problems.append(f"{event} ({label}): took {secs:.1f}s (budget {budget:.1f}s)")
                text = out.strip()
                if not text:
                    if must_respond and expect_output(event):
                        problems.append(f"{event} ({label}): no output; the hook pipeline is broken")
                    continue
                try:
                    parsed: Any = json.loads(text)
                except ValueError:
                    parsed = text  # some TUIs take plain-text stdout
                resolved = adapter.resolve(event)
                if adapter.guard(resolved[1] if resolved else "unknown", parsed) is None:
                    problems.append(f"{event} ({label}): output contains a blocking field: {text[:300]}")

        run_pass("daemon-down", _free_port(), must_respond=False)

        # The executable vanished (package uninstalled, venv deleted): the
        # guarded command must still exit 0 silently.
        env = dict(base_env, SUBCORTEX_DATA_DIR=str(Path(tmp) / "dangling"))
        for event, command, payload in cases[:1]:
            head = shlex.split(command.replace(SHELL_GUARD, ""))[0]
            dangling = command.replace(shlex.quote(head), shlex.quote(str(Path(tmp) / "missing" / "subcortex-hook")), 1)
            try:
                code, out, err, _ = _run_command(dangling, _fill(payload, str(transcript)), env)
            except subprocess.TimeoutExpired:
                problems.append("dangling executable: timed out")
                continue
            if code != 0 or err.strip() or out.strip():
                problems.append(f"dangling executable: exit {code}, stderr {err.strip()[:200]!r}, "
                                f"stdout {out.strip()[:200]!r} (must be 0 / silent)")
        server = create_server(0, cfg, backend_factory=lambda c, name=None: _StubBackend())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            run_pass("stub-daemon", server.server_address[1], must_respond=True)
        finally:
            server.shutdown()
            server.server_close()
    return problems


# -- installer base class -------------------------------------------------------------------


@dataclass
class Target:
    """One config file an installer edits."""

    path: Path
    merge: Callable[[str], str]           # contents with our entries added (idempotent)
    unmerge: Callable[[str], str]         # contents with exactly our entries removed
    is_installed: Callable[[str], bool]


def json_target(path: Path, add: Callable[[Dict[str, Any]], None],
                remove: Callable[[Dict[str, Any]], None],
                installed: Callable[[Dict[str, Any]], bool]) -> Target:
    """Target for a plain-JSON config file edited through dict callbacks.

    ``add`` must be idempotent (typically: remove ours, then add fresh);
    ``remove`` must leave the file otherwise untouched. Files that are not
    plain JSON (comments, trailing commas) are refused, never rewritten.
    """

    def merge(text: str) -> str:
        data = load_json_object(text, path)
        remove(data)
        add(data)
        return dump_json(data)

    def unmerge(text: str) -> str:
        if not text.strip():
            return text
        data = load_json_object(text, path)
        before = json.dumps(data, sort_keys=True)
        remove(data)
        if json.dumps(data, sort_keys=True) == before:
            return text  # nothing of ours: leave the bytes alone
        return dump_json(data) if data else ""

    def is_installed(text: str) -> bool:
        try:
            return installed(load_json_object(text, path))
        except InstallError:
            return False

    return Target(path, merge, unmerge, is_installed)


def mcp_json_target(path: Path, argv: List[str], key: str = "mcpServers",
                    extra: Optional[Dict[str, Any]] = None) -> Target:
    """``{key: {"subcortex": {"command": ..., "args": [...]}}}`` in a JSON file."""
    entry = {"command": argv[0], "args": argv[1:], **(extra or {})}

    def add(data: Dict[str, Any]) -> None:
        servers = data.get(key)
        if not isinstance(servers, dict):
            servers = data[key] = {}
        servers["subcortex"] = entry

    def remove(data: Dict[str, Any]) -> None:
        servers = data.get(key)
        if isinstance(servers, dict) and "subcortex" in servers:
            del servers["subcortex"]
            if not servers:
                del data[key]

    def installed(data: Dict[str, Any]) -> bool:
        return isinstance(data.get(key), dict) and "subcortex" in data[key]

    return json_target(path, add, remove, installed)


def toml_str(value: str) -> str:
    """TOML basic string."""
    return json.dumps(value, ensure_ascii=False)  # JSON string escapes are valid TOML


def toml_block_target(path: Path, body: str, tui: str) -> Target:
    """Our TOML appended as a marked block; the merged file must parse."""
    import tomllib

    def check(text: str) -> str:
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise InstallError(f"{path}: result would not be valid TOML ({exc}); refusing to write") from exc
        return text

    def merge(text: str) -> str:
        if text.strip():
            try:
                tomllib.loads(text)
            except tomllib.TOMLDecodeError as exc:
                raise InstallError(f"{path} is not valid TOML ({exc}); fix it first") from exc
        return check(append_block(text, body, tui))

    def unmerge(text: str) -> str:
        return check(strip_block(text)) if has_block(text) else text

    return Target(path, merge, unmerge, has_block)


PLUGIN_MARKER = "subcortex plugin for"


def bundled_plugin(tui: str, filename: str) -> str:
    """Text of a plugin shipped as package data under ``subcortex/plugins/<tui>/``."""
    path = Path(__file__).resolve().parents[1] / "plugins" / tui / filename
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InstallError(f"bundled plugin {path} is missing from this installation") from exc


def daemon_url() -> str:
    from ..config import load_config

    return f"http://127.0.0.1:{int(load_config()['port'])}"


def owned_file_target(dest: Path, content: str) -> Target:
    """A config file subcortex generates and owns entirely (hooks drop-ins).
    A same-named file that doesn't reference subcortex is never overwritten."""

    def ours(text: str) -> bool:
        return any(marker in text for marker in ("subcortex-hook", "subcortex.hook", '"_subcortex"'))

    def merge(text: str) -> str:
        if text.strip() and not ours(text):
            raise InstallError(f"{dest} exists and was not written by subcortex; refusing to overwrite it")
        return content

    return Target(dest, merge, lambda t: "" if ours(t) else t, ours)


def plugin_file_target(dest: Path, content: str) -> Target:
    """A plugin file we own outright. A same-named file without our marker is
    someone else's and is never overwritten or deleted."""

    def ours(text: str) -> bool:
        return PLUGIN_MARKER in text[:400]

    def merge(text: str) -> str:
        if text.strip() and not ours(text):
            raise InstallError(f"{dest} exists and is not a subcortex plugin; refusing to overwrite it")
        return content

    def unmerge(text: str) -> str:
        return "" if ours(text) else text

    return Target(dest, merge, unmerge, ours)


def parse_version(text: str) -> Optional[Tuple[int, ...]]:
    import re

    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        return None
    return tuple(int(g) for g in match.groups(default="0"))


class Installer:
    """One TUI's integration. Subclasses implement ``targets``; hook-based ones
    also ``hook_events``/``sample_payload``/``expects_output``."""

    name: str = ""
    display_name: str = ""
    seam: str = "hooks"            # "hooks" | "plugin" | "mcp"
    binaries: Tuple[str, ...] = ()
    docs: str = ""
    post_install: str = ""         # printed after a successful install
    min_version: Optional[str] = None
    version_args: Tuple[str, ...] = ("--version",)
    supports_mcp: bool = False     # can also register the `subcortex mcp` server

    def __init__(self, mcp: bool = False) -> None:
        self.mcp = mcp and self.supports_mcp

    # -- to implement ---------------------------------------------------------------------

    def targets(self) -> List[Target]:
        raise NotImplementedError

    def hook_events(self) -> List[str]:
        """TUI event names we register (for the self-test). Empty for non-hook seams."""
        return []

    def sample_payload(self, event: str) -> Dict[str, Any]:
        """A realistic stdin payload for ``event`` (self-test input)."""
        return {}

    def expects_output(self, event: str) -> bool:
        """Must the stub-daemon self-test run of ``event`` print a response?"""
        return False

    def warnings(self) -> List[str]:
        """Situational caveats printed with the install result."""
        return []

    def version_problem(self, version_output: str) -> Optional[str]:
        """Reason the detected binary can't be used, else None (override for quirks)."""
        if not self.min_version:
            return None
        found = parse_version(version_output)
        if found is None:
            return None  # unknown format: don't block, the self-test still guards us
        if found < parse_version(self.min_version):
            return (f"{self.display_name} {'.'.join(map(str, found))} is older than "
                    f"{self.min_version}, the first version with the hooks subcortex needs; upgrade it first")
        return None

    # -- shared behavior ------------------------------------------------------------------

    def command(self, event: Optional[str] = None) -> str:
        return hook_command(self.name, event)

    def mcp_command(self) -> List[str]:
        """argv for the stdio MCP server (absolute, like hook commands)."""
        for candidate in (Path(sys.executable).parent / "subcortex", shutil.which("subcortex")):
            if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return [str(candidate), "mcp"]
        return [sys.executable, "-m", "subcortex", "mcp"]

    def detected(self) -> Optional[str]:
        for binary in self.binaries:
            found = shutil.which(binary)
            if found:
                return found
        return None

    def check_version(self) -> Tuple[Optional[str], Optional[str]]:
        """(blocking problem, warning) from the installed binary's version."""
        binary = self.detected()
        if binary is None:
            return None, (f"{self.display_name} not found on PATH; writing its config anyway "
                          "so it is ready when you install it")
        try:
            proc = subprocess.run([binary, *self.version_args], capture_output=True, text=True,
                                  timeout=10, stdin=subprocess.DEVNULL)
            output = (proc.stdout or "") + (proc.stderr or "")
        except (OSError, subprocess.TimeoutExpired):
            return None, f"could not read the {self.display_name} version; continuing"
        return self.version_problem(output), None

    def plans(self, uninstall: bool = False) -> List[Plan]:
        """One plan per file. Targets sharing a file (hooks + MCP entry in one
        settings.json) are applied in sequence, each on the previous result."""
        originals: Dict[Path, str] = {}
        current: Dict[Path, str] = {}
        for target in self.targets():
            if target.path not in originals:
                originals[target.path] = current[target.path] = read_text(target.path)
            text = current[target.path]
            current[target.path] = target.unmerge(text) if uninstall else target.merge(text)
        return [Plan(path, originals[path], current[path]) for path in originals]

    def self_test(self) -> List[str]:
        cases = [(e, self.command(e), self.sample_payload(e)) for e in self.hook_events()]
        if not cases:
            return []
        return self_test(self.name, cases, self.expects_output)

    def _apply(self, result: Result, plans: List[Plan]) -> None:
        for plan in plans:
            if not plan.changed:
                continue
            saved = backup(plan.path)
            if saved:
                result.backups.append(str(saved))
            if plan.after.strip():
                atomic_write(plan.path, plan.after)
            elif plan.path.exists():
                plan.path.unlink()  # the file only ever held our entries
            result.changed = True

    def install(self, dry_run: bool = False, run_self_test: bool = True,
                check_version: bool = True) -> Result:
        result = Result(ok=False, tui=self.name, action="install")
        try:
            plans = self.plans()
        except InstallError as exc:
            result.messages.append(str(exc))
            return result
        result.paths = [str(p.path) for p in plans]
        if check_version:
            problem, warning = self.check_version()
            if problem:
                result.messages.append(problem)
                return result
            if warning:
                result.messages.append(warning)
        if run_self_test:
            problems = self.self_test()
            if problems:
                result.messages.append("self-test failed; nothing was written:")
                result.messages.extend(f"  - {p}" for p in problems)
                return result
        result.messages.extend(self.warnings())
        if not any(p.changed for p in plans):
            result.ok = True
            result.messages.append("already installed; nothing to change")
            return result
        if not dry_run:
            self._apply(result, plans)
            if self.post_install:
                result.messages.append(self.post_install)
        result.ok = True
        return result

    def uninstall(self, dry_run: bool = False) -> Result:
        result = Result(ok=False, tui=self.name, action="uninstall")
        try:
            plans = self.plans(uninstall=True)
        except InstallError as exc:
            result.messages.append(str(exc))
            return result
        result.paths = [str(p.path) for p in plans]
        if not any(p.changed for p in plans):
            result.messages.append("nothing of ours to remove")
        elif not dry_run:
            self._apply(result, plans)
        result.ok = True
        return result

    def installed_executables(self) -> List[str]:
        """Executables referenced by the subcortex commands currently in this TUI's config."""
        import re

        found: List[str] = []
        for target in self.targets():
            for match in re.finditer(r"""['"]?(/[^'"\s]*subcortex(?:-hook)?)['"]?\s""", read_text(target.path)):
                if match.group(1) not in found:
                    found.append(match.group(1))
        return found

    def status(self) -> Dict[str, Any]:
        installed = []
        paths = []
        for target in self.targets():
            paths.append(str(target.path))
            try:
                installed.append(target.is_installed(read_text(target.path)))
            except Exception:
                installed.append(False)
        return {"tui": self.name, "name": self.display_name, "seam": self.seam,
                "config": paths, "installed": bool(installed) and installed[0],
                "mcp_installed": len(installed) > 1 and installed[1],
                "detected": self.detected()}
