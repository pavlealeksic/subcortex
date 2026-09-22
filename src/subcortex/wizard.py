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
    ("trim_output", "Output trimming", "cut large routine shell output the request doesn't need"),
    ("compaction_snapshot", "Compaction snapshots", "carry the last messages across context compaction"),
]
TYPESAFE_KEYS_URL = "https://console.typesafe.ai/keys"
# (value, label, hint, jev settings or None for a custom endpoint)
JEV_PROVIDERS = [
    ("typesafe", "TypeSafe", f"api.typesafe.ai · keys: {TYPESAFE_KEYS_URL}",
     {"base_url": "https://api.typesafe.ai", "endpoint_path": "/v1/systemone", "api_key_env": "TYPESAFE_API_KEY"}),
    ("openrouter", "OpenRouter", "openrouter.ai · uses your OpenRouter key",
     {"base_url": "https://openrouter.ai/api", "endpoint_path": "/v1/systemone", "api_key_env": "OPENROUTER_API_KEY",
      "model": "jev-latest"}),
    ("custom", "Another endpoint", "any server that speaks the Jev API", None),
]
JEV_MODELS = [
    ("jev-1.13.0", "jev-1.13.0", "pinned · subcortex's thresholds were calibrated on it"),
    ("jev-latest", "jev-latest", "follows new releases · re-check with `subcortex eval` when it moves"),
]
LAYA_TRIM_THRESHOLD = 0.9  # the Laya output rule's primary threshold, set explicitly to opt in
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
    if adapter is not None and not adapter.delivers_hints:
        kinds.discard(PROMPT)  # the prompt hook only records the request (Grok discards its output)
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
        self.installed: List[str] = []

    # -- helpers ---------------------------------------------------------------------------

    def confirm(self, question: str, default: bool) -> bool:
        return default if self.yes else self.ui.confirm(question, default)

    def choose(self, question: str, options, default: int = 0):
        return options[default][0] if self.yes else self.ui.choose(question, options, default)

    # -- flow --------------------------------------------------------------------------------

    def run(self) -> int:
        ui = self.ui
        ui.title(f"subcortex {__version__} setup")
        ui.dim("Fast decisions for your coding agents: a hint when a request is simple, routine shell")
        ui.dim("output trimmed before the model reads it, and recent conversation carried across")
        ui.dim("context compaction. Nothing is written until you confirm it.")
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
            options = [("jev", "Jev — hosted by TypeSafe",
                        "most accurate · ~250 ms a decision · $0.042 per million tokens · needs an API key"),
                       ("laya", "Laya — runs on this machine",
                        f"private · offline · free · more cautious: no output trimming · {provision.laya_package()}")]
            has_key = bool(read_secret(cfg["jev"].get("api_key_env") or "TYPESAFE_API_KEY"))
            default = 0 if has_key or cfg["backend"] == "jev" else 1
            backend = self.choose("Which model should make these decisions?", options, default)
        save_config({"backend": backend})
        if backend == "laya":
            self.setup_laya()
        else:
            self.setup_jev()

    def setup_laya(self) -> None:
        ui = self.ui
        ui.dim("Laya gives prompt hints and carries context across compaction. It doesn't trim")
        ui.dim("output unless you opt in: its judgments cut output a request needed in our tests.")
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
            options = [("dedicated", "Install into subcortex's own environment", f"{provision.venv_dir()} · recommended"),
                       ("current", "Install into this Python", sys.executable),
                       ("skip", "Skip for now", "hooks pass through until the backend is installed")]
            where = self.choose(f"Where should {provision.laya_package()} go?", options, 0)
            if where == "skip":
                self.next_steps.append("install the laya backend: subcortex setup")
                return
            if where == "dedicated":
                foreign = provision.venv_is_foreign()
                if foreign:
                    ui.info(f"{provision.venv_dir()} links to {foreign}.")
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
                python = str(provision.venv_python())
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
        provider = self._jev_endpoint()
        env_name = load_config()["jev"]["api_key_env"]
        import os

        if os.environ.get(env_name, "").strip():
            key = os.environ[env_name].strip()
            ui.ok(f"using ${env_name} from your environment ({_masked(key)})")
            if self._verify_jev(key) and self.confirm(
                    "Also store it privately, so a daemon started at login can use it?", False):
                ui.ok(f"stored in {save_secret(env_name, key)} (only you can read it)")
        elif read_secret(env_name):
            ui.ok(f"using your stored key ({_masked(read_secret(env_name) or '')})")
            if not self.yes and ui.confirm("Replace it?", False):
                self._enter_jev_key(env_name, provider)
            else:
                self._verify_jev(read_secret(env_name) or "")
        elif self.yes:
            self.next_steps.append(f"add your Jev API key: subcortex setup (or export {env_name}=…)")
            return
        else:
            self._enter_jev_key(env_name, provider)
        if read_secret(env_name):
            ui.dim("Each decision sends your latest request, the tool call and a ~1.5 KB excerpt of large")
            ui.dim("outputs (anything that looks like a secret is masked first). `subcortex stats` shows the cost.")
            if not self.yes and ui.confirm("Check the decision quality on your setup now? "
                                           "(64 labeled examples · ~25 s · under $0.01)", False):
                self._run_eval()

    def _jev_endpoint(self) -> str:
        """Which Jev endpoint (and model); returns the provider value."""
        ui = self.ui
        jev = load_config()["jev"]
        current = next((i for i, p in enumerate(JEV_PROVIDERS)
                        if p[3] and p[3]["base_url"].rstrip("/") == str(jev.get("base_url", "")).rstrip("/")), 0)
        if self.yes:
            return JEV_PROVIDERS[current][0]
        provider = ui.choose("Which Jev endpoint?", [p[:3] for p in JEV_PROVIDERS], current)
        settings = next(p[3] for p in JEV_PROVIDERS if p[0] == provider)
        if settings is None:
            from .backends import jev as jev_backend

            def valid_url(value: str) -> Optional[str]:
                try:
                    jev_backend._check_url(jev_backend._join_url(value, "/v1/systemone"))
                except Exception as exc:
                    return str(exc)
                return None

            base = ui.ask("Base URL (as in its docs)", jev["base_url"], validate=valid_url)
            model = ui.ask("Model", jev["model"])
            env_name = ui.ask("Environment variable for its key", jev["api_key_env"],
                              validate=lambda v: None if v.replace("_", "").isalnum() else "letters, digits and _ only")
            save_config({"jev": {"base_url": base, "endpoint_path": "/v1/systemone", "model": model,
                                 "api_key_env": env_name}})
            return provider
        save_config({"jev": dict(settings)})
        if provider == "typesafe":
            # A model chosen earlier stays preselected; otherwise the pinned one:
            # a moving alias can change probability scales under the calibrated rules.
            chosen = _saved_setting("jev", "model")
            index = next((i for i, m in enumerate(JEV_MODELS) if m[0] == chosen), 0)
            save_config({"jev": {"model": ui.choose("Which Jev model?", JEV_MODELS, index)}})
        return provider

    def _enter_jev_key(self, env_name: str, provider: str) -> None:
        """Ask for the key, check it with a real decision, store it only if it works
        (or if the user insists)."""
        ui = self.ui
        if provider == "typesafe":
            ui.info(f"Create a key at {TYPESAFE_KEYS_URL}")
        while True:
            key = ui.ask("API key (paste it; input is hidden)", secret=True, validate=_key_problem)
            if not key:
                self.next_steps.append(f"add your Jev API key: subcortex setup (or export {env_name}=…)")
                return
            if self._verify_jev(key, report_failure=False):
                ui.ok(f"stored in {save_secret(env_name, key)} (only you can read it)")
                return
            choice = ui.choose("What now?", [
                ("retry", "Enter it again", ""),
                ("keep", "Keep it anyway", "e.g. offline right now · `subcortex doctor` checks it later"),
                ("laya", "Use Laya instead", "runs on this machine, no key"),
                ("skip", "Skip for now", "hooks pass everything through until a key is added"),
            ], 0)
            if choice == "retry":
                continue
            if choice == "keep":
                ui.ok(f"stored in {save_secret(env_name, key)} (only you can read it)")
                self.next_steps.append("check the Jev key: subcortex doctor")
            elif choice == "laya":
                save_config({"backend": "laya"})
                self.setup_laya()
            else:
                self.next_steps.append(f"add your Jev API key: subcortex setup (or export {env_name}=…)")
            return

    def _verify_jev(self, key: str, report_failure: bool = True) -> bool:
        """One real decision with ``key`` (not stored yet); True if it worked."""
        ok, message = self.check_jev(key)
        if ok:
            self.ui.ok(message)
        else:
            self.ui.error(message)
            if report_failure:
                self.next_steps.append("check the Jev key/endpoint: subcortex setup")
        return ok

    def check_jev(self, key: str) -> Tuple[bool, str]:
        import os

        from .backends import get_backend

        cfg = load_config()
        env_name = cfg["jev"]["api_key_env"]
        backend = get_backend(cfg, "jev")
        previous = os.environ.get(env_name)
        os.environ[env_name] = key  # this process only, for the one check
        try:
            with self.ui.spinner("checking the key with one decision"):
                started = time.perf_counter()
                result = backend.predict(
                    {"prompt": "what is 2+2?"},
                    {"arithmetic": {"type": "noul",
                                    "instructions": "The request in `prompt` asks for an arithmetic result."}})
            model = result.get("model") if isinstance(result.get("model"), str) else "jev"
            return True, f"key works — {model} answered in {(time.perf_counter() - started) * 1000:.0f} ms"
        except Exception as exc:
            return False, str(exc).replace("jev request failed: ", "")
        finally:
            if previous is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = previous

    def _run_eval(self) -> None:
        from . import cli

        out = io.StringIO()
        with self.ui.spinner("running the labeled examples"), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(out):
            code = cli.main(["eval", "--backend", "jev"])
        for line in out.getvalue().splitlines():
            self.ui.dim(line)
        (self.ui.ok if code == 0 else self.ui.warn)(
            "no hint on complex work, no needed output trimmed" if code == 0
            else "see above; thresholds can be adjusted with subcortex config")

    # -- 2. behaviors ------------------------------------------------------------------------

    def step_features(self) -> None:
        ui = self.ui
        ui.step(2, STEPS, "Behaviors")
        cfg = load_config()
        laya = cfg.get("backend") == "laya"
        opted_in = cfg["thresholds"].get("output_needed_threshold") is not None
        current = {k for k, v in (cfg.get("features") or {}).items() if v}
        if laya and not opted_in:
            current.discard("trim_output")
        options = [(key, label, "off by default with Laya · needs your OK" if key == "trim_output" and laya
                    else hint) for key, label, hint in FEATURES]
        chosen = set(current if self.yes else ui.checklist("What should subcortex do?", options, current))
        if laya and "trim_output" in chosen and not opted_in:
            ui.warn("With Laya, trimming can cut output a request needs (it did on 2 of 6 held-out examples).")
            if ui.confirm("Enable output trimming with Laya anyway?", False):
                save_config({"thresholds": {"output_needed_threshold": LAYA_TRIM_THRESHOLD}})
            else:
                chosen.discard("trim_output")
        save_config({"features": {key: key in chosen for key, _, _ in FEATURES}})
        if chosen:
            ui.ok(", ".join(label.lower() for key, label, _ in FEATURES if key in chosen))
        else:
            ui.warn("all behaviors are off — hooks will pass everything through")
        if not self.yes and ui.confirm("Adjust advanced settings? (time limits, sizes, autostart)", False):
            self.advanced()

    def advanced(self) -> None:
        ui = self.ui
        cfg = load_config()
        hooks, thresholds = cfg["hooks"], cfg["thresholds"]

        def number(low: float, high: float, integer: bool = False):
            def check(value: str) -> Optional[str]:
                try:
                    parsed = int(value) if integer else float(value)
                except ValueError:
                    return "a whole number" if integer else "a number"
                return None if low <= parsed <= high else f"between {low:g} and {high:g}"
            return check

        budget = ui.ask("Time limit for one hook, in seconds", f"{hooks['budget_s']:g}", validate=number(0.5, 30))
        min_chars = ui.ask("Only judge tool outputs longer than (characters)", str(thresholds["min_output_chars"]),
                           validate=number(1000, 1_000_000, integer=True))
        messages = ui.ask("Messages carried across compaction", str(hooks["snapshot_messages"]),
                          validate=number(1, 50, integer=True))
        autostart = ui.confirm("Start the daemon automatically when a hook finds it down?",
                               bool(hooks["autostart_daemon"]))
        save_config({"hooks": {"budget_s": float(budget), "snapshot_messages": int(messages),
                               "autostart_daemon": autostart},
                     "thresholds": {"min_output_chars": int(min_chars)}})
        ui.ok("advanced settings saved (all of them: subcortex config)")

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
                if action != "uninstall":
                    self.installed.append(label)
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
        cfg = load_config()
        ui.title("Done")
        if self.installed:
            ui.ok(f"subcortex is active in {', '.join(self.installed)} "
                  f"(decisions by {'Jev' if cfg.get('backend') == 'jev' else 'Laya'})")
        for step in self.next_steps:
            ui.info(f"→ {step}")
        ui.dim("See what it did:        subcortex stats")
        ui.dim("Check its decisions:    subcortex eval")
        ui.dim("Health check:           subcortex doctor")
        ui.dim("Change anything:        subcortex setup   ·   every setting: subcortex config")


def _saved_setting(section: str, key: str) -> Any:
    """A value the user saved in config.json (not a built-in default), or None."""
    from .config import config_path

    try:
        data = json.loads(config_path().read_text())
    except (OSError, ValueError):
        return None
    value = data.get(section) if isinstance(data, dict) else None
    return value.get(key) if isinstance(value, dict) else None


def _masked(key: str) -> str:
    key = key.strip()
    return f"{key[:8]}…{key[-4:]}" if len(key) > 16 else "…" + key[-2:]


def _key_problem(value: str) -> Optional[str]:
    """Empty is allowed (skip); anything else must look like a key."""
    if not value:
        return None
    if any(c.isspace() for c in value) or not value.isascii() or not value.isprintable():
        return "that doesn't look like a key (spaces or control characters) — paste it again"
    if len(value) < 16:
        return "that's too short for an API key — paste the whole key"
    return None


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
