# Per-TUI reference

What `subcortex install <tui>` writes, which events it uses, and what to know.
Everything here was verified against each TUI's docs and source at its latest
release as of 2026-09-21. Every hook command has the form
`'<abs>/subcortex-hook' <tui> <event> 2>/dev/null || true`.

## Command hooks

### Claude Code — `claude-code`
- **Writes** `~/.claude/settings.json` (`$CLAUDE_CONFIG_DIR`): `UserPromptSubmit`,
  `PostToolUse` (matcher `Bash`), `PreCompact`, `SessionStart` (matcher `compact`).
- Trimming uses `updatedToolOutput` (Claude Code ≥ 2.1.121) in Bash's own
  object shape; skipped when Claude Code already spilled the output to a file,
  for images, interrupted or background commands.
- Interactive sessions run no settings hooks until workspace trust is accepted.
- Other TUIs (Cursor, Droid, Grok Build, Devin, Cortex Code) execute these
  hooks too; the adapter recognises their payloads and stays silent. Install
  subcortex for those TUIs separately.
- MCP (optional): `claude mcp add --scope user subcortex -- subcortex mcp`.
- One-run test: `claude -p "…" --settings <file-with-hooks> --setting-sources local`.

### Codex CLI — `codex` (≥ 0.133)
- **Writes** `$CODEX_HOME/hooks.json` (default `~/.codex`): `UserPromptSubmit`,
  `PostToolUse` (`^Bash$`), `PreCompact`, `SessionStart` (`^compact$`).
  `config.toml` is only touched to remove the block 0.1.0 wrote there, and
  for `--mcp` (`[mcp_servers.subcortex]`).
- **Trust step:** Codex skips new hooks until you run `/hooks` and trust them
  (hash-verified; any edit re-triggers review). `--dangerously-bypass-hook-trust`
  skips it for one run.
- Trimming replies `{"continue": false, "stopReason", "reason"}`: Codex
  replaces the tool result with `reason` and continues. (`decision: block`
  would show the model a *failed* call.) Output Codex already truncated is left alone.
- Hooks run as `$SHELL -lc`: a login profile that prints to stdout corrupts
  every hook's output. Keep shell startup files quiet.

### Open Interpreter — `open-interpreter`
- Same Codex engine. **Writes** `~/.openinterpreter/hooks.json`; same events,
  trust step (`/hooks`) and trimming reply as Codex.

### Qoder CLI — `qoder`, CodeBuddy Code — `codebuddy`
- Claude Code clones. **Write** `~/.qoder/settings.json` /
  `~/.codebuddy/settings.json`; all four events; trimming via
  `updatedToolOutput` (a string). MCP: Qoder's `settings.json`,
  CodeBuddy's `~/.codebuddy/.mcp.json`.

### GitHub Copilot CLI — `copilot` (≥ 1.0.67)
- **Writes its own file** `$COPILOT_HOME/hooks/subcortex.json` (default
  `~/.copilot`): `userPromptSubmitted`, `postToolUse` (`bash|powershell`), `preCompact`.
- Trimming via `modifiedResult` (keeps `resultType: "success"`).
- No post-compaction event: the snapshot is delivered with the next prompt.
- Restart copilot after installing. MCP: `$COPILOT_HOME/mcp-config.json`.

### Factory Droid — `droid`
- **Writes** `~/.factory/hooks.json` (`$FACTORY_HOME_OVERRIDE/.factory`), keyed by
  event with no `hooks` wrapper: `UserPromptSubmit`, `PreCompact`,
  `SessionStart` (`compact`). Droid's PostToolUse can only append, so no trimming.
- Compaction starts a new session id; the restore follows `previous_session_id`.
- One invalid entry disables Droid's whole hooks file — hence the self-test.
  Hooks are snapshotted at startup: restart droid. MCP: `~/.factory/mcp.json`.

### Qwen Code — `qwen-code` (≥ 0.16)
- **Writes** `$QWEN_HOME/settings.json` (default `~/.qwen`): `UserPromptSubmit`,
  `PreCompact`, `SessionStart` (`^compact$`); entries named `subcortex:<Event>`.
- `UserPromptSubmit` also fires on tool-result continuations; only prompts
  carrying `submitted_prompt` are classified.
- PostToolUse is append-only in Qwen and the shell tool already truncates, so no trimming.

### Gemini CLI — `gemini-cli` (≥ 0.27)
- **Writes** `$GEMINI_CLI_HOME/.gemini/settings.json`: `BeforeAgent`,
  `PreCompress` (matcher `manual`); entries named `subcortex:<Event>`, timeouts in ms.
