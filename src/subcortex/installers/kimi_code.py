"""Kimi Code CLI: ``[[hooks]]`` in ``$KIMI_CODE_HOME/config.toml`` (+ optional ``mcp.json``).

Kimi validates hook entries with a strict schema — one unknown key or bad
event name silently drops the *whole* hooks section, the user's own hooks
included — so each entry carries exactly ``event``/``command``/``timeout``.
Hooks load at startup: restart kimi after installing.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..adapters.kimi_code import kimi_home
from .base import Installer, Target, mcp_json_target, toml_block_target, toml_str

EVENTS = {"UserPromptSubmit": 5, "PreCompact": 10, "PostCompact": 5}

SESSION = "session_4f0c2a8e-9b1d-4c3e-8a77-2f5d0e6b1c90"


class KimiCodeInstaller(Installer):
    name = "kimi-code"
    display_name = "Kimi Code CLI"
    seam = "hooks"
    binaries = ("kimi",)
    docs = "https://www.kimi.com/code/docs/en/kimi-code-cli/customization/hooks.html"
    min_version = "0.33.0"
    supports_mcp = True
    post_install = "restart kimi so it loads the new hooks"

    def version_problem(self, version_output: str) -> Optional[str]:
        # "kimi-cli <v>" is the dist-info of the legacy Python CLI (installed_version).
        if version_output.strip().lower().replace("_", "-").startswith(("kimi, version", "kimi-cli ")):
            return ("this `kimi` is the legacy Python kimi-cli (~/.kimi), not Kimi Code CLI; "
                    "install @moonshot-ai/kimi-code")
        return super().version_problem(version_output)

    def _body(self) -> str:
        tables = []
        for event, timeout in EVENTS.items():
            tables.append("\n".join([
                "[[hooks]]",
                f"event = {toml_str(event)}",
                f"command = {toml_str(self.command(event))}",
                f"timeout = {timeout}",
            ]))
        return "\n\n".join(tables) + "\n"

    def targets(self) -> List[Target]:
        home = kimi_home()
        targets = [toml_block_target(home / "config.toml", self._body(), self.name)]
        if self.mcp:
            targets.append(mcp_json_target(home / "mcp.json", self.mcp_command(),
                                           extra={"startupTimeoutMs": 10000}))
        return targets

    def hook_events(self) -> List[str]:
        return list(EVENTS)

    def sample_payload(self, event: str) -> Dict[str, Any]:
        common = {"hook_event_name": event, "session_id": SESSION, "cwd": "/tmp",
                  "client_type": "kimi_code_cli"}
        if event == "UserPromptSubmit":
            return {**common, "prompt": [{"type": "text", "text": "what does ls -la do?"}],
                    "is_steer": False}
        if event == "PreCompact":
            return {**common, "trigger": "auto", "token_count": 241337}
        return {**common, "trigger": "auto", "estimated_token_count": 18422}

    def expects_output(self, event: str) -> bool:
        return event == "UserPromptSubmit"
