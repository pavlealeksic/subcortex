# Gemini CLI — subcortex hook adapter spec

Target: **@google/gemini-cli v0.60.0** (latest stable, released 2026-09-15; npm `latest` = 0.60.0 on 2026-09-21).
I diffed the hook files below against `v0.62.0-nightly.20260921` and found no changes: `types.ts`, `hookRunner.ts`, `hookRegistry.ts`, `hookPlanner.ts`, `hookAggregator.ts`, `coreToolHookTriggers.ts` and `chatCompressionService.ts`.

Legend: **[src]** = verified in upstream source at tag v0.60.0 · **[doc]** = official docs only · **[UNVERIFIED]** = inference, or not checked end-to-end.

---

## 1. Sources, versions, detection

Sources:
- Docs: https://geminicli.com/docs/hooks/ · https://geminicli.com/docs/hooks/reference/ · https://geminicli.com/docs/hooks/writing-hooks/ · https://geminicli.com/docs/tools/mcp-server/
- Repo docs @v0.60.0: `docs/hooks/{index,reference,writing-hooks,best-practices}.md`, `docs/reference/configuration.md`, `docs/cli/trusted-folders.md`, `docs/cli/enterprise.md`
- Source @v0.60.0 (https://raw.githubusercontent.com/google-gemini/gemini-cli/v0.60.0/…):
  `packages/core/src/hooks/{types,hookRunner,hookAggregator,hookEventHandler,hookPlanner,hookRegistry,hookSystem,trustedHooks}.ts`,
  `packages/core/src/core/{coreToolHookTriggers,client}.ts`, `packages/core/src/scheduler/{scheduler,tool-executor,hook-utils}.ts`,
  `packages/core/src/context/chatCompressionService.ts`, `packages/cli/src/gemini.tsx`, `packages/cli/src/ui/AppContainer.tsx`,
  `packages/cli/src/ui/commands/clearCommand.ts`, `packages/cli/src/config/{config,settings,settingsSchema,settings-validation}.ts`,
  `packages/cli/src/utils/envVarResolver.ts`, `packages/core/src/utils/{paths,trust,shell-utils}.ts`, `packages/core/src/config/{config,storage}.ts`,
  `packages/core/src/tools/{shell,mcp-tool}.ts`, `packages/core/src/tools/definitions/base-declarations.ts`, `packages/cli/src/commands/mcp/add.ts`,
  `packages/core/src/utils/fileUtils.ts`, `integration-tests/hooks-system.test.ts`
- Release notes (GitHub releases v0.15.0 to v0.60.0), npm registry `@google/gemini-cli`.

Version history of hooks [src: release notes]:
| Version | Change |
|---|---|
| v0.15.0 (2025-11-14) | Hook execution engine lands. Opt-in only. |
| v0.19–v0.21 | Events, compression and session-lifecycle integration, `/hooks` panel, `gemini hooks migrate`. |
| v0.24.0 (2026-01-14) | `hooks.enabled` setting. SessionStart context injection. Hooks get folder-trust support. **Folder trust default becomes untrusted.** |
| v0.26.0 | `hooks` object may contain only event names. Toggles move to `hooksConfig`. |
| **v0.27.0 (2026-02-04)** | **Hooks enabled by default.** Injected context is wrapped in `<hook_context>` tags. |
| v0.28.0 | Legacy `tools.enableHooks` removed. |
| v0.31.0 | AfterTool `tailToolCallRequest` added. |

- **Minimum to support: v0.27.0.** It is the first version where hooks are on by default and use the current schema.
- **Recommended: ≥0.28.0.**
- For 0.15–0.26 you must also opt in. The exact legacy keys are **[UNVERIFIED]**: `tools.enableHooks: true` and `hooks.enabled`/`hooksConfig.enabled`.

Detection:
- `gemini --version` prints the bare version from package.json (yargs `.version(getVersion())`) [src].
- Package: `@google/gemini-cli`. Binary: `gemini` (`bundle/gemini.js`). Needs Node ≥20.
- Offline check: read `version` from `<npm root -g>/@google/gemini-cli/package.json`.

## 2. Config locations, isolation, enablement, trust

| Scope | Path | Notes |
|---|---|---|
| User | `$GEMINI_CLI_HOME/.gemini/settings.json`, or `~/.gemini/settings.json` | [src] `Storage.getGlobalGeminiDir()` = `join(homedir(), '.gemini')`. `homedir()` returns `GEMINI_CLI_HOME` when it is set. |
| Project | `<cwd>/.gemini/settings.json` | Ignored entirely in an untrusted folder. |
| System | `/etc/gemini-cli/settings.json` (Linux) | Override with `GEMINI_CLI_SYSTEM_SETTINGS_PATH` [src]. |
| System defaults | platform path | Override with `GEMINI_CLI_SYSTEM_DEFAULTS_PATH` [src]. |

Merge behaviour:
- Each hooks event array uses `mergeStrategy: CONCAT` across scopes [src].
- Duplicates are dropped at plan time by the key `name + ":" + command` [src `getHookKey`, `hookPlanner.deduplicateHooks`].

Env vars for isolated e2e tests:
- `GEMINI_CLI_HOME=/tmp/x`: a **home replacement**, so the CLI uses `/tmp/x/.gemini/…` (settings, trustedFolders.json, trusted_hooks.json, tmp/chats, mcp-server-enablement.json) [src + configuration.md].
- `GEMINI_CLI_SYSTEM_SETTINGS_PATH=/tmp/x/sys.json` and `GEMINI_CLI_SYSTEM_DEFAULTS_PATH=/tmp/x/sysdef.json`: keep the machine's `/etc` config out [src].
- `GEMINI_CLI_TRUSTED_FOLDERS_PATH`: relocates `trustedFolders.json` [doc].
- `GEMINI_CLI_TRUST_WORKSPACE=true` (or `--skip-trust`): trust the cwd for the session. `GEMINI_CLI_TRUST_WORKSPACE=false` or `GEMINI_RESTRICTED_MODE=true` force untrusted [src `utils/trust.ts`].
- Auth: `GEMINI_API_KEY`.
- Offline model: hidden flag `--fake-responses <jsonl>` (and `--fake-responses-non-strict`, `--record-responses`) [src cli/config.ts; used by upstream integration tests]. It is hidden and unsupported, so **[UNVERIFIED]** that it is stable.
- Headless run: `gemini -p "…" --output-format json` (also `text` and `stream-json`).

Enablement:
- `hooksConfig.enabled` defaults to **true** (requires a restart).
- `false` means no HookSystem is created at all [src `config.initialize`].
- `hooksConfig.disabled: string[]` disables hooks by `name` (or by `command` when there is no name). It merges as UNION.
- `hooksConfig.notifications` (default true) shows a UI indicator while hooks run.

**Trust gotchas [src]:**
- `security.folderTrust.enabled` defaults to **true** in v0.60.0. The schema default is true and `docs/reference/configuration.md` says true; `docs/cli/trusted-folders.md` still says "disabled by default", which is stale.
- When the folder is untrusted, `HookRegistry.processHooksFromConfig()` skips **all** settings hooks. That includes **user-level** hooks from `~/.gemini/settings.json`, because the merged `hooks` are all registered as source `Project` and gated on `isTrustedFolder()`.
- `HookRunner` also refuses Project-source hooks in untrusted folders.
- **So in an untrusted folder subcortex hooks silently do not run.** That is fail-open, but you get no behaviour.
- Extension hooks still load when untrusted.
- Headless with trust enabled and an untrusted folder exits with `FatalUntrustedWorkspaceError` [doc]. E2E tests must set `GEMINI_CLI_TRUST_WORKSPACE=true`.
- Project-hook fingerprinting: new or changed **project** hooks (key `name:command`, stored in `~/.gemini/trusted_hooks.json`) produce a one-time warning and are then auto-trusted [src `hookRegistry.checkProjectHooksTrust`]. User-scope hooks are not fingerprinted.
- MCP servers do not connect in untrusted folders [doc].

Settings string expansion [src `envVarResolver`]:
- Every string in settings.json has `$VAR`, `${VAR}` and `${VAR:-default}` expanded at load time from the CLI's environment. Undefined variables are left as-is.
- At exec time `HookRunner` additionally substitutes `$GEMINI_PROJECT_DIR`, `$GEMINI_CWD`, `$GEMINI_PLANS_DIR`, `$GEMINI_SESSION_ID` and `$CLAUDE_PROJECT_DIR` with shell-escaped values.
- **Write an absolute binary path and avoid `$` in our commands.**

Hook process environment [src]:
- `sanitizeEnvironment(process.env)`. Redaction is off unless `security.environmentVariableRedaction.enabled`, or under GitHub Actions.
- Plus `GEMINI_PROJECT_DIR`, `GEMINI_CWD` (both = payload `cwd`), `GEMINI_PLANS_DIR`, `GEMINI_SESSION_ID`, `CLAUDE_PROJECT_DIR`.
- Plus the per-hook `env` object. `CommandHookConfig.env` exists in types but is **undocumented**.
- Spawn: `bash -c <command>` on Unix, PowerShell on Windows. `cwd` = payload `cwd`. stdin = one JSON document, then EOF.

## 3. Registration (user scope `~/.gemini/settings.json`)

Rules:
- `type: "command"` is required.
- `timeout` is in **milliseconds** (default 60000). There is no enforced max; Node's `setTimeout` limit of 2^31−1 applies.
- Matchers [src `hookPlanner`]:
  - Tool events (BeforeTool/AfterTool): `new RegExp(matcher).test(toolName)`, unanchored. An invalid regex falls back to an exact string compare.
  - Lifecycle events (SessionStart source, SessionEnd reason, PreCompress trigger): **exact string equality only**, so `"startup|resume"` matches nothing.
  - `""`, `"*"` or an omitted matcher match everything.
  - BeforeAgent receives no matcher context, so any matcher matches.
- `sequential: true` on any group makes that event's hooks run serially. Otherwise they run in parallel.
- Unknown event keys produce a UI warning ("Invalid hook event name") and are skipped [src]. **Never write Qwen/Claude event names here.**

```json
{
  "hooks": {
    "BeforeAgent": [
      { "hooks": [ { "type": "command", "name": "subcortex:BeforeAgent",
          "description": "managed-by=subcortex",
          "command": "'/usr/local/bin/subcortex' hook gemini BeforeAgent 2>/dev/null; exit 0",
          "timeout": 5000 } ] }
    ],
    "AfterTool": [
      { "matcher": "^run_shell_command$",
        "hooks": [ { "type": "command", "name": "subcortex:AfterTool",
          "description": "managed-by=subcortex",
          "command": "'/usr/local/bin/subcortex' hook gemini AfterTool 2>/dev/null; exit 0",
          "timeout": 10000 } ] }
    ],
    "PreCompress": [
      { "hooks": [ { "type": "command", "name": "subcortex:PreCompress",
          "description": "managed-by=subcortex",
          "command": "'/usr/local/bin/subcortex' hook gemini PreCompress 2>/dev/null; exit 0",
          "timeout": 5000 } ] }
    ],
    "SessionStart": [
      { "hooks": [ { "type": "command", "name": "subcortex:SessionStart",
          "description": "managed-by=subcortex",
          "command": "'/usr/local/bin/subcortex' hook gemini SessionStart 2>/dev/null; exit 0",
          "timeout": 5000 } ] }
    ]
  }
}
```

Why the `2>/dev/null; exit 0` wrapper is mandatory (details in §5):
- If the binary is missing, bash exits 127 with "command not found" on stderr.
- Gemini turns any exit code other than 0 or 1 plus non-JSON text into **`decision:"deny"`**. That would block every prompt.
- The wrapper forces exit 0 and silences stderr.
- On Windows (PowerShell): `& 'C:\…\subcortex.exe' hook gemini BeforeAgent 2>$null; exit 0`. Gemini appends `; if ($LASTEXITCODE -ne 0) {exit $LASTEXITCODE}`, which becomes unreachable. **[UNVERIFIED on Windows]**

CLI alternative: none. `gemini hooks` only has `migrate --from-claude`, which wrongly copies Claude's seconds into Gemini's millisecond field. `/hooks` in the TUI shows the panel and can `enable`/`disable` hooks by name.

Tagging and uninstall:
- Put our hooks in **their own definition objects**; never append into a user's group.
- Mark each with `name` = `subcortex:<Event>` (the name is also the `/hooks disable` handle and the dedupe key) and `description: "managed-by=subcortex"`.
- The command always starts with the absolute subcortex path followed by ` hook gemini `.
- Uninstall steps:
  1. For each event array, drop inner `hooks[]` items whose `name` starts with `subcortex:`, or whose `command` matches `/subcortex(\.exe)?'?\s+hook\s+gemini\s/`.
  2. Drop definition objects left with an empty `hooks`.
  3. Drop event keys left with an empty array.
  4. Remove `subcortex:*` entries from `hooksConfig.disabled`.
  5. Remove `mcpServers.subcortex`.
- Unknown extra keys are tolerated (Zod `.passthrough()` [src]), but do not rely on custom keys.
- Preserve comments and formatting where possible: Gemini itself uses a comment-preserving writer.

## 4. Events and I/O

Common stdin fields [src `createBaseInput`]:
`session_id`, `transcript_path` (JSONL `…/.gemini/tmp/<project>/chats/session-<ts>-<id8>.jsonl`, or `""` if recording is off), `cwd`, `hook_event_name`, `timestamp` (ISO-8601).

Transcript format [src]: JSONL records.
- Message types: `user`, `gemini` (with `toolCalls[]`, `thoughts`, `tokens`, `model`), `info`, `error`, `warning`.
- Also `$set` metadata updates and `$rewindTo` records.

### 4.1 Prompt submit: `BeforeAgent`
- Fires once per user prompt. It is de-duplicated per `prompt_id`; tool-response continuations do not re-fire it [src `client.fireBeforeAgentHookSafe`].
- Fires in both interactive and `-p` modes.
- `prompt` = `partToString(request)`. It may include expanded `@file` content. In `-p` mode it may also include a SessionStart `<hook_context>…</hook_context>` prefix, so strip that before classifying.

stdin:
```json
{"session_id":"3f0c2a7e-5b1d-4c8e-9a41-2d7e6b0f9c13",
 "transcript_path":"/Users/alice/.gemini/tmp/myproj/chats/session-2026-09-21T14-02-3f0c2a7e.jsonl",
 "cwd":"/Users/alice/src/myproj","hook_event_name":"BeforeAgent",
 "timestamp":"2026-09-21T14:02:11.512Z","prompt":"rename foo to bar in utils.ts"}
```

(a) Context injection:
```json
{"hookSpecificOutput":{"hookEventName":"BeforeAgent","additionalContext":"Prefer the most direct, minimal path."}}
```
- Effect [src]: `<`/`>` are escaped to `&lt;`/`&gt;`, and the text is appended as an extra text part `<hook_context>…</hook_context>` on this turn's user message.
- Multiple hooks' contexts are joined with `\n`.
- No-op output: `{}` (or empty stdout).

### 4.2 After tool: `AfterTool` (shell tool id `run_shell_command`)

stdin:
```json
{"session_id":"3f0c2a7e-…","transcript_path":"…","cwd":"/Users/alice/src/myproj",
 "hook_event_name":"AfterTool","timestamp":"2026-09-21T14:03:40.101Z",
 "tool_name":"run_shell_command",
 "tool_input":{"command":"npm test","description":"Run test suite","is_background":false},
 "tool_response":{"llmContent":"Output: > myproj@1.0.0 test\n> vitest run\n…\nExit Code: 1\nProcess Group PGID: 48213",
                  "returnDisplay":"…"}}
```

Payload notes:
- `tool_response` = `{llmContent, returnDisplay, error?}`. `error` is present only on tool error (`{message,type}`).
- Shell `llmContent` lines: `Output: …`, `Error: …` (if any), `Exit Code: N` (only if ≠0), `Signal:`, `Background PIDs:`, `Process Group PGID:`.
- `returnDisplay` may be a string or a structured ANSI object; use `llmContent`.
- MCP tools also carry `mcp_context`. Tail calls carry `original_request_name`.
- AfterTool sees the **full** output. Gemini's own truncation runs *after* the hook: `tools.truncateToolOutputThreshold`, default 40000 chars, shell and single-text-part MCP only. It keeps the first 20% and last 80%, puts a `... [N characters omitted] ...` marker in between, and saves the full text to `<projectTemp>/tool-outputs/…` [src `tool-executor.truncateOutputIfNeeded`, `fileUtils.formatTruncatedToolOutput`].

(b) Tool-output replacement:
- **There is no non-blocking replace field.** There is no `updatedToolOutput` equivalent.
- The available levers:

| Output | Effect [src `coreToolHookTriggers.executeToolWithHooks`] | Subcortex |
|---|---|---|
| `hookSpecificOutput.additionalContext` | Appended to llmContent as `\n\n<hook_context>…</hook_context>` (escaped). Can only **add**. | Allowed (append only) |
| `decision:"deny"`/`"block"` + `reason` | Result becomes the **error** `Tool result blocked: <reason>` (type EXECUTION_FAILED). Technically a replacement, but marked as a failed/blocked tool call. | **FORBIDDEN** |
| `continue:false` | Result becomes `Agent execution stopped by hook: …` (STOP_EXECUTION). Kills the agent loop. | **FORBIDDEN** |
| `hookSpecificOutput.tailToolCallRequest {name,args}` | The scheduler runs a new tool in place of this one and sends its result to the model under the original function name and callId [src `scheduler.ts` ~L776, `tool-executor.createSuccessResult`]. | See note |

- Tail-call note: this is the only non-error replacement path, e.g. `{name:"mcp_subcortex_echo",args:{text:"<head…marker…tail>"}}`. It is **not recommended** as a default:
  - The tail call goes through validation and policy, so it may prompt the user unless the MCP server has `trust:true`.
  - If the tool is not registered (MCP disconnected, untrusted folder), the model gets a **tool-not-found error instead of the shell output**.
  - BeforeTool and AfterTool re-fire for the tail tool.
  - **[UNVERIFIED end-to-end]**
- **Recommendation:** on Gemini, behaviour 2 is **append-only / no-op**. Rely on Gemini's native 40k head/tail truncation, or on the MCP fallback. Say plainly: *hook-based replacement is impossible without either a block (error result) or the risky tail call.*

### 4.3 Pre-compaction: `PreCompress`
- stdin adds `"trigger":"auto"|"manual"`.
- `manual` comes from `/compress` and its alias `/compact` (v0.34+). It uses force=true and always attempts compression.
- **When it fires [src `chatCompressionService.compress`]:** before the token-threshold check, on **every** `tryCompressChat`.
  - `client.processTurn` calls that on **every model request**: each user prompt and each tool-response continuation.
  - The only exceptions are empty history and `experimental.contextManagement: true`. That setting defaults to false; when it is on, PreCompress **never** fires.
  - So `trigger:"auto"` does **not** mean a compaction will happen. It will happen only if tokens ≥ `model.compressionThreshold` (default 0.5) × the model limit.
  - Docs call the hook "fired asynchronously", but the code **awaits** it, so it is on the hot path. Keep it fast (<100 ms) and cheap: snapshot-on-change, not a full rewrite.

```json
{"session_id":"3f0c2a7e-…","transcript_path":"…","cwd":"…","hook_event_name":"PreCompress",
 "timestamp":"2026-09-21T15:10:02.000Z","trigger":"auto"}
```

(c) Observe or veto:
- **Observe only.** The return value of `firePreCompressEvent` is discarded [src]: no `decision`, no `continue`, no `additionalContext`.
- `systemMessage` is shown to the user (UI hook message).
- **Cannot veto** by any means. Exit 2 or deny have no effect on compression; they only produce a warning.
- Output `{}`.

### 4.4 Session start: `SessionStart`
- `source` ∈ `"startup" | "resume" | "clear"` [src enum `SessionStartSource`].
- **There is no post-compaction SessionStart in Gemini.** Compression only emits an internal `ChatCompressed` UI event; there is no hook.

```json
{"session_id":"…","transcript_path":"…","cwd":"…","hook_event_name":"SessionStart",
 "timestamp":"2026-09-21T14:00:01.000Z","source":"startup"}
```

- Output: `{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"…"}}` [src].
  - Interactive (`AppContainer`): added as a **user** history message `<hook_context>…</hook_context>`.
  - `-p` mode (`gemini.tsx`): prepended to the prompt.
  - `/clear` (`clearCommand.ts`): additionalContext is **ignored**; only `systemMessage` is shown.
- `decision`/`continue` are ignored; startup is never blocked.

**Behaviour 4 on Gemini (re-inject after compaction), recommended design [UNVERIFIED design, mechanics are src]:**
1. Snapshot in `PreCompress`. It is cheap; skip the write if the transcript is unchanged.
2. Mark `pending_reinject` only when compaction actually happened:
   - For `trigger:"manual"`, set it directly.
   - For `auto`, optionally register a `BeforeModel` observer hook that returns `{}` and sets the flag when `llm_request.messages[0].content` contains `<state_snapshot>` and `messages[1]` is `"Got it. Thanks for the additional context!"` with a changed hash. That is Gemini's post-compression history shape [src `chatCompressionService`].
   - BeforeModel gets the whole history on stdin on every request, which costs latency. It is optional.
3. Inject the snapshot on the next `BeforeAgent` (next user prompt), or on the next `AfterTool` via append-only `additionalContext` if that comes first. Then clear the flag.

## 5. Exit codes, output parsing, timeouts [src `hookRunner.executeCommandHook` + `convertPlainTextToHookOutput`]

Parse target: `stdout.trim()`, or `stderr.trim()` **if stdout is empty**. JSON is parsed; if the result is a JSON string it is parsed again. Objects and arrays are accepted as output.

| Exit | stdout/stderr | Result |
|---|---|---|
| 0 | JSON object | Used as output. |
| 0 | empty + empty | No output, success. |
| 0 | non-JSON text (stdout, or stderr when stdout is empty) | `{decision:"allow", systemMessage:text}`, **displayed to the user**. |
| 1 | JSON object | success=false (UI warning "Hook(s) [name] failed … Press F12"), **but the JSON is still honoured**. |
| 1 | non-JSON text | `{decision:"allow", systemMessage:"Warning: "+text}` + failure warning. |
| **2** | non-JSON text in stdout/stderr | **`{decision:"deny", reason:text}` → BLOCK** (tool/turn/result/retry). |
| **2** | JSON object | JSON honoured (the integration test writes deny JSON to stderr). |
| 2 | both empty | No output. Not blocked, warning only. |
| **any other (3…255, 126, 127…)** | **non-JSON text** | **Same as 2 → `decision:"deny"`. Docs claim "warning"; source says block.** |
| killed by signal (exitCode null) | — | Treated as exit **0** (`exitCode || 0`). |
| timeout | — | SIGTERM, then SIGKILL after 5 s. success=false, output **discarded**. Warning only. |
| spawn error | — | Failure warning only. |

- Default timeout 60000 ms. There is no global cap or bus timeout.
- stdin EPIPE is ignored, so the hook may skip reading stdin.
- A failing hook produces a UI warning on every call.
- **Adapter contract:** always exit 0; always print exactly one JSON object (at least `{}`) to stdout; never write to stderr.

**Every response that blocks, denies, stops or alters control. The adapter must never emit these:**
- `decision: "deny"` or `"block"`:
  - BeforeTool: blocks the tool.
  - AfterTool: result becomes "Tool result blocked" error.
  - BeforeAgent: blocks the turn and **discards the prompt**.
  - AfterAgent: rejects the response and forces a retry.
  - BeforeModel/AfterModel: blocks the turn.
- `decision: "ask"`: BeforeTool forces a confirmation.
- `continue: false` (with or without `stopReason`):
  - BeforeTool/AfterTool: STOP_EXECUTION, kills the loop.
  - BeforeAgent: stops the turn (the prompt is kept).
  - AfterAgent: stops the session.
  - AfterModel: kills the loop.
- `hookSpecificOutput.clearContext: true` (AfterAgent): wipes model memory.
- `hookSpecificOutput.tailToolCallRequest` (AfterTool): replaces the tool result.
- `hookSpecificOutput.tool_input` (BeforeTool): rewrites the arguments.
- `hookSpecificOutput.llm_request` / `llm_response` (BeforeModel/AfterModel): overrides or skips the model call, or replaces a chunk.
- `hookSpecificOutput.toolConfig` (BeforeToolSelection): `mode:"NONE"` disables all tools.
- Exit code 2, **or any exit code ≥2 together with non-JSON stdout/stderr text**.
- Non-JSON stdout on exit 0 does not block, but is shown as `systemMessage` noise.

Safe to emit: `{}`, `hookSpecificOutput.hookEventName` + `additionalContext` (BeforeAgent, AfterTool, SessionStart), `suppressOutput`.

## 6. MCP fallback registration

`~/.gemini/settings.json`:
```json
{ "mcpServers": { "subcortex": {
    "command": "/usr/local/bin/subcortex", "args": ["mcp"],
    "timeout": 600000, "trust": false,
    "description": "managed-by=subcortex" } } }
```

- CLI equivalent: `gemini mcp add -s user subcortex /usr/local/bin/subcortex mcp`.
  - **The default scope is `project`**, and `project` errors when cwd is `$HOME`.
  - Remove: `gemini mcp remove -s user subcortex`.
  - Enable/disable state lives in `~/.gemini/mcp-server-enablement.json`.
- Model-visible tool names are `mcp_subcortex_<tool>` [src `mcp-tool.ts`].
- Tool calls prompt for confirmation unless `trust:true`, policy allows them, or yolo mode is on.
- The MCP server env is sanitized; pass variables explicitly via `env`.
- Not connected in untrusted folders.
- `$VAR` in `env` is expanded.

## 7. Differences vs Qwen Code (summary; see qwen-code.md §7)

| | Gemini CLI 0.60.0 | Qwen Code 0.24.3 |
|---|---|---|
| Event names | `BeforeAgent`, `AfterTool`, `PreCompress`, `SessionStart` | `UserPromptSubmit`, `PostToolUse`, `PreCompact`, `SessionStart` |
| Timeout unit | ms | seconds (values ≥1000 read as ms) |
| Lifecycle matcher | exact string | regex |
| SessionStart after compaction | **none** | `source:"compact"` |
| Pre-compaction fires | every model request (pre-threshold) | only when compaction proceeds |
| Exit ≥3 + text | **deny (blocks)** | non-blocking warning |
| User hooks in untrusted folder | **not run** (trust on by default) | run (trust off by default) |
| Config-dir env | `GEMINI_CLI_HOME` = home replacement | `QWEN_HOME` = the `.qwen` dir itself |
| `mcp add` default scope | project | user |
| MCP tool name | `mcp_subcortex_x` | `mcp__subcortex__x` |

## 8. Unverified / caveats

- Tail-call replacement (§4.2): mechanism is src-verified; behaviour end-to-end (confirmation UX, AfterTool re-entry) was not run.
- BeforeModel-based compaction detection (§4.4): the marker strings are from src, but they are prompt text and could change.
- PreCompress firing on every model request: derived from `client.processTurn` and `chatCompressionService`, not observed at runtime.
- Windows PowerShell wrapper and quoting.
- `--fake-responses` flag (hidden/testing-only).
- Exact opt-in keys for pre-0.27 versions.
- `CommandHookConfig.env` works (src) but is undocumented.
- The transcript path directory naming (`tmp/<project>/chats`) depends on the ProjectRegistry id. Always read `transcript_path`; do not compute it.
- `docs/cli/trusted-folders.md` ("disabled by default") contradicts the v0.60.0 schema and reference (default true). I followed source.
