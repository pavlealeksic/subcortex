# Research notes (2026-09-21)

Point-in-time integration specs, one per TUI, written while building the 0.2.0
adapters. Each was checked against the TUI's official docs **and** its source
(or shipped bundle) at the latest release of that date, and flags what could
not be verified. They are the evidence behind [../tuis.md](../tuis.md).

Caveats:

- Sections titled "Mismatches in the existing code" / "7b" describe the 0.1.0
  code (commit `3c96217`) and the refactor in progress at the time — all of it
  was addressed in 0.2.0.
- TUIs move fast. Before changing an adapter, re-check the linked sources;
  don't treat these notes as current.
- Wave 2 (`kiro.md`, `grok-build.md`, `docker-agent.md`, `letta.md`,
  `vibe.md`, `mcp-only.md`) covers the agents first found by the sweep.
- `sweep.md` surveys every other terminal agent found (Kiro, Grok Build,
  Docker Agent, Pi, Cline, Letta, Mistral Vibe, Warp, Zed, …) and ranks them
  as integration candidates.
