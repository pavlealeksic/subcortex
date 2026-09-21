# Qwen Code — subcortex hook adapter spec

Target: **@qwen-code/qwen-code v0.24.3** (latest stable, released 2026-09-21; npm `latest` = 0.24.3).

Legend: **[src]** = verified in upstream source at tag v0.24.3 · **[doc]** = official docs · **[UNVERIFIED]** = inference, or not checked end-to-end.

---

## 1. Sources, versions, detection

Sources:
- Docs: https://qwenlm.github.io/qwen-code-docs/en/users/features/hooks/ · …/users/configuration/settings/ · …/users/features/mcp/
- Repo docs @v0.24.3: `docs/users/features/{hooks,mcp}.md`, `docs/users/configuration/{settings,trusted-folders,auth}.md`
- Source @v0.24.3 (https://raw.githubusercontent.com/QwenLM/qwen-code/v0.24.3/…):
  `packages/core/src/hooks/{types,hookRunner,hookAggregator,hookEventHandler,hookSystem,hookRegistry,hookPlanner,hook-matcher,hook-timeout,user-prompt-submit-context}.ts`,
  `packages/core/src/core/{toolHookTriggers,coreToolScheduler,client,llm-chat}.ts`, `packages/core/src/services/chatCompressionService.ts`,
  `packages/core/src/config/{config,storage}.ts`, `packages/core/src/confirmation-bus/message-bus.ts`,
  `packages/cli/src/config/{settingsSchema,settings,trustedFolders,hook-settings}.ts`, `packages/cli/src/commands/{hooks.tsx,mcp/add.ts,mcp/remove.ts}`,
  `packages/core/src/tools/{shell,tool-names,mcp-tool}.ts`, `packages/core/src/utils/shell-utils.ts`
- GitHub release notes v0.12.0 to v0.24.3; npm registry `@qwen-code/qwen-code`.

Version history of hooks [src: release notes and old tags]:
| Version | Change |
|---|---|
| v0.12.0 (2026-03-09) | Hooks infrastructure. Experimental; toggled by `hooksConfig.enabled`. |
| v0.13.0 | 10 core events, including `SessionStart` (sources startup/resume/clear/compact) and `PreCompact`. |
| **v0.14.0 (2026-04-03)** | Experimental flag removed, so hooks are on by default. `disableAllHooks` added. |
| v0.14.1 | JSON on exit 2 is parsed. |
| v0.14.2 | A signal kill is no longer treated as exit 0. |
| v0.14.4 | `PostCompact` added. |
| **v0.16.0 (2026-05-21)** | SessionStart `additionalContext` actually injected into chat context. |
| v0.17.1 | PreCompact `custom_instructions` and `additionalContext` plumbing. |
| v0.21.1 | `submitted_prompt` added. |
| **v0.23.4 (2026-09-14)** | **Breaking:** command-hook `timeout` is read in **seconds**; values ≥1000 are still read as ms. Plain-text stdout becomes `additionalContext` on SessionStart, UserPromptSubmit and UserPromptExpansion. |

- **Minimum to support: v0.16.0.** Before that, SessionStart context did not reach the model.
- **Recommended: ≥0.23.4.**
- Cross-version trick: write `timeout` as **≥1000 in milliseconds** (e.g. `10000`). It means 10 s on every version.

Detection:
- `qwen --version` prints the version (yargs) **[UNVERIFIED exact format]**.
- Package: `@qwen-code/qwen-code`. Binary: `qwen`. Needs Node ≥22.
- Offline check: `<npm root -g>/@qwen-code/qwen-code/package.json` → `version`.

## 2. Config locations, isolation, enablement, trust

| Scope | Path |
|---|---|
| User | `$QWEN_HOME/settings.json`, or `~/.qwen/settings.json` [src `Storage.getGlobalQwenDir`]. **`QWEN_HOME` is the `.qwen` directory itself**, not a home replacement. |
| Project | `<projectRoot>/.qwen/settings.json`. Loaded only in a trusted folder. |
| System | Linux `/etc/qwen-code/settings.json` · macOS `/Library/Application Support/QwenCode/settings.json` · Windows `C:\ProgramData\qwen-code\settings.json`. Override with `QWEN_CODE_SYSTEM_SETTINGS_PATH`. |
| System defaults | `…/system-defaults.json`. Override with `QWEN_CODE_SYSTEM_DEFAULTS_PATH`. |

Hook source order at run time: Project, User, System, Extensions [src `hookRegistry.getSourcePriority`].

Isolation env for e2e tests:
- `QWEN_HOME=/tmp/x/qwen`: settings, credentials, `trustedFolders.json`, MCP enablement. Added in v0.15.10.
- `QWEN_RUNTIME_DIR=/tmp/x/run`: projects/chats, debug logs, tmp. Defaults to `QWEN_HOME`.
- `QWEN_CODE_SYSTEM_SETTINGS_PATH=/tmp/x/sys.json` and `QWEN_CODE_SYSTEM_DEFAULTS_PATH=/tmp/x/sysdef.json`.
- `QWEN_CODE_TRUSTED_FOLDERS_PATH` [src].
- Model via an OpenAI-compatible mock: `OPENAI_API_KEY=x OPENAI_BASE_URL=http://127.0.0.1:PORT/v1 OPENAI_MODEL=m` [doc auth.md]. Upstream integration tests use a fake OpenAI server.
- Headless: `qwen -p "…"`. `--approval-mode yolo` or `--yolo` avoids tool prompts.
- Debug: `--debug` or `QWEN_DEBUG_LOG_FILE=1`. The log is `<runtime>/debug/latest`; look for `[HOOK_REGISTRY]` and `[TRUSTED_HOOKS]` lines.

Enablement:
- Hooks are **on by default**.
- `"disableAllHooks": true` (top level, requires restart) disables them. So do `--safe-mode`/`QWEN_CODE_SAFE_MODE` and `--bare`/`QWEN_CODE_SIMPLE`.
- There is **no** per-hook disabled list in v0.24.3; `hooksConfig` was removed from the schema.
- `enabled`, `disabled` and `notifications` keys inside `hooks` are skipped [src `HOOKS_CONFIG_FIELDS`].

Trust:
- `security.folderTrust.enabled` defaults to **false** [src schema + doc], so every folder is trusted.
- When folder trust is enabled and the folder is untrusted: project hooks are not loaded, and MCP `trust:true` is ignored.
- **User and system hooks always load, regardless of trust** [src `hookRegistry.processHooksFromConfig`].
- There is no hook fingerprint or consent prompt for settings hooks.
- Hot reload: opening the interactive `/hooks` menu reloads definitions. Otherwise they are read at session start. `disableAllHooks` needs a restart.

Settings string expansion: `$VAR`/`${VAR}` in settings.json are expanded at load time (`resolveEnvVarsInObject`) [src]. Use an absolute binary path and avoid `$`.

Hook process [src `hookRunner.executeCommandHook`]:
- Shell: `bash -c <command>` on Unix. On Windows the default is **cmd.exe** (`/d /s /c`); per-hook `"shell":"powershell"` switches it.
- `cwd` = payload `cwd`.
- env: `sanitizeChildEnv(process.env)`, which strips Qwen daemon secrets, plus `QWEN_PROJECT_DIR`, `CLAUDE_PROJECT_DIR`, `GEMINI_PROJECT_DIR` (all = `cwd`), shell-context variables, and the per-hook `env`.
- Bash commands are **not** text-substituted since v0.23.4; bash expands the variables itself, so double-quote them.
- The process runs in its own process group. On timeout or abort the whole tree is killed: SIGTERM, 2 s grace, then SIGKILL.
- stdout and stderr are each capped at 1 MiB.

## 3. Registration (user scope `~/.qwen/settings.json`)

Rules:
- `type:"command"` is required. `timeout` is in seconds (default 60); values ≥1000 are ms; non-positive or non-numeric values fall back to 60 s.
- Optional per-hook fields: `name`, `description`, `env`, `async` (fire-and-forget, output never delivered), `shell`, `statusMessage`.
- Matchers [src `hook-matcher.ts`], same rules for every event that supports them:
  - `""`, `"*"`, `".*"` or omitted match all.
  - An exact value (or tool alias) matches first.
  - A `|` list is compared entry-wise, unless it starts with `^` or `(`.
  - Otherwise it is an **unanchored regex**, so anchor it.
  - Tool matchers also accept Claude aliases (`"Bash"` → `run_shell_command`).
- `UserPromptSubmit` takes no matcher; `SessionStart` matches `source`; `PreCompact` matches `trigger`.
- Unknown event keys produce the warning "Invalid hook event name" and are skipped. **Never write Gemini names here.**

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "name": "subcortex:UserPromptSubmit",
          "description": "managed-by=subcortex",
          "command": "'/usr/local/bin/subcortex' hook qwen UserPromptSubmit 2>/dev/null; exit 0",
          "timeout": 10000 } ] }
    ],
    "PostToolUse": [
      { "matcher": "^run_shell_command$",
        "hooks": [ { "type": "command", "name": "subcortex:PostToolUse",
          "description": "managed-by=subcortex",
          "command": "'/usr/local/bin/subcortex' hook qwen PostToolUse 2>/dev/null; exit 0",
          "timeout": 10000 } ] }
    ],
    "PreCompact": [
      { "hooks": [ { "type": "command", "name": "subcortex:PreCompact",
          "description": "managed-by=subcortex",
          "command": "'/usr/local/bin/subcortex' hook qwen PreCompact 2>/dev/null; exit 0",
          "timeout": 10000 } ] }
    ],
    "SessionStart": [
      { "matcher": "^compact$",
        "hooks": [ { "type": "command", "name": "subcortex:SessionStart",
          "description": "managed-by=subcortex",
          "command": "'/usr/local/bin/subcortex' hook qwen SessionStart 2>/dev/null; exit 0",
          "timeout": 10000 } ] }
    ]
  }
}
```

Notes on the snippet:
- `timeout: 10000` means 10 s on all versions (legacy ms rule).
- **Keep UserPromptSubmit well under 60 s** (see §5, bus timeout).
- The `2>/dev/null; exit 0` wrapper makes a missing binary or a crash fail open. Qwen only blocks on exit 2, but the wrapper also suppresses "Warning:" system-message noise.
- Optionally add `PostToolUseFailure` with the same matcher. Shell commands with a non-zero exit (per `isShellExitError`) are reported as **tool errors**, so `PostToolUse` does **not** fire for them [src shell.ts]. PostToolUseFailure can only append context.
- Optionally add `PostCompact`. Its output is ignored, but its stdin carries `compact_summary` for the snapshot store.
- On Windows (cmd.exe): `"C:\…\subcortex.exe" hook qwen UserPromptSubmit 2>nul & exit /b 0` **[UNVERIFIED]**.

CLI: `qwen hooks` has no management subcommands. `/hooks` is a read-only browser that also reloads. Edit JSON directly.

Tagging and uninstall:
- Put our hooks in their own definition objects.
- Mark each with `name` = `subcortex:<Event>` and `description: "managed-by=subcortex"`. The command starts with the absolute subcortex path followed by ` hook qwen `.
- Registry dedupe key = `name:command` (per source).
- Uninstall steps:
  1. Remove inner hook items whose `name` starts with `subcortex:`, or whose command matches `/subcortex(\.exe)?['"]?\s+hook\s+qwen\s/`.
  2. Prune empty definition objects.
  3. Prune empty event arrays.
  4. Remove `mcpServers.subcortex`.
- There is no Zod validation of settings in Qwen [src: `validateSettings` absent in settings.ts]. Still, avoid custom keys.

## 4. Events and I/O

Common stdin fields [src `createBaseInput`]:
- `session_id`, `transcript_path` (JSONL under `<QWEN_RUNTIME_DIR|QWEN_HOME|~/.qwen>/projects/<sanitized-cwd>/chats/…`; always use the field, do not compute it), `cwd`, `hook_event_name`, `timestamp`.
- `permission_mode` (`default|plan|auto_edit|auto|yolo`).
- `prompt_id` when the event belongs to a turn.
- `agent_id` only inside subagents.
- `source_type`/`source_id` only for ACP sessions.
- The contract is forward-extensible: ignore unknown fields.

### 4.1 Prompt submit: `UserPromptSubmit`
- **Fires for more than user input** [src client.ts]: on `UserQuery`, **`ToolResult` (every tool-result continuation)** and `Hook` sends. It skips Retry, Steer, Cron, Notification, Teammate and Goal.
- `prompt` is the model-bound text, which on continuations is serialized function responses.
- `submitted_prompt` holds the real user text. It is present only for interactive TUI submissions and first-turn headless `UserQuery`, and absent for continuations, Vim-mode input, history recalls, and ACP clients without a declaration.
- **Adapter rule:** classify and inject only when `submitted_prompt` is present. Otherwise emit `{}`. Optional fallback: first event per `prompt_id`. **[UNVERIFIED]**

```json
{"session_id":"8b1f4c2e-9d77-4a0b-b5e3-0c6a1f2d9e44",
 "transcript_path":"/Users/alice/.qwen/projects/-Users-alice-src-myproj/chats/8b1f4c2e-9d77-4a0b-b5e3-0c6a1f2d9e44.jsonl",
 "cwd":"/Users/alice/src/myproj","hook_event_name":"UserPromptSubmit",
 "timestamp":"2026-09-21T14:02:11.512Z","permission_mode":"default",
 "prompt_id":"8b1f4c2e-9d77-4a0b-b5e3-0c6a1f2d9e44########1",
 "prompt":"rename foo to bar in utils.ts","submitted_prompt":"rename foo to bar in utils.ts"}
```
(The `transcript_path` filename and the `prompt_id` shape are illustrative **[UNVERIFIED]**.)

(a) Context injection:
```json
{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"Prefer the most direct, minimal path."}}
```
- Effect [src]: `<`/`>` are escaped, and the text is appended as its **own text part** `<qwen:user-prompt-submit-context>…</qwen:user-prompt-submit-context>`.
- The transcript records `systemPayload.hookContext`; the UI shows the original prompt.
- Plain-text stdout with exit 0 would *also* become context (v0.23.4+). Never print logs to stdout.

### 4.2 After tool: `PostToolUse` (shell id `run_shell_command`; Claude alias `Bash` accepted)

```json
{"session_id":"8b1f4c2e-…","transcript_path":"…","cwd":"/Users/alice/src/myproj",
 "hook_event_name":"PostToolUse","timestamp":"2026-09-21T14:03:40.101Z","permission_mode":"default",
 "prompt_id":"…","tool_name":"run_shell_command",
 "tool_input":{"command":"ls -R node_modules","is_background":false,"description":"List deps"},
 "tool_response":{"llmContent":"Command: ls -R node_modules\nDirectory: (root)\nOutput: …\nError: (none)\nExit Code: 0\nSignal: (none)\nProcess Group PGID: 48213",
                  "returnDisplay":"…"},
 "tool_use_id":"toolu_1790000000000_ab12cd34e","tool_call_id":"call_9f8e7d6c","duration_ms":5231}
```

Payload notes:
- Shell `returnDisplay` is normalized to plain text [src `normalizeHookDisplayResponse`].
- **Qwen's shell tool truncates in-tool before the hook sees the output.** It keeps head and tail (`keep:'both'`) within a per-tool char budget; the defaults are `tools.truncateToolOutputThreshold` 25000 chars and `tools.truncateToolOutputLines` 1000. A later scheduler gate persists oversized results to disk. So "huge" outputs rarely reach PostToolUse intact [src shell.ts, coreToolScheduler].

(b) Tool-output replacement: **impossible without blocking. Say it plainly.** [src `toolHookTriggers.firePostToolUseHook`, `coreToolScheduler` ~L226k]

| Output | Effect | Subcortex |
|---|---|---|
| `hookSpecificOutput.additionalContext` | Appended **after** Qwen's truncation as `content + "\n\n" + ctx` (escaped). Add-only. | Allowed |
| `hookSpecificOutput.artifacts` | Attaches UI artifacts. | Don't |
| `continue:false` (+`stopReason`) | Result **replaced by an error** `stopReason` (EXECUTION_DENIED; span "post hook stopped"). | **FORBIDDEN** |
| `decision:"block"/"deny"` + `reason` | **Not read** by the v0.24.3 PostToolUse path (only `continue` and `additionalContext` are read), even though the docs list it. Exit 2 maps to `decision:deny`, which is therefore also ignored here. | **FORBIDDEN** (documented as blocking; could be wired later) |

- There is no `updatedToolOutput` / `updatedMCPToolOutput`.
- **Recommendation:** behaviour 2 on Qwen is append-only / no-op. Rely on Qwen's native head/tail truncation (tunable via the settings above, which require a restart), or on the MCP fallback.

### 4.3 Pre-compaction: `PreCompact`
- stdin adds `"trigger":"manual"|"auto"` and `"custom_instructions":"<text from /compress …>"`.
- **Fires only when compaction will actually run** [src `chatCompressionService`]: after the token-threshold gate (`effectiveTokens ≥ auto threshold`, or screenshot overflow) and only when history has ≥2 entries.
- `manual` = `/compress` (optionally with instructions).
- Awaited synchronously, directly and not over the bus, before the summary side-query.

```json
{"session_id":"8b1f4c2e-…","transcript_path":"…","cwd":"…","hook_event_name":"PreCompact",
 "timestamp":"2026-09-21T15:10:02.000Z","permission_mode":"default","trigger":"auto","custom_instructions":""}
```

(c) Observe or veto:
- **Observe.** Only `hookSpecificOutput.additionalContext` is used [src]. It is trimmed, capped (MAX_HOOK_INSTRUCTIONS_CHARS), `<`/`>` escaped, and appended to the **summarizer system prompt** as "Additional Instructions". It is not a veto.
- `decision`/`continue`/exit 2 have **no effect**, so it **cannot veto**. Hook errors are caught and compaction proceeds.
- Emit `{}`. Only emit `additionalContext` if you deliberately want to steer the summary.
- `PostCompact` (optional, output ignored) fires only after a successful compaction, with `compact_summary`. It is waited for.

### 4.4 Session start: `SessionStart`
- `source` ∈ `"startup" | "resume" | "clear" | "compact"`. The enum also has `"branch"`, which the docs do not list [src types.ts].
- Also carries `model`, `permission_mode`, and optional `agent_type`.
- **Post-compaction firing [src client.ts]:**
  - Manual `/compress`: `tryCompressChat` → `startChat(history, Compact)` → the hook is **awaited**, then the context is applied. If our hook returns nothing, the previous (startup) SessionStart context is re-applied.
  - Auto compaction: on the `ChatCompressed` stream event, `void fireSessionStartHook(Compact).then(apply)`. This is **fire-and-forget**; the context is applied when the hook resolves, possibly after the next request has already been sent.
  - Only fires if `hasHooksForEvent('SessionStart')`.

```json
{"session_id":"8b1f4c2e-…","transcript_path":"…","cwd":"…","hook_event_name":"SessionStart",
 "timestamp":"2026-09-21T15:10:09.000Z","permission_mode":"default","source":"compact","model":"qwen3-coder-plus"}
```

Response:
```json
{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"Pre-compaction snapshot: …"}}
```
- Effect [src `llm-chat.applySessionStartContext`]: `<`/`>` are escaped.
- The text is placed in the **system instruction** as `\n\n<qwen:session-start-context hidden="true">\nSessionStart additional context:\n…\n</qwen:session-start-context>`.
- It **replaces** any previous SessionStart block and **persists** until the next SessionStart (another compaction, clear or resume). It is not one-shot. Keep it short.
- Plain-text stdout on exit 0 is also treated as context (v0.23.4+).
- `decision`/`continue` are not consulted on this path [src `fireSessionStartHook` returns only `getAdditionalContext()`].

## 5. Exit codes, output parsing, timeouts [src `hookRunner` close handler + `convertPlainTextToHookOutput`]

| Exit | Output handling | Result |
|---|---|---|
| 0 | stdout JSON object (a double-encoded JSON string is also accepted) | Used as output. |
| 0 | stdout non-JSON text | SessionStart/UserPromptSubmit/UserPromptExpansion: **becomes `additionalContext` (model-visible)**. Other events: `systemMessage` (shown). |
| 0 | stdout starts with `{` but invalid JSON | Ignored: non_blocking_error, nothing reaches the model. |
| 0 | stdout empty, stderr present | stderr parsed. A JSON object is used; plain text becomes `systemMessage`. |
| 0 | both empty | No output. |
| **2** | stdout **ignored**; stderr JSON object used as output; stderr plain text → **`{decision:"deny", reason}`** | **Blocking** where the event honours decisions. |
| 1 / any other non-zero | stdout (else stderr) JSON object **still used**; plain text → `{decision:"allow", systemMessage:"Warning: …"}` | Non-blocking. |
| killed by signal (null) | as above | non_blocking_error. |
| timeout | process-group kill, no output | outcome `timeout`, non-blocking. |

Timeouts:
- Default 60 s. Values ≥1000 are ms. The maximum usable timer is 2^31−1 ms.
- **MessageBus request timeout = 60 s** for events dispatched over the bus: UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure, Notification, PermissionRequest, and others. PostToolBatch has its own 15 s limit.
- **UserPromptSubmit: if the bus request rejects (timeout or abort), `client.ts` rethrows and the user's turn FAILS** ("UserPromptSubmit hook failed"). Configure `timeout` well under 60 s (10 s recommended) so the runner kills the hook first.
- For PostToolUse, a bus failure only records a `hookError` and continues.
- PreCompact and SessionStart are called directly (no bus timeout).

**Every response that blocks, denies, stops or alters control. The adapter must never emit these:**
- `decision: "block"` or `"deny"`:
  - UserPromptSubmit: turn blocked (`UserPromptSubmitBlocked`).
  - PreToolUse: tool denied.
  - Stop/SubagentStop: forces the agent to keep going (capped by `stopHookBlockingCap`, default 8).
  - PostToolBatch: last result replaced with an error.
  - UserPromptExpansion: blocked.
  - TodoCreated/TodoCompleted: blocked.
  - PostToolUse: documented as blocking (currently not read).
- `decision: "ask"`, and `decision: "allow"`/`"approve"` on PreToolUse: the fallback permission decision, where **allow skips the approval prompt**.
- `hookSpecificOutput.permissionDecision: "deny" | "ask" | "allow"` and `updatedInput` (PreToolUse).
- `hookSpecificOutput.decision {behavior, updatedInput, updatedPermissions, message, interrupt}` (PermissionRequest).
- `continue: false` / `stopReason`: UserPromptSubmit (blocked), PreToolUse (stop), PostToolUse (**result replaced by error**), PostToolBatch (stop), UserPromptExpansion (blocked), Stop.
- `terminalSequence` (Notification): raw terminal escapes.
- `hookSpecificOutput.artifacts`.
- **Exit code 2** on any event.
- Not blocking, but not to emit by accident:
  - Plain-text stdout on SessionStart/UserPromptSubmit gets injected into model context.
  - PreCompact `additionalContext` steers the summarizer.

Safe to emit: `{}`, and `{"hookSpecificOutput":{"hookEventName":"<Event>","additionalContext":"…"}}` on UserPromptSubmit, PostToolUse, PostToolUseFailure and SessionStart.

## 6. MCP fallback registration

`~/.qwen/settings.json`:
```json
{ "mcpServers": { "subcortex": {
    "command": "/usr/local/bin/subcortex", "args": ["mcp"],
    "timeout": 600000, "trust": false,
    "description": "managed-by=subcortex" } } }
```

- CLI: `qwen mcp add subcortex /usr/local/bin/subcortex mcp`. **The default scope is `user`**; `-s project` writes `.qwen/settings.json`.
- Remove: `qwen mcp remove subcortex` (default scope user).
- `timeout` is in ms (default 600000).
- `trust:true` skips confirmations only in trusted workspaces.
- `versionNegotiation` defaults to `"legacy"`.
- Model-visible tool names: `mcp__subcortex__<tool>` (double underscore) [src `mcp-tool.ts`].

## 7. Qwen vs Gemini differences (Qwen is a Gemini CLI fork, but the hook layers diverge)

| Topic | Qwen Code 0.24.3 | Gemini CLI 0.60.0 |
|---|---|---|
| Prompt event | `UserPromptSubmit`. Also fires on tool-result continuations; use `submitted_prompt`. | `BeforeAgent`. Once per prompt_id. |
| After-tool event | `PostToolUse` (success only) + `PostToolUseFailure` (non-zero shell exit lands here) | `AfterTool` (all outcomes; `error` field) |
| Output replacement | impossible (only `continue:false` → error) | only via deny (error) or `tailToolCallRequest` |
| Context wrapper | `<qwen:user-prompt-submit-context>` part. Tool: plain `\n\n` append. | `<hook_context>` part / append |
| Pre-compaction | `PreCompact`. Fires only on real compaction. `additionalContext` → summarizer prompt. | `PreCompress`. Fires before every model request (pre-threshold). Output ignored. |
| Post-compaction reinjection | `SessionStart` `source:"compact"`, stored in the system instruction (persistent) | none (workaround: BeforeAgent) |
| Exit 2 | blocks (stderr) | blocks (stderr) |
| Exit ≥3 / 127 + text | non-blocking warning | **deny / blocks** |
| Plain stdout, exit 0 | becomes model context on SessionStart/UserPromptSubmit | shown as systemMessage |
| Timeout unit / default | seconds (≥1000 = ms) / 60 s. Bus cap 60 s. | ms / 60000. No bus cap. |
| Lifecycle matcher | regex (anchor it) | exact string |
| Hook types | command, http, prompt (+ SDK function) | command (+ runtime) |
| Per-hook disable | none (`disableAllHooks` only) | `hooksConfig.disabled[]` |
| Trust default | folder trust **off**. User hooks always run. | folder trust **on**. User hooks skipped when untrusted. |
| Config-dir env | `QWEN_HOME` = the `.qwen` dir; `QWEN_RUNTIME_DIR` | `GEMINI_CLI_HOME` = home (contains `.gemini`) |
| Windows hook shell | cmd.exe | PowerShell |
| `mcp add` default scope | user | project |
| MCP tool naming | `mcp__srv__tool` | `mcp_srv_tool` |
| Hook env vars | `QWEN_PROJECT_DIR`, `CLAUDE_PROJECT_DIR`, `GEMINI_PROJECT_DIR` | `GEMINI_PROJECT_DIR`, `GEMINI_CWD`, `GEMINI_SESSION_ID`, `GEMINI_PLANS_DIR`, `CLAUDE_PROJECT_DIR` |

## 8. Unverified / caveats

- `qwen --version` output format.
- Windows cmd.exe wrapper quoting.
- The exact transcript filename.
- The `prompt_id` format shown in the samples.
- Minimum-version claims are based on release notes plus spot-checks of v0.13/v0.14/v0.16 `types.ts` and schema, not full behaviour tests.
- `submitted_prompt` / first-per-`prompt_id` gating for UserPromptSubmit is a design recommendation. Whether `ToolResult` continuations reuse the same `prompt_id` was not traced.
- PostToolUse `decision:block` being ignored is true for v0.24.3 source (`firePostToolUseHook`). The docs describe it as supported, so treat it as forbidden regardless.
- Auto-compaction SessionStart(compact) is async. Whether the snapshot lands before the next model request is timing-dependent.
- `isShellExitError` routes non-zero shell exits to PostToolUseFailure. Some commands with benign non-zero codes may be exempt; the exact rules were not read.
