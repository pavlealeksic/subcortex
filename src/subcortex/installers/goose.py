"""Goose: register ``subcortex mcp`` as a stdio extension in ``config.yaml``.

Goose (>= v1.34) hooks are observation-only for everything but PreToolUse/Stop
(which can block, so we never register them), PostToolUse payloads carry no
output, and there is no compaction event — so Goose's seam is an MCP
extension. The stdlib has no YAML writer, so the entry is inserted line-wise
under the top-level ``extensions:`` mapping between marker comments, and
removed the same way; when PyYAML is importable the result is also parsed and
checked. Anything we can't edit unambiguously is refused.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

from .base import InstallError, Installer, Target

BEGIN = "# >>> subcortex (managed; remove with: subcortex uninstall goose)"
END = "# <<< subcortex"
_TOP_KEY = re.compile(r"^extensions:\s*(?P<rest>.*?)\s*(#.*)?$")


def goose_config() -> Path:
    root = os.environ.get("GOOSE_PATH_ROOT", "").strip()
    if root and os.path.isabs(root):
        return Path(root) / "config" / "config.yaml"
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    return (Path(xdg) if xdg else Path.home() / ".config") / "goose" / "config.yaml"


def _entry_lines(argv: List[str], indent: str) -> List[str]:
    q = json.dumps  # JSON strings are valid YAML flow scalars
    inner = indent * 2
    return [
        f"{indent}{BEGIN}",
        f"{indent}subcortex:",
        f"{inner}enabled: true",
        f"{inner}type: stdio",
        f"{inner}name: subcortex",
        f"{inner}description: {q('subcortex: local millisecond decisions (prompt triage, output judging)')}",
        f"{inner}cmd: {q(argv[0])}",
        f"{inner}args: [{', '.join(q(a) for a in argv[1:])}]",
        f"{inner}envs: {{}}",
        f"{inner}timeout: 300",
        f"{inner}bundled: false",
        f"{indent}{END}",
    ]


def _unmarked_entry(text: str) -> Optional[Tuple[int, int]]:
    """Line range of an ``extensions.subcortex`` entry without our markers.

    Goose rewrites config.yaml with serde_yaml (plugin discovery, ``goose
    configure``, toggling an extension), which drops comments, so an entry we
    wrote can outlive its markers. A second ``subcortex:`` key next to it would
    make the file invalid for Goose, which then starts from an empty config.
    """
    lines = text.splitlines()
    in_extensions = False
    for i, line in enumerate(lines):
        if line.strip() and not line[0].isspace() and not line.startswith("#"):
            in_extensions = line.split(":", 1)[0].strip() == "extensions"
            continue
        match = re.match(r"^([ \t]+)subcortex:\s*(#.*)?$", line)
        if in_extensions and match:
            indent = len(match.group(1))
            end = i + 1
            while end < len(lines) and (not lines[end].strip()
                                        or len(lines[end]) - len(lines[end].lstrip()) > indent):
                end += 1
            if any(re.match(r"^\s+cmd:.*subcortex", l) for l in lines[i + 1:end]):
                return i, end
    return None


def strip_entry(text: str) -> str:
    unmarked = _unmarked_entry(text) if "# >>> subcortex" not in text else None
    if unmarked:
        lines = text.splitlines(keepends=True)
        text = "".join(lines[:unmarked[0]] + lines[unmarked[1]:])
    lines_in = text.splitlines()
    begins = [i for i, l in enumerate(lines_in) if l.strip().startswith("# >>> subcortex")]
    if begins and not any(l.strip().startswith(END) for l in lines_in[begins[0] + 1:]):
        # Removing "to the end of the file" could delete the user's own settings.
        raise InstallError(f"the subcortex block in the goose config has no end marker ({END}); "
                           "remove it by hand, then retry")
    out, inside = [], False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("# >>> subcortex"):
            inside = True
            continue
        if inside and stripped.startswith(END):
            inside = False
            continue
        if not inside:
            out.append(line)
    lines = out
    # An `extensions:` key we left without children becomes an empty mapping.
    for i, line in enumerate(lines):
        if line.rstrip("\n") == "extensions:":
            nxt = next((l for l in lines[i + 1:] if l.strip() and not l.lstrip().startswith("#")), "")
            if not nxt.startswith((" ", "\t")):
                lines[i] = "extensions: {}\n"
    return "".join(lines)


def insert_entry(text: str, argv: List[str]) -> str:
    text = strip_entry(text)
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    for i, line in enumerate(lines):
        match = _TOP_KEY.match(line.rstrip("\n"))
        if not match:
            continue
        rest = match.group("rest")
        if rest in ("{}", "null", "~", ""):
            indent = "  "
            for later in lines[i + 1:]:
                if later.strip() and not later.lstrip().startswith("#"):
                    lead = later[:len(later) - len(later.lstrip())]
                    if lead and rest == "":
                        indent = lead
                    break
            new = ["extensions:\n"] + [l + "\n" for l in _entry_lines(argv, indent)]
            return "".join(lines[:i] + new + lines[i + 1:])
        raise InstallError(f"{goose_config()}: `extensions:` uses inline syntax ({rest!r}); "
                           "add the entry shown by --dry-run by hand")
    # No `extensions:` key yet: the key itself goes inside our markers so
    # uninstall restores the original bytes.
    entry = _entry_lines(argv, "  ")
    block = [BEGIN + "\n", "extensions:\n"] + [l + "\n" for l in entry[1:-1]] + [END + "\n"]
    return "".join(lines + block)


def _validate(text: str, expect_ours: bool) -> str:
    try:
        import yaml  # type: ignore
    except ImportError:
        return text
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise InstallError(f"{goose_config()}: result would not be valid YAML ({exc})") from exc
    has = isinstance(data, dict) and isinstance(data.get("extensions"), dict) \
        and "subcortex" in data["extensions"]
    if has != expect_ours:
        raise InstallError(f"{goose_config()}: could not edit `extensions` unambiguously; "
                           "add the entry shown by --dry-run by hand")
    return text


class GooseInstaller(Installer):
    name = "goose"
    display_name = "Goose"
    seam = "mcp"
    binaries = ("goose",)
    docs = "https://block.github.io/goose/docs/getting-started/using-extensions"
    min_version = "1.34.0"
    post_install = "applies to new goose sessions (tools appear as subcortex__*)"

    def targets(self) -> List[Target]:
        argv = self.mcp_command()

        def merge(text: str) -> str:
            if text.strip():
                _validate(strip_entry(text), expect_ours=False)
            return _validate(insert_entry(text, argv), expect_ours=True)

        def unmerge(text: str) -> str:
            if not installed(text):
                return text
            return _validate(strip_entry(text), expect_ours=False)

        def installed(text: str) -> bool:
            return "# >>> subcortex" in text or _unmarked_entry(text) is not None

        return [Target(goose_config(), merge, unmerge, installed)]
