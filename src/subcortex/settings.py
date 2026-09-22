"""Every user-facing setting, described once: the interactive editor
(``subcortex config``) and ``subcortex config set`` both validate through it.

A setting is (dotted key, section, label, help, kind, options). Kinds:
``bool`` · ``choice`` (options["choices"]: [(value, label, hint)], options["other"]
allows a free value) · ``int``/``float`` (options["min"], options["max"]) ·
``prob`` (null = the backend's calibrated rule, else 0..1) · ``url`` · ``name``.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import DEFAULT_CONFIG, _ENV_OVERRIDES, config_path, load_config, save_config, unset_config

Setting = Tuple[str, str, str, str, str, Dict[str, Any]]

LAYA_MODELS = [("multilingual", "multilingual", "default · many languages"),
               ("english", "english", "English only · smallest"),
               ("typed-decisions", "typed-decisions", "tuned for typed decisions")]
JEV_MODELS = [("jev-1.13.0", "jev-1.13.0", "pinned · the thresholds were calibrated on it"),
              ("jev-latest", "jev-latest", "follows new releases · re-check with `subcortex eval`")]

SETTINGS: List[Setting] = [
    ("backend", "Decisions", "Backend", "who makes the decisions", "choice",
     {"choices": [("jev", "Jev", "hosted by TypeSafe · most accurate · needs a key"),
                  ("laya", "Laya", "runs on this machine · private · free · no trimming by default")]}),
    ("jev.model", "Decisions", "Jev model", "a pinned version keeps the calibrated thresholds meaningful",
     "choice", {"choices": JEV_MODELS, "other": True}),
    ("model", "Decisions", "Laya model", "the local model", "choice", {"choices": LAYA_MODELS}),
    ("features.prompt_hint", "Behaviors", "Prompt hints",
     "tell the main model when a request looks simple", "bool", {}),
    ("features.trim_output", "Behaviors", "Output trimming",
     "cut large routine shell output the request doesn't need", "bool", {}),
    ("features.compaction_snapshot", "Behaviors", "Compaction snapshots",
     "carry the last messages across context compaction", "bool", {}),
    ("thresholds.prompt_simple_confidence", "Thresholds", "Hint threshold",
     "minimum p(simple) for a hint · empty = the backend's calibrated rule", "prob", {}),
    ("thresholds.output_needed_threshold", "Thresholds", "Trim threshold",
     "trim only below this p(needed) · empty = calibrated (Laya: no trimming)", "prob", {}),
    ("thresholds.min_output_chars", "Thresholds", "Judge outputs longer than",
     "characters; shorter outputs are always kept", "int", {"min": 1000, "max": 1_000_000}),
    ("hooks.budget_s", "Hooks", "Time limit per hook",
     "seconds; after that the hook passes everything through", "float", {"min": 0.5, "max": 30}),
    ("hooks.http_timeout_s", "Hooks", "Wait for the daemon",
     "seconds a hook waits for a decision", "float", {"min": 0.2, "max": 30}),
    ("hooks.autostart_daemon", "Hooks", "Autostart the daemon",
     "a hook that finds the daemon down starts it", "bool", {}),
    ("hooks.head_chars", "Trimming", "Keep from the start",
     "characters of a trimmed output's head", "int", {"min": 0, "max": 100_000}),
    ("hooks.tail_chars", "Trimming", "Keep from the end",
     "characters of a trimmed output's tail", "int", {"min": 0, "max": 100_000}),
    ("hooks.snapshot_messages", "Compaction", "Messages kept",
     "recent messages restored after compaction", "int", {"min": 1, "max": 50}),
    ("hooks.snapshot_chars", "Compaction", "Characters per message",
     "each restored message is cut to this length", "int", {"min": 50, "max": 10_000}),
    ("port", "Daemon", "Port", "127.0.0.1 port of the daemon", "int", {"min": 1024, "max": 65535}),
    ("jev.base_url", "Jev endpoint", "Base URL", "as given in the provider's docs", "url", {}),
    ("jev.api_key_env", "Jev endpoint", "Key variable", "environment variable / stored secret name",
     "name", {}),
    ("jev.timeout", "Jev endpoint", "Timeout", "seconds per decision request", "float", {"min": 0.2, "max": 60}),
]
BY_KEY = {s[0]: s for s in SETTINGS}
SECTIONS = list(dict.fromkeys(s[1] for s in SETTINGS))


def _get(cfg: Dict[str, Any], key: str) -> Any:
    node: Any = cfg
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def _nested(key: str, value: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    node = out
    parts = key.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    return out


def parse(key: str, raw: Any) -> Any:
    """``raw`` (text or a value) as the setting's type; ValueError if it can't be."""
    setting = BY_KEY.get(key)
    if setting is None:
        raise ValueError(f"unknown setting {key!r}; see: subcortex config show")
    kind, options = setting[4], setting[5]
    text = raw.strip() if isinstance(raw, str) else raw
    if kind == "bool":
        if isinstance(text, bool):
            return text
        lowered = str(text).lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{key} takes true/false")
    if kind in ("int", "float"):
        try:
            value = int(text) if kind == "int" else float(text)
        except (TypeError, ValueError):
            raise ValueError(f"{key} takes {'a whole number' if kind == 'int' else 'a number'}") from None
        if not options["min"] <= value <= options["max"]:
            raise ValueError(f"{key} must be between {options['min']:g} and {options['max']:g}")
        return value
    if kind == "prob":
        if text in (None, "", "null", "none", "calibrated"):
            return None
        try:
            value = float(text)
        except (TypeError, ValueError):
            raise ValueError(f"{key} is a probability between 0 and 1, or empty for the calibrated rule") from None
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{key} is a probability between 0 and 1")
        return value
    if kind == "choice":
        allowed = [c[0] for c in options["choices"]]
        if text in allowed or (options.get("other") and isinstance(text, str) and text):
            return text
        raise ValueError(f"{key} is one of: {', '.join(allowed)}")
    if kind == "url":
        from .backends import jev as jev_backend

        try:
            jev_backend._check_url(jev_backend._join_url(str(text), "/v1/systemone"))
        except Exception as exc:
            raise ValueError(f"{key}: {exc}") from None
        return str(text).rstrip("/")
    if kind == "name":
        if not text or not str(text).replace("_", "").isalnum():
            raise ValueError(f"{key}: letters, digits and _ only")
        return str(text)
    raise ValueError(f"{key}: unsupported")


def env_override(key: str) -> Optional[str]:
    """The SUBCORTEX_* variable currently forcing this setting, if any."""
    for env, (section, name, _) in _ENV_OVERRIDES.items():
        if (f"{section}.{name}" if section else name) == key and os.environ.get(env, "").strip():
            return env
    return None


def saved(key: str) -> bool:
    try:
        data = json.loads(config_path().read_text())
    except (OSError, ValueError):
        return False
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def shown(key: str, value: Any) -> str:
    kind = BY_KEY[key][4]
    if kind == "bool":
        return "on" if value else "off"
    if kind == "prob":
        return "calibrated" if value is None else f"{value:g}"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


# -- the interactive editor -----------------------------------------------------------------


class Editor:
    def __init__(self, ui: Any) -> None:
        self.ui = ui
        self.changed: List[str] = []

    def run(self) -> int:
        from .ui import Cancelled

        ui = self.ui
        ui.title("subcortex settings")
        ui.dim(f"{config_path()} · changes are saved at once; the daemon picks them up by itself")
        cursor = 0
        try:
            while True:
                cfg = load_config()
                options = [(section, section, self._summary(cfg, section)) for section in SECTIONS]
                options.insert(1, ("key", "Jev API key", self._key_summary(cfg)))
                options.append((None, "Done", ""))
                section = ui.choose("Which settings?", options, cursor)
                if section is None:
                    break
                cursor = next(i for i, o in enumerate(options) if o[0] == section)
                if section == "key":
                    self.edit_key()
                else:
                    self.edit_section(section)
        except Cancelled:
            ui.write()
        self.after()
        return 0

    def _summary(self, cfg: Dict[str, Any], section: str) -> str:
        parts = []
        for key, sec, label, _, _, _ in SETTINGS:
            if sec == section:
                parts.append(f"{label.lower()} {shown(key, _get(cfg, key))}")
        text = " · ".join(parts)
        return text if len(text) <= 70 else text[:69] + "…"

    def _key_summary(self, cfg: Dict[str, Any]) -> str:
        from .config import read_secret

        env_name = cfg["jev"]["api_key_env"]
        if os.environ.get(env_name, "").strip():
            return f"from ${env_name}"
        stored = read_secret(env_name)
        return f"stored ({stored[:8]}…{stored[-4:]})" if stored and len(stored) > 16 else "not set"

    def edit_section(self, section: str) -> None:
        ui = self.ui
        cursor = 0
        while True:
            cfg = load_config()
            options = []
            for key, sec, label, help_text, kind, _ in SETTINGS:
                if sec != section:
                    continue
                forced = env_override(key)
                state = f"forced by ${forced}" if forced else ("changed" if saved(key) else "default")
                options.append((key, f"{label}: {shown(key, _get(cfg, key))}", f"{state} · {help_text}"))
            options.append((None, "Back", ""))
            key = ui.choose(f"{section} — change which? (on/off settings toggle)", options, cursor)
            if key is None:
                return
            cursor = next(i for i, o in enumerate(options) if o[0] == key)
            self.edit(key)

    def edit(self, key: str) -> None:
        ui = self.ui
        _, _, label, help_text, kind, options = BY_KEY[key]
        cfg = load_config()
        current = _get(cfg, key)
        default = _get(DEFAULT_CONFIG, key)
        forced = env_override(key)
        if forced:
            ui.warn(f"${forced} is set, so it wins over the config file until you unset it")
        if kind == "bool":
            value: Any = not current  # toggles in place
        elif kind == "choice":
            choices = list(options["choices"])
            if options.get("other"):
                choices.append(("__other__", "Another value…", ""))
            index = next((i for i, c in enumerate(choices) if c[0] == current), len(choices) - 1)
            value = ui.choose(f"{label}?", choices, index)
            if value == "__other__":
                value = ui.ask(label, str(current), validate=lambda v: _problem(key, v))
        elif kind == "prob":
            pick = ui.choose(f"{label}?", [("calibrated", "Calibrated for the backend", "recommended"),
                                           ("custom", "A value of my own", "0 to 1 · check it with subcortex eval")],
                             0 if current is None else 1)
            value = None if pick == "calibrated" else ui.ask(
                f"{label} (0 to 1)", "" if current is None else f"{current:g}", validate=lambda v: _problem(key, v))
        else:
            value = ui.ask(f"{label} ({help_text})", shown(key, current), validate=lambda v: _problem(key, v))
        value = parse(key, value)
        if key == "thresholds.output_needed_threshold" and value is not None and cfg.get("backend") == "laya":
            ui.warn("With Laya this enables trimming, which cut needed output on held-out examples.")
            if not ui.confirm("Keep this value?", False):
                return
        if value == default and saved(key):
            unset_config(key)  # back to the default: drop it from the file
        elif value != current or not saved(key):
            save_config(_nested(key, value))
        if value != current:
            self.changed.append(key)
            ui.ok(f"{label}: {shown(key, value)}")

    def edit_key(self) -> None:
        from .config import delete_secret, read_secret, save_secret

        ui = self.ui
        cfg = load_config()
        env_name = cfg["jev"]["api_key_env"]
        if os.environ.get(env_name, "").strip():
            ui.info(f"the key comes from ${env_name} in your environment; change it there")
            return
        stored = read_secret(env_name)
        actions = [("replace", "Enter a new key", "checked with one real decision before it's saved")]
        if stored:
            actions.append(("remove", "Remove the stored key", "Jev decisions stop until a key is added"))
        actions.append((None, "Back", ""))
        action = ui.choose("Jev API key", actions, 0)
        if action == "remove":
            if ui.confirm("Remove the stored key?", False):
                delete_secret(env_name)
                ui.ok("removed")
        elif action == "replace":
            from .wizard import Wizard, _key_problem

            key = ui.ask("API key (paste it; input is hidden)", secret=True, validate=_key_problem)
            if not key:
                return
            import argparse

            ok, message = Wizard(ui, argparse.Namespace(yes=False)).check_jev(key)
            (ui.ok if ok else ui.error)(message)
            if ok or ui.confirm("Save it anyway?", False):
                ui.ok(f"stored in {save_secret(env_name, key)} (only you can read it)")

    def after(self) -> None:
        """Follow-ups some changes need."""
        ui = self.ui
        if "port" in self.changed:
            from . import installers

            plugins = [installers.get_installer(n).display_name for n in installers.names()
                       if installers.get_installer(n).seam == "plugin"
                       and installers.get_installer(n).status()["installed"]]
            ui.info("the daemon moves to the new port the next time it starts: subcortex serve --stop")
            if plugins:
                ui.info(f"plugins carry the port: run subcortex install for {', '.join(plugins)}")
        if self.changed:
            ui.dim("check the effect of threshold changes with: subcortex eval")


def _problem(key: str, value: Any) -> Optional[str]:
    try:
        parse(key, value)
    except ValueError as exc:
        return str(exc)
    return None


def edit(ui: Any = None) -> int:
    from .ui import UI

    return Editor(ui or UI()).run()


def known(keys: Sequence[str]) -> List[str]:
    return [k for k in keys if k in BY_KEY]
