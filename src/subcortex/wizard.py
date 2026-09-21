"""``subcortex setup``: the interactive installer.

Five steps — backend, behaviors, TUIs, review & install, daemon — each saved
as soon as it is confirmed, so cancelling part-way keeps what was agreed to
and changes nothing else. Every choice has a flag, and ``--yes`` accepts the
defaults, so the same flow runs unattended.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import __version__, installers, provision, service
from .config import load_config, read_secret, save_config, save_secret, secrets_path
from .installers.base import InstallError, Plan
from .ui import UI, Cancelled

STEPS = 5
FEATURES = [
    ("prompt_hint", "Prompt hints", "tell the main model when a request looks simple"),
    ("trim_output", "Output trimming", "cut large, disposable shell output to head + tail"),
    ("compaction_snapshot", "Compaction snapshots", "keep the last messages across context compaction"),
]
MODELS = [
    ("multilingual", "multilingual", "default · many languages"),
    ("english", "english", "English only · smallest"),
    ("typed-decisions", "typed-decisions", "tuned for typed decisions"),
]


def capabilities(name: str) -> str:
    """Short 'hint · trim · compaction' summary of what subcortex does in a TUI."""
    from .adapters import get_adapter
    from .adapters.base import PRE_COMPACT, PROMPT, TOOL_OUTPUT

    installer = installers.get_installer(name)
    if installer.seam == "plugin":
        return "hint · trim · compaction"
    if installer.seam == "mcp":
        return "on-demand tools"
    adapter = get_adapter(name)
    kinds = set(adapter.events.values()) if adapter else set()
    parts = [label for kind, label in ((PROMPT, "hint"), (TOOL_OUTPUT, "trim"), (PRE_COMPACT, "compaction"))
             if kind in kinds]
    return " · ".join(parts) or "—"


def tui_rows() -> List[Dict[str, Any]]:
    rows = []
    for name in installers.names():
        installer = installers.get_installer(name, mcp=True)
        try:
            installed = bool(installer.status()["installed"])
        except Exception:
            installed = False
        rows.append({"name": name, "display": installer.display_name, "seam": installer.seam,
                     "detected": installer.detected(), "installed": installed,
                     "mcp": installer.supports_mcp})
    rows.sort(key=lambda r: (not (r["detected"] or r["installed"]), r["display"].lower()))
    return rows


def pick_tuis(ui: UI, question: str, rows: Sequence[Dict[str, Any]], preselected: Set[str]) -> List[str]:
    options = []
    for row in rows:
        state = "installed" if row["installed"] else ("found" if row["detected"] else "not found")
        options.append((row["name"], row["display"], f"{row['seam']} · {capabilities(row['name'])} · {state}"))
    return ui.checklist(question, options, preselected)


def _diffstat(plan: Plan) -> str:
    added = removed = 0
    for line in plan.diff().splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    action = "create" if not plan.before else ("delete" if not plan.after else "edit")
    return f"{action} {plan.path}  (+{added} −{removed})"


class Wizard:
    def __init__(self, ui: UI, args: argparse.Namespace) -> None:
        self.ui = ui
        self.args = args
        self.yes = bool(getattr(args, "yes", False))
        self.next_steps: List[str] = []

    # -- helpers ---------------------------------------------------------------------------

    def confirm(self, question: str, default: bool) -> bool:
        return default if self.yes else self.ui.confirm(question, default)

    def choose(self, question: str, options, default: int = 0):
        return options[default][0] if self.yes else self.ui.choose(question, options, default)

    # -- flow --------------------------------------------------------------------------------

    def run(self) -> int:
        ui = self.ui
        ui.title(f"subcortex {__version__} setup")
        ui.dim("A small local decision model that saves your coding TUIs tokens. "
               "Nothing is written until you confirm it.")
        try:
            self.step_backend()
            self.step_features()
            selected, remove = self.step_tuis()
            self.step_install(selected, remove)
            self.step_daemon()
        except Cancelled:
            ui.write()
            ui.warn("setup cancelled — steps you already confirmed stay applied; nothing else was changed")
            return 130
        self.summary()
        return 0

    # -- 1. backend --------------------------------------------------------------------------

    def step_backend(self) -> None:
        ui = self.ui
        ui.step(1, STEPS, "Decision backend")
        cfg = load_config()
        backend = getattr(self.args, "backend", None)
        if not backend:
            options = [("laya", "Laya — local model", f"private, offline, free · {provision.laya_package()}"),
                       ("jev", "Jev — hosted API", "nothing to install locally · needs an API key")]
            backend = self.choose("Which decision backend?", options, 0 if cfg["backend"] == "laya" else 1)
        save_config({"backend": backend})
        if backend == "laya":
            self.setup_laya()
        else:
            self.setup_jev()

    def setup_laya(self) -> None:
        ui = self.ui
        cfg = load_config()
        model = getattr(self.args, "model", None)
        if not model:
            current = next((i for i, m in enumerate(MODELS) if m[0] == cfg.get("model")), 0)
            model = self.choose("Which Laya model?", MODELS, current)
        save_config({"model": model})

        python = provision.daemon_python()
        ready, why = provision.backend_ready(python)
        if ready:
            ui.ok(why)
        else:
            ui.warn(why)
            if getattr(self.args, "skip_backend_install", False):
                self.next_steps.append("install the laya backend: subcortex setup")
                return
            options = [("dedicated", "Install into subcortex's own environment", f"{provision.VENV_DIR} · recommended"),
                       ("current", "Install into this Python", sys.executable),
                       ("skip", "Skip for now", "hooks pass through until the backend is installed")]
            where = self.choose(f"Where should {provision.laya_package()} go?", options, 0)
            if where == "skip":
                self.next_steps.append("install the laya backend: subcortex setup")
                return
            if where == "dedicated":
                foreign = provision.venv_is_foreign()
                if foreign:
                    ui.info(f"{provision.VENV_DIR} links to {foreign}.")
                    ui.dim("It will be replaced by a dedicated environment; the linked one is left untouched.")
                    if not self.confirm("Replace the link?", True):
                        self.next_steps.append("install the laya backend: subcortex setup")
                        return
                with ui.spinner("creating the environment"):
                    ok, message = provision.create_venv(replace=bool(foreign))
                if not ok:
                    ui.error(f"could not create the environment: {message}")
                    self.next_steps.append("install the laya backend: subcortex setup")
                    return
                python = str(provision.VENV_PYTHON)
            else:
                python = sys.executable
            with ui.spinner(f"installing {provision.laya_package()} and subcortex (a few minutes the first time)"):
                ok, tail = provision.install_laya(python)
            if not ok:
                ui.error("pip failed:")
                for line in tail.splitlines()[-8:]:
                    ui.dim(line)
                self.next_steps.append("install the laya backend: subcortex setup")
                return
            ready, why = provision.backend_ready(python)
            (ui.ok if ready else ui.warn)(why)
            if not ready:
                return
            save_config({"daemon_python": python})
        if self.confirm("Download and load the model now? (first time only; a few hundred MB)", True):
            self.warm_up()

    def warm_up(self) -> None:
        ui = self.ui
        cfg = load_config()
        if not self.restart_daemon():
            ui.error("the daemon did not start; see `subcortex doctor`")
            return
        port = int(cfg["port"])
        try:
            with ui.spinner("downloading and loading the model"):
                _post(port, "/verdict/prompt", {"prompt": "what is 2+2?"}, timeout=1800)
            started = time.perf_counter()
            body = _post(port, "/verdict/prompt", {"prompt": "rename foo to bar"}, timeout=60)
            ms = (time.perf_counter() - started) * 1000
        except Exception as exc:
            ui.error(f"the model did not answer: {exc}")
            return
        if body.get("success"):
            ui.ok(f"model ready — a decision now takes {ms:.0f} ms")
        else:
            ui.error(f"the backend failed: {body.get('error')}")

    def setup_jev(self) -> None:
        ui = self.ui
        jev = load_config()["jev"]
        if not self.yes and self.ui.confirm(f"Use the default endpoint ({jev['base_url']}{jev['endpoint_path']})?", True) is False:
            from .backends import jev as jev_backend

            def valid_url(value: str) -> Optional[str]:
                try:
                    jev_backend._check_url(jev_backend._join_url(value, "/"))
                except Exception as exc:
                    return str(exc)
                return None

            base = ui.ask("Base URL", jev["base_url"], validate=valid_url)
            path = ui.ask("Endpoint path", jev["endpoint_path"])
            model = ui.ask("Model", jev["model"])
            save_config({"jev": {"base_url": base, "endpoint_path": path, "model": model}})
        env_name = load_config()["jev"]["api_key_env"]
        import os

        if os.environ.get(env_name, "").strip():
            ui.ok(f"using ${env_name} from your environment")
            if self.confirm("Also store it privately so a daemon started at login can use it?", False):
                path = save_secret(env_name, os.environ[env_name].strip())
                ui.ok(f"stored in {path} (readable only by you)")
        elif read_secret(env_name):
            ui.ok(f"using the key stored in {secrets_path()}")
            if not self.yes and ui.confirm("Replace it?", False):
                self._ask_key(env_name)
        elif self.yes:
            self.next_steps.append(f"provide the jev API key: export {env_name}=… or run subcortex setup")
            return
        else:
            self._ask_key(env_name)
        if read_secret(env_name) and self.confirm("Check the key with one test decision?", True):
            self.test_jev()

    def _ask_key(self, env_name: str) -> None:
        key = self.ui.ask(f"{env_name} (input hidden; stored with mode 600)", secret=True)
        if key:
            path = save_secret(env_name, key)
            self.ui.ok(f"stored in {path} (readable only by you)")
        else:
            self.next_steps.append(f"provide the jev API key: export {env_name}=… or run subcortex setup")

    def test_jev(self) -> None:
        from .backends import get_backend

        backend = get_backend(load_config(), "jev")
        try:
            with self.ui.spinner("asking jev"):
                started = time.perf_counter()
                result = backend.predict(
                    {"prompt": "what is 2+2?"},
                    {"arithmetic": {"type": "noul",
                                    "instructions": "The request in `prompt` asks for an arithmetic result."}})
            model = result.get("model") if isinstance(result.get("model"), str) else "jev"
            self.ui.ok(f"key works — {model} answered in {(time.perf_counter() - started) * 1000:.0f} ms")
        except Exception as exc:
            self.ui.error(f"test decision failed: {exc}")
            self.next_steps.append("check the jev key/endpoint: subcortex setup")

    # -- 2. behaviors ------------------------------------------------------------------------

    def step_features(self) -> None:
        self.ui.step(2, STEPS, "Behaviors")
        current = {k for k, v in (load_config().get("features") or {}).items() if v}
        chosen = set(current if self.yes else self.ui.checklist("What should subcortex do?", FEATURES, current))
        save_config({"features": {key: key in chosen for key, _, _ in FEATURES}})
        if chosen:
            self.ui.ok(", ".join(label.lower() for key, label, _ in FEATURES if key in chosen))
        else:
            self.ui.warn("all behaviors are off — hooks will pass everything through")

    # -- 3. TUIs --------------------------------------------------------------------------------

    def step_tuis(self) -> Tuple[List[str], List[str]]:
        ui = self.ui
        ui.step(3, STEPS, "Coding TUIs")
        rows = tui_rows()
        installed = {r["name"] for r in rows if r["installed"]}
        requested = getattr(self.args, "tuis", None)
        if requested:
            selected = _resolve(requested, rows)
        elif self.yes:
            selected = [r["name"] for r in rows if r["detected"] or r["installed"]]
        else:
            ui.dim("Found on this machine are preselected; the others can be set up ahead of time.")
            preselected = {r["name"] for r in rows if r["detected"] or r["installed"]}
            selected = pick_tuis(ui, "Wire subcortex into which TUIs?", rows, preselected)
        remove = sorted(installed - set(selected))
        if remove and not self.yes:
            names = ", ".join(installers.get_installer(n).display_name for n in remove)
            if not ui.confirm(f"Remove subcortex from {names} (not selected)?", False):
                remove = []
        elif self.yes:
            remove = []
        names = [installers.get_installer(n).display_name for n in selected]
        self.ui.ok(", ".join(names) if names else "no TUIs selected")
        self.mcp = bool(getattr(self.args, "mcp", False))
        if not self.mcp and not self.yes and any(r["mcp"] for r in rows if r["name"] in selected):
            self.mcp = ui.confirm("Also register the on-demand MCP tools where supported? "
                                  "(the model pays tokens each time it calls them)", False)
        return selected, remove

    # -- 4. review & install --------------------------------------------------------------------

    def step_install(self, selected: List[str], remove: List[str]) -> None:
        ui = self.ui
        ui.step(4, STEPS, "Review & install")
        if not selected and not remove:
            ui.info("no TUIs selected")
            return
        work: List[Tuple[str, str, Any, List[Plan]]] = []
        for name in selected:
            installer = installers.get_installer(name, mcp=self.mcp)
            try:
                plans = installer.plans()
            except InstallError as exc:
                ui.error(f"{installer.display_name}: {exc}")
                continue
            work.append(("install", name, installer, plans))
        for name in remove:
            installer = installers.get_installer(name, mcp=True)
            try:
                work.append(("uninstall", name, installer, installer.plans(uninstall=True)))
            except InstallError as exc:
                ui.error(f"{installer.display_name}: {exc}")
        pending = [w for w in work if any(p.changed for p in w[3])]
        for action, name, installer, plans in work:
            changed = [p for p in plans if p.changed]
            verb = "remove" if action == "uninstall" else "set up"
            if not changed:
                ui.ok(f"{installer.display_name}: already {'removed' if action == 'uninstall' else 'set up'}")
                continue
            ui.info(f"{installer.display_name} — {verb}:")
            for plan in changed:
                ui.dim(f"  {_diffstat(plan)}")
        if not pending:
            return
        if not self.yes and ui.confirm("Show the full diffs?", False):
            for _, _, _, plans in pending:
                for plan in plans:
                    if plan.changed:
                        ui.write(plan.diff() or f"(creates {plan.path})")
        if not self.confirm("Apply these changes?", True):
            ui.warn("nothing written to any TUI config")
            return
        for action, name, installer, _ in pending:
            label = installer.display_name
            if action == "uninstall":
                result = installer.uninstall()
            else:
                with ui.spinner(f"{label}: testing the hook commands, then writing"):
                    result = installer.install(check_version=not getattr(self.args, "ignore_version", False))
            if result.ok:
                ui.ok(f"{label}: {'removed' if action == 'uninstall' else 'installed'}")
                for backup in result.backups:
                    ui.dim(f"  backup: {backup}")
                for message in result.messages:
                    if message == installer.post_install:
                        self.next_steps.append(f"{label}: {message}")
                    elif "not found on PATH" not in message:
                        ui.dim(f"  {message}")
            else:
                ui.error(f"{label}: nothing written")
                for message in result.messages:
                    ui.dim(f"  {message}")

    # -- 5. daemon ------------------------------------------------------------------------------

    def restart_daemon(self) -> bool:
        from . import cli

        cfg = load_config()
        health = cli._health(cfg)
        wanted = (cfg.get("backend"), __version__)
        if health and (health.get("backend"), health.get("version")) == wanted:
            return True
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            if health:
                cli._stop_daemon()
                time.sleep(0.5)
            cli._spawn_daemon(cfg)
            return cli._wait_for_health(cfg) is not None

    def step_daemon(self) -> None:
        ui = self.ui
        ui.step(5, STEPS, "Daemon")
        if self.confirm("Start (or restart) the daemon now?", True):
            with ui.spinner("starting the daemon"):
                ok = self.restart_daemon()
            (ui.ok if ok else ui.error)("daemon running" if ok else "daemon did not start; see `subcortex doctor`")
        st = service.status()
        if not st["supported"]:
            ui.dim("no login service on this platform; hooks start the daemon when they need it")
            return
        if st["installed"]:
            ui.ok(f"starts at login ({st['path']})")
            return
        wanted = getattr(self.args, "service", None)
        if wanted is None:
            wanted = self.confirm("Start the daemon automatically at login?", False)
        if wanted:
            try:
                for note in service.install():
                    ui.ok(note)
            except RuntimeError as exc:
                ui.error(str(exc))
        else:
            ui.dim("without it, the first hook after a reboot starts the daemon (that request passes through)")

    # -- summary ------------------------------------------------------------------------------

    def summary(self) -> None:
        ui = self.ui
        ui.title("Done")
        for step in self.next_steps:
            ui.info(f"→ {step}")
        ui.dim("Check health any time: subcortex doctor · change choices: subcortex setup")


def _resolve(requested: Sequence[str], rows: Sequence[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    for token in requested:
        for part in str(token).split(","):
            part = part.strip()
            if part == "detected":
                names += [r["name"] for r in rows if r["detected"]]
            elif part == "all":
                names += [r["name"] for r in rows]
            elif part == "none":
                continue
            elif part:
                key = installers.canonical_name(part)
                if key is None:
                    raise SystemExit(f"unknown TUI {part!r}; see: subcortex tuis")
                names.append(key)
    return list(dict.fromkeys(names))


def _post(port: int, path: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    from . import localhttp

    _, reply = localhttp.request(port, "POST", path, payload, timeout)
    if not isinstance(reply, dict):
        raise ValueError("daemon reply is not a JSON object")
    return reply


def run(args: argparse.Namespace, ui: Optional[UI] = None) -> int:
    return Wizard(ui or UI(), args).run()