- Hooks — even user-level ones — only run in **trusted folders**.
- `PreCompress` fires before every model request, so subcortex only listens
  for `/compress`; the snapshot is restored with the next prompt. Gemini has
  no post-compaction session start and truncates tool output itself (40k).
- Gemini blocks a prompt on any exit status ≥ 2 with text output; the command
  guard makes that impossible.

### Cursor CLI — `cursor`
- **Writes** `~/.cursor/hooks.json` (always the real home directory):
  `beforeSubmitPrompt`, `preCompact`, with `"version": 1`.
- Cursor skips `beforeSubmitPrompt` in `-p` mode; hints fire in interactive sessions.
- Shell output can't be replaced (only MCP results), so no trimming.
- Two things break *every* hook in that file, and the installer avoids both:
  an unknown event key, and `//` inside any string.

### Kimi Code CLI — `kimi-code` (≥ 0.33)
- **Writes** a marked `[[hooks]]` block in `$KIMI_CODE_HOME/config.toml`
  (default `~/.kimi-code`): `UserPromptSubmit`, `PreCompact`, `PostCompact`.
  Each entry has exactly `event`/`command`/`timeout` — one unknown key silently
  drops every hook in the file.
- Hints go out as `{"message": ...}`. PostToolUse is fire-and-forget (Kimi
  externalizes huge results itself). The transcript is read from the session's
  `wire.jsonl`; the snapshot is restored with the first prompt after `PostCompact`.
- The legacy Python `kimi-cli` (`~/.kimi`, `kimi, version 1.x`) is detected and refused.
  Restart kimi after installing. MCP: `$KIMI_CODE_HOME/mcp.json`.

### OpenHands CLI — `openhands` (≥ 1.12)
- **Writes** `~/.openhands/hooks.json`: `UserPromptSubmit` only, keeping the
  file's existing key style (snake_case, PascalCase or `{"hooks": …}`).
- A project `.openhands/hooks.json` *replaces* the user file inside that
  project; `install` warns when it sees one.
- No compaction hook: subcortex spots a new `Condensation` event in the
  conversation's event log and restores the messages it hid, once, with the next prompt.
- The CLI has been unmaintained since 2026-08; its successor wasn't evaluated.

### Junie CLI — `junie`, Devin CLI — `devin`
- Prompt hint only (`UserPromptSubmit`): neither can replace output or offers a
  pre-compaction event. Junie hooks are an Early Access feature.
  **Write** `~/.junie/config.json` / `~/.config/devin/config.json`.

## Plugins

### OpenCode — `opencode` (≥ 1.1.62), Kilo Code CLI — `kilo`
- **Writes** one plugin file: `~/.config/opencode/plugins/subcortex.ts` /
  `~/.config/kilo/plugin/subcortex.ts` (`$XDG_CONFIG_HOME`). Uninstall deletes it.
- `chat.message` appends a fully formed synthetic hint part; `tool.execute.after`
  trims `bash` output with exit 0 that OpenCode hasn't already spilled to a
  file; `experimental.session.compacting` hands the last messages to the summarizer.
- MCP (OpenCode, optional): `mcp.subcortex` in `~/.config/opencode/opencode.json`.

### Amp — `amp`
- **Writes** `~/.config/amp/plugins/subcortex.ts`. `agent.start` appends a
  hidden hint, `tool.result` replaces shell output (status unchanged),
  `agent.end` keeps a rolling snapshot, and a compaction is detected when the
  first visible message changes — the snapshot then rides the next prompt.
- Reload plugins (Ctrl+O → `plugins: reload`); `amp -x` needs `--plugin-ready-timeout`.

## MCP only

### Crush — `crush`, Goose — `goose`
- Neither has a hook that can inject context (Crush only has PreToolUse; Goose
  hooks are observation-only) and both truncate shell output themselves.
- **Writes** `mcp.subcortex` in `~/.config/crush/crush.json` (strict JSON) /
  `extensions.subcortex` in Goose's `config.yaml` (inserted line-wise between
  marker comments, validated with PyYAML when available).

## No seam

### Aider
No hooks, plugins or MCP. Trim test/lint output with the wrapper, which keeps
the command's exit code:

```yaml
# ~/.aider.conf.yml
test-cmd: subcortex wrap -- pytest -q
lint-cmd:
  - "python: subcortex wrap -- ruff check"
```
