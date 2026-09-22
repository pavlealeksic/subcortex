# Changelog

## 0.3.1

### Onboarding
- `subcortex setup` explains the choice honestly (Jev: most accurate, hosted,
  needs a key; Laya: private and free, no trimming by default) and preselects
  Jev when a key is already available.
- Jev: pick TypeSafe, OpenRouter or another endpoint, get pointed to
  https://console.typesafe.ai/keys, paste the key hidden. It is checked with
  one real decision *before* it is saved; a wrong key offers retry, keep
  anyway, use Laya instead, or skip. Keys are only ever shown masked. A pinned
  model (`jev-1.13.0`, the calibrated one) is preselected, and an optional
  quality check runs `subcortex eval`.
- With Laya, output trimming is off by default and needs an explicit OK that
  states the measured risk.
- Optional advanced settings (hook time limit, output size, compaction memory,
  autostart), and a summary of where subcortex is active and how to see it
  working.

### Settings
- `subcortex config` in a terminal is now a settings editor: every option by
  section with its current value and help, on/off settings toggle in place,
  menus for choices, validated numbers, "calibrated or custom" thresholds,
  a reset to default by choosing it again, settings forced by `SUBCORTEX_*`
  variables marked as such, and the Jev key (replace — checked first — or
  remove). `config set` validates through the same rules.

## 0.3.0

Decisions you can trust, and a hook path that is safe under real, concurrent use.

### Decisions
- Questions and thresholds are calibrated per backend on labeled examples and
  checked on a held-out set. Jev: 29/32 simple requests hinted, **0/32 complex**;
  18/18 disposable outputs trimmed, **0/18 needed**. The 0.2.0 prompt question
  was inverted on Laya and gave "simple" hints on complex work; it is gone.
- Output is judged against the user's latest request, remembered per session.
  Without a request, nothing is trimmed.
- Laya no longer trims by default (it trimmed needed output on the held-out
  set); it keeps prompt hints and compaction restore.
- Trims keep lines that mention the request or warn, cut on line boundaries and
  must save 20%. Failure detection now covers compiler errors, tsc, go test,
  make, segfaults and panics, in linear time.
- New `subcortex eval` runs the labeled sets through your backend and thresholds.
- New `subcortex stats` report: hints, trims (tokens saved), restores and Jev
  cost per TUI, from a counts-only private ledger.

### Jev
- Kept-alive connections: ~250 ms per decision instead of ~650 ms.
- Usage and cost are metered; answers are validated against the v1 schema;
  HTTP errors say what to do; official base URLs work as pasted from the docs;
  2.5 s timeout; keys with stray whitespace are refused clearly.
- Model input is bounded (a head+tail excerpt) and secrets are masked first.

### Safety
- Hooks run as `python -I -m subcortex.hook`: `PYTHONPATH` and a project's own
  modules (a `json.py`) can no longer break a hook or run inside it. The daemon
  is started the same way, from its data dir.
- Hooks and plugins reach the daemon over a direct socket: `HTTP_PROXY` no
  longer routes prompts to a proxy (urllib and Bun's fetch did).
- A C-level watchdog ends a hook even when work holds the interpreter lock; a
  regex that could backtrack for 16 s on blank-line output is gone.
- Per-session state is keyed by TUI and session, atomic, private and locked; a
  compaction snapshot is restored exactly once and deleted only after delivery.
- The daemon requires a per-user token, refuses browser-shaped requests,
  answers bursts (it used to drop 56 of 64 concurrent requests), sheds load it
  can't serve in time, reloads its config, stops cleanly on SIGTERM, restarts
  itself after an upgrade, and `serve --stop` never signals an unconfirmed PID.
- Installers keep symlinked configs symlinked, re-merge a file the TUI changed
  during install, and never touch bytes outside subcortex's own block.
- `doctor` flags integrations installed by an older subcortex.

### Verified inside real TUIs
Driven end to end, sandboxed, against mock model APIs (what each TUI sent to
the model is asserted): Claude Code 2.1.278, Codex 0.155.1, Open Interpreter
0.0.45, Gemini CLI 0.60.0, Qwen Code 0.24.3, Kimi Code 2.0.2, OpenCode
1.18.31, Grok Build 1.0.40, Pi 0.87.0, Cline 3.0.62, Docker Agent 1.142.0,
Mistral Vibe 2.25.5, Factory Droid 0.224.0, Copilot CLI 1.0.87, OpenHands
1.16.0, Letta Code 0.32.15, CodeBuddy 2.156.0, Junie 26.9.21, Devin CLI
3000.11.1, Cursor CLI 2026.09.18, Amp's plugin runtime, and the MCP servers in
Crush 0.96.1, Goose 1.51.0 and Auggie 0.36.0. That found and fixed:
- Codex / Open Interpreter: a failed command could be trimmed (the hook has
  no exit code; it is now read from the session log). Open Interpreter
  installs follow `INTERPRETER_HOME`.
- Cline: compaction restore never arrived (`<user_input mode="act">` prompts
  were dropped from snapshots).
- Gemini CLI / Qwen Code: snapshots repeated subcortex's own hint (and on
  Gemini compounded the previous restore).
- Mistral Vibe: could never trim (no prompt event); the request now comes
  from its message log.
- Goose: after Goose rewrote its config, a reinstall wrote a duplicate key,
  which made Goose reset the user's whole config.
- Amp: a failed command (reported as `done` with a non-zero `exitCode`) could
  be trimmed. CodeBuddy's post-tool hook carries no output, so it is no longer
  registered. Letta's default conversations of different agents no longer
  share state.
- Installers follow each TUI's config-dir overrides (`CODEBUDDY_CONFIG_DIR`,
  `QODER_CONFIG_DIR`, `JUNIE_HOME`, `XDG_CONFIG_HOME` for Devin), and MCP
  entries run isolated (`python -I -m subcortex mcp`) like hooks.
- The daemon could run an older subcortex from the backend venv; it now
  always runs the installed code. Installers no longer execute a TUI to read
  its version (Droid's `--version` refreshes logins and self-updates).

### Compatibility
- Hook commands changed: run `subcortex install <tui>` (or `subcortex setup`)
  once; `subcortex doctor` lists what needs it. Old commands keep working
  until then (fail-open).
- Plugins must be reinstalled to get the token; until then their requests are
  refused and they pass everything through unchanged.
- `thresholds.prompt_simple_confidence` / `output_needed_threshold` now default
  to `null` (the backend's calibrated rule); a number overrides the primary
  threshold.
- The MCP tool `subcortex_judge_output` takes the request as `task`.
