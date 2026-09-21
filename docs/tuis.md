# Per-TUI reference

What `subcortex install <tui>` writes, which events it uses, and what to know.
Everything here was verified against each TUI's docs and source at its latest
release as of 2026-09-21. Every hook command has the form
`'<python>' -I -m subcortex.hook <tui> <event> 2>/dev/null || true` (isolated
from `PYTHONPATH` and the project directory; harmless if subcortex is gone).

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

### Grok Build — `grok-build`
- **Writes its own file** `$GROK_HOME/hooks/subcortex.json` (default `~/.grok`):
  `PostToolUse` (`Bash|bash`), `PreCompact`, `PostCompact`. Always trusted;
  loaded when a session starts (`/hooks` → `r` reloads).
- Trimming replies with the *complete* tagged result (Grok rejects its own doc
  example): the received `toolResult` with `output_for_prompt` trimmed — the
  `exit: N` header kept — and `output: []`. Verified end to end with grok 1.0.40.
- Grok discards prompt-hook output, so no hint. The compaction snapshot comes
  back as `additionalContext` on the next shell call (the only channel Grok offers).
- Grok also runs `~/.claude/settings.json` hooks; the Claude Code adapter
  recognises Grok (`GROK_HOOK_EVENT`) and stays silent.

### Docker Agent — `docker-agent` (≥ 1.137)
- **Writes its own drop-in** `~/.config/cagent/hooks.d/50-subcortex.yaml`
  (`DOCKER_AGENT_CONFIG_DIR`/`CAGENT_CONFIG_DIR`): `user_prompt_submit`,
  `user_steering_messages_submit`, `user_followup_submit`,
  `tool_response_transform` (`shell`), `before_compaction`, `after_compaction`;
  every hook `on_error: ignore`.
- snake_case replies (`hook_specific_output`); the rewrite is what the model
  sees *and* what's persisted. Before 1.137 a rewrite could bypass secret
  redaction, hence the version floor.
- The snapshot is read read-only from `~/.cagent/session.db` and delivered
  with the next prompt.

### Mistral Vibe — `vibe` (≥ 2.25.5)
- **Writes** a marked `[[hooks]]` block in `$VIBE_HOME/hooks.toml` (default
  `~/.vibe`; Vibe never rewrites that file): one `post_tool` hook on `bash`.
- Trimming replies `{"decision": "deny", "reason": …}`, which in Vibe replaces
  the model-visible text without failing the call (verified in both of its
  harnesses) — allowed for this event only. No prompt or compaction events exist.

### Letta Code — `letta`
- **Writes** `~/.letta/settings.json` → `hooks.UserPromptSubmit` (timeout in
  ms, `quiet: true` so the hint isn't echoed in the TUI). The hint is plain
  text (Letta injects stdout verbatim). Restart letta after installing, and
  don't edit hooks through `/hooks` in a session started before the install.

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

### Pi — `pi` (≥ 0.87)
- **Writes** `~/.pi/agent/extensions/subcortex.ts` (`$PI_CODING_AGENT_DIR`).
  `before_agent_start` adds a hidden hint message; `tool_result` trims
  successful `bash`/`powershell` output (keeping Pi's `Full output: <path>`
  pointer); `session_before_compact` snapshots what's about to be summarized
  and the `context` hook re-inserts it after that summary on every request.
- A Pi extension that fails to load stops `pi` from starting, so the file has
  only type imports and every handler catches everything.

### Cline CLI — `cline` (≥ 3.0.62)
- **Writes** `~/.cline/plugins/subcortex.ts` (`$CLINE_DIR`); `--mcp` also
  registers the MCP server in `cline_mcp_settings.json`.
- `beforeModel` appends the hint to the prompt (request-only) and, when a new
  compaction summary appears, snapshots/restores into it; `afterTool` trims
  successful `run_commands` entries.
- Cline fails the whole run if a plugin hook errors or takes over 3 s, so
  every hook races a 2.3 s deadline and resolves to "no change" on anything unexpected.

## MCP only

### Crush — `crush`, Goose — `goose`
- Neither has a hook that can inject context (Crush only has PreToolUse; Goose
  hooks are observation-only) and both truncate shell output themselves.
- **Writes** `mcp.subcortex` in `~/.config/crush/crush.json` (strict JSON) /
  `extensions.subcortex` in Goose's `config.yaml` (inserted line-wise between
  marker comments, validated with PyYAML when available).

### Warp — `warp`, Zed — `zed`, Kiro CLI — `kiro`, Auggie — `auggie`
- No hooks that can inject anything; subcortex registers its MCP server:
  `~/.warp/.mcp.json` · Zed's `global_settings.json` (its `settings.json` is
  JSONC; `global_settings.json` is merged beneath it and never written by Zed) ·
  `~/.kiro/settings/mcp.json` (Kiro's default engine only takes hooks inside
  agent files, and the opt-in engine injects noise on every prompt) ·
  `~/.augment/settings.json`.

### Continue CLI (`cn`), Rovo Dev CLI — manual
- Continue: add to the `mcpServers` list in `~/.continue/config.yaml` (don't
  create the file — that switches `cn` away from its remote config):
  ```yaml
  mcpServers:
    - name: subcortex
      command: /path/to/subcortex
      args: [mcp]
  ```
- Rovo Dev: add `"subcortex": {"command": "/path/to/subcortex", "args": ["mcp"]}`
  under `mcpServers` in the file named by `mcp.mcpConfigPath` in
  `~/.rovodev/config.yml` (default `~/.rovodev/mcp.json`); Rovo Dev asks you
  to trust it on first launch.

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
