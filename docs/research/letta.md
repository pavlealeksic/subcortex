# Letta Code (`letta`) — subcortex hook adapter spec

Target: `@letta-ai/letta-code` **0.32.15** (npm `latest`, published 2026-09-21T01:40Z; git tag `v0.32.15` = `1bcc2535`, 2026-09-20). Verified by reading the source at that tag (not only docs). `main` was 3 commits ahead at time of reading (0b5f2cc, 2026-09-21); none of them touch `src/hooks/` or `src/settings-manager.ts`. Nothing was executed.

**Bottom line**
- Command hooks: B1 ✅ (raw stdout → `<system-reminder>`), B2 ✗ (append only), B3 ◐ (PreCompact observe; auto path is fire-and-forget and fires *after* compaction has started/finished), B4 ◐ (no post-compaction event; re-inject on next UserPromptSubmit).
- **New since the sweep: Letta "mods"** (`~/.letta/mods/*.mjs|js|ts`, trusted in-process JS). `tool_end` can **replace** a tool result (genuine B2), `turn_start` can rewrite the outbound user message (B1), `compact_start`/`compact_end` give B3/B4 **on the local backend only**. This is a plugin seam (like Kilo/Pi), not a command hook. See §7.

---

## 1. Sources, versions, detection

- Source (tag `v0.32.15`, https://github.com/letta-ai/letta-code):
  - `src/hooks/types.ts` (event list, stdin shapes, config types), `src/hooks/executor.ts` (spawn, timeout, exit-code mapping, output collection), `src/hooks/index.ts` (per-event runners), `src/hooks/loader.ts` (merge order, matcher, `disabled`), `src/hooks/writer.ts` (`/hooks` UI writer).
  - Call sites: `src/cli/app/use-submit-handler.ts` (UserPromptSubmit ~L640–670, reminder assembly ~L3570–3670, manual `/compact` PreCompact ~L2091), `src/cli/helpers/accumulator.ts` (auto-compaction PreCompact ~L1475; server-side-tool PostToolUse ~L1291), `src/tools/manager.ts` (client-tool Pre/PostToolUse, `collectPostToolHookFeedback`, `appendHookFeedbackToToolReturn`, `emitToolEndEvent`), `src/cli/app/AppCoordinator.tsx` + `use-conversation-switching.ts` (SessionStart), `src/websocket/listener/commands.ts` (listener `/compact`).
  - Settings: `src/settings-manager.ts` (path, JSON parse, persist/merge), MCP: `src/mcp-client.ts`, `src/cli/commands/mcp.ts`, `src/cli/subcommands/mcp.ts`.
  - Mods: `src/mods/{paths,mod-sources,file-extensions,disable,types,mod-engine}.ts`, `src/cli/mods/local-backend-mod-events.ts`, authoring doc `src/skills/builtin/creating-mods/references/events.md`.
- Docs: https://docs.letta.com/reference/settings/index.md (does **not** document hooks), https://docs.letta.com/configuration/mods/index.md.
- Version: `letta --version` prints `0.32.15 (Letta Code)` (`src/index.ts`). npm dist-tags at time of check: `latest=0.32.15`, `next=0.18.5-next.1` (stale).
- Detect a Letta invocation inside the hook: env `LETTA_HOOK_EVENT` is always set; payload key is `event_type` (not `hook_event_name`); cwd key is `working_directory`.
- Header comment says "Claude Code-compatible" — it is **not** payload-compatible (see §4). Event *names* and the `{matcher, hooks:[{type,command,timeout}]}` group shape do match Claude.

## 2. Config paths, relocation, trust

| What | Path | Format |
|---|---|---|
| User hooks | `$HOME/.letta/settings.json` → key `hooks` (`process.env.HOME \|\| os.homedir()`) | **plain JSON** (`JSON.parse`, no comments) |
| Project hooks | `<cwd>/.letta/settings.json` → `hooks` (ignored when `<cwd>` is `$HOME`, to avoid double-loading) | JSON |
| Project-local hooks | `<cwd>/.letta/settings.local.json` → `hooks` | JSON |
| Mods | `~/.letta/mods/*.{js,mjs,ts,tsx}` (or `$LETTA_MODS_DIR`; legacy `~/.letta/extensions/`, `$LETTA_EXTENSIONS_DIR`), per-agent `$MEMORY_DIR/mods/` | JS/TS module |
| MCP (client-local) | `~/.letta/settings.json` → `agents[].mcpServers[]` (**per agent**) | JSON |

- **Merge:** all three hook files are merged per event; order = project-local, project, global (all run). `hooks.disabled: true` anywhere disables all hooks unless the *user* file says `disabled: false`.
- **Relocation:** there is no `LETTA_HOME`. Set `HOME=<tmp>` for isolated tests (settings path uses `process.env.HOME`).
- **Trust:** no trust prompt or enable flag for hooks or mods. Hooks are on when configured.
- **When changes take effect:** global settings (incl. `hooks`) are read **once at startup** into the settings-manager cache; `/reload` only clears project/local caches (`clearCaches()`), so a **restart is needed** after installing user-level hooks.
- **File safety (important for the installer):**
  - Letta's `persistSettings()` re-reads the file and writes back `{...fileOnDisk, ...onlyKeysThisProcessDirtied}` with `JSON.stringify(…, null, 2)`. External edits to keys Letta did not touch survive. **But** if the user edits hooks via `/hooks` while Letta is running, Letta writes its *in-memory* `hooks` (loaded at startup) and drops hook entries we added after that start. ⇒ install while Letta is not running, or tell the user to restart.
  - If the file fails `JSON.parse` at startup, Letta logs "Error loading settings, using defaults", marks default keys dirty and may rewrite the file from defaults. ⇒ never leave it invalid; write atomically.
  - The file is never JSONC (Letta writes it with `JSON.stringify`); safe to merge with a JSON parser.

## 3. Registration and tagging

Schema (`types.ts`, no runtime validation — a wrong shape can throw inside the runner):
- Tool events (`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PermissionRequest`): array of `{ "matcher": string, "hooks": HookCommand[] }`.
- Simple events (`UserPromptSubmit`, `Notification`, `Stop`, `SubagentStop`, `PreCompact`, `SessionStart`, `SessionEnd`): array of `{ "hooks": HookCommand[] }` (a `matcher` is ignored).
- `HookCommand` = `{ "type": "command", "command": string, "timeout"?: number /* ms, default 60000 */, "quiet"?: boolean }` or a `type:"prompt"` LLM hook (don't use).
- `quiet: true` suppresses Letta's `console.log` of `[hook:<Event>] <command>`, exit code and **the hook's stdout/stderr** into the TUI. Without it, our injected hint is echoed on screen. **Set `quiet: true`.**
- Matcher (tool events): `""`/`"*"` = all; otherwise anchored, case-sensitive regex `^(?:pattern)$` against the **internal** tool name; invalid regex → exact-string compare.

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "'/abs/subcortex-hook' letta UserPromptSubmit 2>/dev/null || true", "timeout": 5000, "quiet": true } ] }
    ],
    "PreCompact": [
      { "hooks": [ { "type": "command", "command": "'/abs/subcortex-hook' letta PreCompact 2>/dev/null || true", "timeout": 10000, "quiet": true } ] }
    ],
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "'/abs/subcortex-hook' letta SessionStart 2>/dev/null || true", "timeout": 5000, "quiet": true } ] }
    ]
  }
}
```

- Optional observe-only PostToolUse (stats; it **cannot** replace output and it is *awaited* for client tools, so it adds latency to every shell call):
  `"PostToolUse": [ { "matcher": "Bash|exec_command|shell_command|ShellCommand|shell|Shell", "hooks": [ { "type":"command", "command":"'/abs/subcortex-hook' letta PostToolUse 2>/dev/null || true", "timeout":5000, "quiet":true } ] } ]`
  - Shell tool names are toolset-dependent: Claude toolset (default for non-OpenAI models) = `Bash`; Codex toolset (OpenAI / ChatGPT OAuth) = `exec_command` (+ `write_stdin`). Also registered: `shell_command`, `ShellCommand` (`STREAMING_SHELL_TOOLS` in `manager.ts` also lists `shell`, `Shell`). Hooks receive the **internal** name.
- Tagging: no `name`/`id` field exists. Tag = the command string. Uninstall = remove every `HookCommand` whose `command` starts with `'/abs/subcortex-hook' letta ` (then drop empty groups/arrays). Merge into existing arrays; never replace the whole `hooks` object (user hooks and `disabled` live there).
- Shell: command runs via `/bin/zsh -c` first on macOS, then `$SHELL -c`, then bash/sh fallbacks (`buildShellLaunchers`). cwd = `working_directory` (process cwd). If `LETTA_BASH_STRICT` is truthy a strict-shell prelude is prepended (`withStrictShellPrelude`); harmless for our one-liner because `|| true` still forces exit 0 (UNVERIFIED if the prelude sets `-e`/`pipefail` in a way that matters).
- Env given to the hook: parent env minus `LETTA_AGENT_ID/AGENT_ID/LETTA_CONVERSATION_ID/CONVERSATION_ID/LETTA_MEMORY_DIR/MEMORY_DIR/USER_CWD/LETTA_WORKING_DIR`, plus `LETTA_HOOK_EVENT`, `LETTA_WORKING_DIR`, `USER_CWD`, and `LETTA_AGENT_ID`/`AGENT_ID` when the payload has `agent_id`. **`LETTA_CONVERSATION_ID` is stripped and not re-set.**

## 4. Events: stdin and responses

**Payload basics**
- stdin = `JSON.stringify(input)`, no trailing newline, then EOF. snake_case keys. Undefined keys omitted.
- Base: `event_type`, `working_directory`, `session_id?`. **`session_id` is never populated by any runner in 0.32.15** — key state by `conversation_id` (fallback `agent_id`).
- **No `transcript_path` on any event.**

Event list (`types.ts`): PreToolUse, PostToolUse, PostToolUseFailure, PermissionRequest, UserPromptSubmit, Notification, Stop, SubagentStop, PreCompact, SessionStart, SessionEnd.

Surfaces: UserPromptSubmit / SessionStart / manual-`/compact` PreCompact are wired in the **interactive TUI** (`use-submit-handler.ts`, `AppCoordinator.tsx`) and listener `/compact`. `letta -p` (headless) does **not** call `runUserPromptSubmitHooks`/`runSessionStartHooks` (no import in `src/headless*.ts`); tool hooks and accumulator-driven PreCompact/server-tool PostToolUse still fire there.

### (a) Prompt submit → UserPromptSubmit ✅
stdin:
```json
{"event_type":"UserPromptSubmit","working_directory":"/Users/me/proj","prompt":"rename getUser to fetchUser","is_command":false,"agent_id":"agent-3f2c…","conversation_id":"conv-91ab…"}
```
- Skipped entirely for slash commands (`is_command` true → no hooks run) and for system-only turns (task notifications with no user text).
- Hooks run **sequentially**; the whole chain is awaited before the prompt is sent.
- Output semantics (`executeHooks` + submit handler):
  - exit 0 → `stdout.trim()` (if non-empty) is collected **verbatim; JSON is not parsed**. All collected strings are joined with `\n`, wrapped as `<system-reminder>\n…\n</system-reminder>`, and pushed as a **separate text part prepended to the user message** (`pushReminder(userPromptSubmitHookFeedback)`, after SessionStart feedback, bash-mode prefix; before memory-git reminder).
  - Per-string cap 10,000 chars (`LIMITS.HOOK_OUTPUT_CHARS`); longer text is saved to a file and replaced by a 2,000-char preview + path.
  - `{}` would be injected as the literal text `{}`. Emit **plain text** or nothing.
- What to emit: plain text hint (plus, when a re-inject flag is pending, the snapshot — see (d)). Empty stdout when nothing to say.
- Queued prompts (agent busy): the hook runs at submit, the message is enqueued and the hook feedback from that run is discarded; the dequeue path re-enters the submit handler (hooks likely run again). UNVERIFIED end-to-end.

### (b) After shell tool → replace output ✗ (hooks); ✅ via mod (§7)
PostToolUse stdin (client tool, `manager.ts`):
```json
{"event_type":"PostToolUse","working_directory":"/Users/me/proj","tool_name":"Bash","tool_input":{"command":"npm test"},"tool_call_id":"toolu_01…","tool_result":{"status":"success","output":"…full displayable text…"},"agent_id":"agent-3f2c…"}
```
(`preceding_reasoning` / `preceding_assistant_message` are only filled on the server-side-tool path.)
- PostToolUse hooks run in **parallel** and are **awaited** for client tools (fire-and-forget for server-side tools).
- The only thing fed back is **appended**: exit-0 JSON `hookSpecificOutput.additionalContext` or top-level `additionalContext` (string) → `toolReturn + "\n\n[Hook feedback]:\n" + …`. Raw non-JSON stdout is ignored.
- **Replacement is impossible via hooks.** (A PreToolUse `hookSpecificOutput.updatedInput` could rewrite `tool_input.command` to pipe through a filter — `manager.ts` shallow-merges it into args — but that changes the executed command and exit semantics; not a genuine replacement. Not recommended.)
- PostToolUseFailure (tool threw): `error_message`, `error_type`; same append-only semantics.

### (c) Pre-compaction → PreCompact ◐
stdin:
```json
{"event_type":"PreCompact","working_directory":"/Users/me/proj","context_length":183412,"agent_id":"agent-3f2c…"}
```
- Two firing paths:
  1. **Manual `/compact`** (TUI `use-submit-handler.ts`; listener `commands.ts`): awaited, fires **before** the compaction request. `context_length`/`max_context_length` are `undefined`; `agent_id` and `conversation_id` present.
  2. **Automatic** (`accumulator.ts`): fires when the stream delivers `event_message` with `event_type:"compaction"`. **Not awaited** (fire-and-forget `.catch`). `context_length` = last known context tokens; `conversation_id` is **undefined**; `max_context_length` undefined. On the local backend that chunk is emitted **after** compaction has already run (`pi-stream-adapter.ts emitCompactionChunks`); on Letta Cloud compaction is server-side. So this is effectively "compaction happened", not "about to happen".
- Return value: ignored **except** exit 2 on the manual path, which **blocks `/compact`** ("Compact blocked: …") even though the type comment says "cannot block". Always exit 0.
- **No transcript path.** Snapshot options:
  - Letta persists all messages (compaction only changes the in-context buffer), so the recent history is still retrievable after the fact: `letta messages list --agent "$LETTA_AGENT_ID" --conversation <conversation_id|default> --limit 20 --order desc` (JSON only; uses the CLI's auth; `LETTA_AGENT_ID` is in the hook env when `agent_id` is set). On the auto path `conversation_id` is missing and `LETTA_CONVERSATION_ID` is stripped, so the "default" conversation may be the wrong one. Spawning a second Node CLI costs ~1 s; fine on the fire-and-forget auto path, keep under the timeout on the manual path. Output shape and auth behaviour UNVERIFIED at runtime.
  - Or rely on subcortex's own rolling record of user prompts from UserPromptSubmit (user text only).
- Never veto: exit 0, empty stdout.

### (d) Post-compaction → re-inject ◐
- There is **no post-compaction hook** and SessionStart does **not** fire after compaction (it fires on app start when the agent loads, `/new`, `/clear`, conversation switch/resume; always with `is_new_session` computed per path).
- Strategy: PreCompact sets `pending_reinject[conversation_id ?? agent_id]`; the **next UserPromptSubmit** (which has both ids) prints the snapshot + hint as plain text and clears the flag. Limitation: nothing is injected mid-turn after an auto-compaction; Letta itself re-injects its own reminders after compaction (`markPostCompactionContextRemindersPending`).
- SessionStart stdin: `{"event_type":"SessionStart","working_directory":…,"is_new_session":true,"agent_id":…,"agent_name":…,"conversation_id":…}`. **stdout of every SessionStart hook is collected regardless of exit code** (even exit 1/2), prefixed `[SessionStart hook context]:`, wrapped in `<system-reminder>` and prepended to the **first user message** of that session only. Useful for a one-time hint; not a compaction signal.

## 5. Exit codes, timeouts, responses to avoid

| Result | Effect |
|---|---|
| exit 0 | ALLOW. UserPromptSubmit: stdout injected. PreToolUse: `hookSpecificOutput.updatedInput` (JSON) rewrites args. PostToolUse: JSON `additionalContext` appended. SessionStart: stdout injected. |
| **exit 2** | BLOCK: UserPromptSubmit → prompt not sent (stderr shown as `<user-prompt-submit-hook>` status); PreToolUse → tool returns "Error: Tool execution blocked by hook"; PermissionRequest → deny; Stop/SubagentStop → keeps agent going; **manual PreCompact → `/compact` aborted**; PostToolUse(Failure) → `[cmd]: stderr` appended as feedback. Sequential runners stop at the first block. |
| exit 1 / other / signal / **timeout** | ERROR (non-blocking) — but **`Hook error: <stderr>`** (or `Hook error: Hook timed out after Nms` / spawn error) is pushed into feedback: for **UserPromptSubmit it is injected into the model's `<system-reminder>`**, for PostToolUse it is **appended to the tool output**. |

- Timeout default 60000 ms (command hooks); SIGTERM, then SIGKILL after 1 s.
- Our wrapper (`2>/dev/null || true`) guarantees exit 0 and no stderr, so only a **timeout** can leak an error string. Keep hooks well under the configured `timeout` (<1 s target; UserPromptSubmit 5000 ms budget is generous).
- Never emit: exit 2; PreToolUse `hookSpecificOutput.updatedInput`; PostToolUse `additionalContext` (unless an append is intended); `type:"prompt"` hooks.
- stdin write errors (EPIPE) are ignored by Letta; the hook may exit without reading stdin.

## 6. MCP (`subcortex mcp`)

- **Per-agent only.** Client-local MCP servers live in `~/.letta/settings.json` → `agents[]` entry matched by `agentId` + `baseUrl` (absent = Letta API/cloud; `localhost:8283` or `local:/path` for others):
  ```json
  {"agents":[{"agentId":"agent-3f2c…","mcpServers":[{"name":"subcortex","transport":"stdio","command":"/abs/subcortex","args":["mcp"],"cwd":"/Users/me/proj"}]}]}
  ```
  `StdioMcpServerConfig` = `{name, transport?:"stdio", command, args?, env?, cwd?}`.
- Interactive registration (per agent, from inside the TUI): `/mcp add --transport stdio subcortex /abs/subcortex mcp` (optional `--cwd PATH`, repeatable `--env K=V`; `cwd` defaults to the TUI's `process.cwd()` at add time). Errors if the name already exists for that agent.
- **No non-interactive `letta mcp add`**: `letta mcp` only has `list|get|tools|schema|search|call`.
- Writing `agents[].mcpServers` from outside is possible but fragile: `agents` is a Letta-managed key that Letta rewrites (pins, memfs, toolsets) from its in-memory copy, and agent ids are server-specific. Prefer `/mcp add`, or expose subcortex tools globally through a mod (`letta.tools.register`, §7).
- Server-side MCP (Letta API `/v1/mcp-servers`, ADE) is separate; hosted Letta Cloud cannot run stdio servers.

## 7. Recommended alternative: a Letta mod (`~/.letta/mods/subcortex.mjs`)

Trusted local JS loaded at startup and on `/reload` (no trust prompt). Disabled by `letta --no-mods` or `LETTA_DISABLE_MODS=1`. Factory = `export default function activate(letta) {…; return dispose}` (or named `activate`). `.ts/.tsx` are transpiled with `ts.transpileModule`; `.mjs` avoids that. Handlers may be async; a throwing handler is isolated and recorded to `~/.letta/mods/diagnostics/latest.json`.

| Behaviour | Mod event | Contract (from `src/mods/types.ts`, `events.md`, `manager.ts`) |
|---|---|---|
| B1 | `turn_start` `{agentId, conversationId, input: MessageCreate[]}` | mutate `event.input` or return `{input}`; **never** return `{cancel}` |
| B2 | `tool_end` `{agentId, conversationId, toolCallId, toolName, args, status, output}` | return `{result:{status: event.status, output: replacement}}` → **replaces** the tool return (`executeTool` applies `override.output`). Only string results; first handler returning `result` wins; `toolName` is the name the model called (`Bash`, `exec_command`, …); `output` already includes any `[Hook feedback]` appendix. |
| B3 | `compact_start` `{agentId, conversationId, trigger: manual\|context_window_overflow\|context_window_limit}` | notification-only, fires **before** compaction with history intact. **Local backend only** (`installLocalBackendModEventHooks` no-ops on cloud). `ctx.conversation.getHistory()` gives recent messages. |
| B4 | `compact_end` `{…, messagesBefore, messagesAfter, contextTokensBefore, contextTokensAfter}` | set a flag; inject on next `turn_start`. Local backend only. |

The mod can shell out to `subcortex-hook` (via `node:child_process.execFile`, not a shell string) or talk to the subcortex daemon. It must catch everything and fall through (return `undefined`) on error.

## 8. Unverified / flagged

- Whether dequeued (busy-queued) prompts re-run UserPromptSubmit and inject its output (likely yes; not traced to the end).
- Exact JSON shape/latency/auth of `letta messages list` inside a hook.
- Cloud-backend timing of the `compaction` event_message relative to server-side compaction (local backend: after).
- Mod `turn_start` ordering relative to Letta's own reminder parts; mod behaviour on the desktop/listener surface (docs: listener loads tools/commands/providers and tool/turn events, not UI).
- Hook schema is unvalidated; a malformed `hooks` value (non-array) may throw inside the runner rather than be ignored.
- `quiet` suppresses TUI echo only; not verified whether any other surface still logs hook output.
