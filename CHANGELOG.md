# Changelog

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
