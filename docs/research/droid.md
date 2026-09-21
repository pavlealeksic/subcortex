# Factory Droid CLI (`droid`) — subcortex hook adapter spec

Status: verified 2026-09-21 against docs.factory.ai (current `harness/hooks`, archived April-2026 hooks reference,
settings, CLI reference, MCP, full changelog) **and** by static reading of the shipped **0.223.0** darwin-arm64 binary
(Bun-compiled; app JS is plaintext inside `@factory/cli-darwin-arm64@0.223.0` → `bin/droid`). "BIN" = read from that
binary. Where BIN contradicts the docs, BIN wins (it is what runs). Nothing was executed.

## 1. Sources, versions, detection

| Item | Value |
|---|---|
| Hooks (current) | https://docs.factory.ai/harness/hooks (`.md` suffix gives raw markdown); old URL https://docs.factory.ai/reference/hooks-reference redirects there |
| Hooks reference (Apr 2026, full Claude-style detail) | http://web.archive.org/web/20260403094501/https://docs.factory.ai/reference/hooks-reference |
| Settings | https://docs.factory.ai/droid-cli/settings |
| CLI reference | https://docs.factory.ai/droid-cli/cli-reference |
| MCP | https://docs.factory.ai/harness/mcp |
| Changelog | https://docs.factory.ai/changelog/release-notes |
| Latest version | **CLI v0.223.0 (September 19, 2026)** — changelog top entry; npm `droid@latest` = 0.223.0 |
| Version detection | `droid --version` / `droid -v` (output format assumed bare `0.223.0` — unverified). Payload has no version. Hook env always contains `FACTORY_PROJECT_DIR`, `DROID_PROJECT_DIR`, `CLAUDE_PROJECT_DIR` (BIN) — use `DROID_PROJECT_DIR` to detect Droid. |
| Hook history | 0.24.0 hooks (experimental, behind /settings); **0.25.0** added `UserPromptSubmit`, `Stop`, `SubagentStop`, `PreCompact`, `SessionStart`, `SessionEnd`; 0.27.4 hooks permanently enabled; 0.31.0 `--no-hooks`; 0.40.0 Stop decision/reason; **0.136.0 standardized `hooks.json` path** (+ auto-migration); 0.143.0 `suppressOutput`; 0.152.0/0.173.0 multiple hooks combined / run exactly once; 0.167.0 hung SessionStart no longer blocks init; **0.196.0 SessionStart context kept (was dropped)**; **0.202.0 "Lifecycle hooks now run again instead of being skipped"** (regression in some earlier builds); 0.211.0 hook command rewrites run as edited; 0.223.0 `preserveRiskLevel`. |
| Recommended floor | **≥ 0.202.0** (lifecycle hooks + SessionStart context reliable). |

**Is Droid Claude-Code-compatible?** Mostly, not verbatim. Same event names (`PreToolUse PostToolUse UserPromptSubmit Notification Stop SubagentStop PreCompact SessionStart SessionEnd`; no `PermissionRequest`/`PostToolUseFailure`), same group shape (`matcher` + `hooks[]`), snake_case stdin, `hookSpecificOutput.{additionalContext, permissionDecision, permissionDecisionReason, updatedInput}`, exit 2 = block. Differences: tool names (`Execute`, `Create`, `Edit`, `ApplyPatch`, `Read`, `LS`, `Glob`, `Grep`, `Task`, `FetchUrl`, `WebSearch`, `mcp__<server>__<tool>`), extra `commandRegex`, `permission_mode` ∈ `off|spec|auto-low|auto-medium|auto-high`, **exit 3 = abort**, **PreCompact is blockable**, no `updatedMCPToolOutput`/tool-output replacement, standalone `hooks.json` has **no** `"hooks"` wrapper, compaction starts a **new session id**.

## 2. Config paths, relocation, trust

- User: **`~/.factory/hooks.json`** (preferred). Project: `<project>/.factory/hooks.json`. Legacy `.factory/hooks/hooks.json` is read only when `hooks.json` is absent (and archived to `hooks/hooks.migrated.json` on next save). Org-managed hooks (Enterprise Controls) and plugin `hooks/hooks.json` are merged in.
- BIN `loadHooks()`: reads **both** `hooks.json` and the `hooks` key of the same folder's `settings.json`, merged shallowly per event: `{...settingsJson.hooks, ...hooksJson}` — an event key present in hooks.json **replaces** that event's array from settings.json. (Docs say settings.json is read only "if hooks.json is absent" — BIN is more permissive.) ⇒ Install into `hooks.json`, never into settings.json.
- Relocation (undocumented, BIN): **`FACTORY_HOME_OVERRIDE=<dir>`** replaces the home directory ⇒ Droid uses `<dir>/.factory/{settings.json,hooks.json,mcp.json,sessions/,logs/}`. (`FACTORY_HOME` — used by some third-party tools — is **not** read.) `--settings <file>` merges an extra settings file for one process (whether its `hooks` key is honored: unverified). `FACTORY_API_KEY=fk-…` for auth in tests.
- Disable switches that silence us: `hooksDisabled: true` (settings or top of hooks.json), `droid --no-hooks`, org `allowManagedHooksOnly: true`.
- Reload: Droid "snapshots hooks at startup and warns when hooks are modified externally" (docs) — restart after install.
- Trust: a FolderTrustService with `trustedFolders` exists (BIN); whether project `.factory/hooks.json` is trust-gated is unverified — use the user file.
- Sandbox: when Droid's sandbox is enabled in `per-command` mode, **hook commands are wrapped in the sandbox** (BIN `prepareHookCommand`) — subcortex's state/snapshot writes may be denied; must fail open.
- Hook process: spawned via the user's shell with cwd = project dir; stdin = `JSON.stringify(input)`; every top-level string/number/bool input field is also exported as an env var (e.g. `session_id`, `hook_event_name`, `prompt`, `source`, `trigger`).

**Isolated e2e recipe:** `FACTORY_HOME_OVERRIDE=$TMP/fh FACTORY_API_KEY=fk-… droid exec --auto low --cwd $TMP/repo "…"` with `$TMP/fh/.factory/hooks.json`. `/compact` for PreCompact/SessionStart(compact) needs an interactive session.

## 3. Registration — `~/.factory/hooks.json` (top-level event map, no wrapper)

```json
{
  "UserPromptSubmit": [
    { "hooks": [ { "type": "command", "command": "/Users/me/.local/bin/subcortex hook droid UserPromptSubmit", "timeout": 5 } ] }
  ],
  "PostToolUse": [
    { "matcher": "Execute", "hooks": [ { "type": "command", "command": "/Users/me/.local/bin/subcortex hook droid PostToolUse", "timeout": 10 } ] }
  ],
  "PreCompact": [
    { "hooks": [ { "type": "command", "command": "/Users/me/.local/bin/subcortex hook droid PreCompact", "timeout": 10 } ] }
  ],
  "SessionStart": [
    { "hooks": [ { "type": "command", "command": "/Users/me/.local/bin/subcortex hook droid SessionStart", "timeout": 10 } ] }
  ]
}
```

Schema (BIN zod): top-level keys `PreToolUse PostToolUse Notification UserPromptSubmit Stop SubagentStop PreCompact SessionStart SessionEnd hooksDisabled showHookOutput`; group `{matcher?: string, commandRegex?: string, hooks: [...]}`; entry `{type: "command" (REQUIRED literal), command: string, timeout?: number, preserveRiskLevel?: boolean}`. Unknown keys are **stripped** (lost when Droid rewrites the file via `/hooks`). A file that fails JSON parse or schema validation is **ignored entirely** ("Invalid hooks file, ignoring") ⇒ a bad write disables every user hook.
- `timeout` in **seconds**, default **60**; on expiry SIGTERM, then SIGKILL after 2 s; result forced to exit code 1 (non-blocking).
- `matcher`: empty/omitted/`*` = all; exact string or case-sensitive regex. Matched value: tool name for Pre/PostToolUse; `source` for SessionStart (`startup|resume|clear|compact`); `trigger` for PreCompact (`manual|auto`). Omitting the matcher on SessionStart/PreCompact and filtering in subcortex is the safest choice.
- `commandRegex`: extra filter on the `Execute` command string (invalid regex ⇒ skipped).
- Identical commands are deduplicated; all matching hooks run (in parallel).

**Tagging / uninstall:** identify ours by `command` (regex `(^|[\\/])subcortex(\.exe)?["']?\s+hook\s+droid\s+`). Uninstall: parse, remove matching entries from each group, drop groups whose `hooks` becomes empty and events whose array becomes empty, preserve everything else, atomic write + `.bak`. Always write `"type":"command"`.

## 4. Events

Common stdin: `session_id`, `transcript_path` (session JSONL `~/.factory/sessions/<encoded-cwd>/<session_id>.jsonl` per BIN `getSessionMessagesPath`; the `<encoded-cwd>` form in the samples is illustrative; records with `"type":"message"`), `cwd`, `permission_mode`, `hook_event_name`, optional `message_id`. Execute `tool_input` carries `command`, `riskLevelReason`, `riskLevel` (`low|medium|high`).

### 4.1 Prompt submit — `UserPromptSubmit`

```json
{"session_id":"3f6c2a9e-51b4-4c3e-8a07-9d2e6b1f4c80","transcript_path":"/Users/me/.factory/sessions/-Users-me-proj/3f6c2a9e-51b4-4c3e-8a07-9d2e6b1f4c80.jsonl","cwd":"/Users/me/proj","permission_mode":"auto-medium","hook_event_name":"UserPromptSubmit","message_id":"msg_01J9…","prompt":"rename getUser to fetchUser in src/api.ts","has_images":false}
```
(a) context injection, exit 0:
```json
{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"prefer the most direct, minimal path"}}
```
Plain non-JSON stdout on exit 0 is also used as context (BIN `DI()`), so **never print logs to stdout**. Only the **first** hook result that yields context is used for the prompt (BIN, exec path). Undocumented: `hookSpecificOutput.updatedInput.prompt` rewrites the prompt — don't send it.

### 4.2 After tool — `PostToolUse` (matcher `Execute`)

```json
{"session_id":"3f6c2a9e-…","transcript_path":"/Users/me/.factory/sessions/-Users-me-proj/3f6c2a9e-….jsonl","cwd":"/Users/me/proj","permission_mode":"auto-medium","hook_event_name":"PostToolUse","tool_name":"Execute","tool_input":{"command":"npm test 2>&1","riskLevelReason":"Runs the project's test suite without modifying files.","riskLevel":"low"},"tool_response":"> proj@1.0.0 test\n> vitest run\n … 4,812 lines …\n Test Files  212 passed (212)\n\n[Process exited with code 0]"}
```
`tool_response` for Execute is (per BIN) the tool's result value — a string ending in `[Process exited with code N]`; accept string or object (`output`/`stdout`/`stderr`/`text`). Fires on the success path.

(b) tool-output replacement: **IMPOSSIBLE.** BIN parses only `continue, stopReason, suppressOutput, systemMessage, decision, reason, hookSpecificOutput.{permissionDecision, permissionDecisionReason, additionalContext, updatedInput}`; the tool result is stored unchanged. Available instead: append-only `hookSpecificOutput.additionalContext` (added as a separate system message, visibility llm_only; first hook only) — which adds tokens. ⇒ For behavior 2 emit `{}`.
Optional alternative (not recommended by default, unverified): `PreToolUse` on `Execute` returning `hookSpecificOutput.updatedInput.command` to wrap the command before it runs (0.211.0: "Commands changed by hooks now run as edited"; 0.223.0 `preserveRiskLevel` keeps the original risk level). Docs pair `updatedInput` with `permissionDecision:"allow"`, which **bypasses the permission system** — whether `updatedInput` applies without it is unverified.

### 4.3 Pre-compaction — `PreCompact`

```json
{"session_id":"3f6c2a9e-…","transcript_path":"/Users/me/.factory/sessions/-Users-me-proj/3f6c2a9e-….jsonl","cwd":"/Users/me/proj","permission_mode":"auto-medium","hook_event_name":"PreCompact","trigger":"auto","custom_instructions":"","message_count":212,"estimated_tokens":171904,"message_id":"msg_01J9…"}
```
(c) Output is not used. **BIN 0.223.0: exit code 2 or 3 BLOCKS compaction** ("PreCompact hook blocked compaction") — docs say "N/A", ignore them. Exit 0 with `{}`. Snapshot from `transcript_path`.

### 4.4 Session start — `SessionStart`

Compaction creates a **new session** (`createInactiveSessionWithId({parentSessionId, source:"compact"})`, BIN), so SessionStart fires with `source:"compact"`, a **new** `session_id`, and `previous_session_id` = the pre-compaction session (key the snapshot by that).
```json
{"session_id":"a1d4e7f0-2c93-4b6e-9f15-7e0c3b8a2d41","transcript_path":"/Users/me/.factory/sessions/-Users-me-proj/a1d4e7f0-….jsonl","cwd":"/Users/me/proj","permission_mode":"auto-medium","hook_event_name":"SessionStart","source":"compact","previous_session_id":"3f6c2a9e-51b4-4c3e-8a07-9d2e6b1f4c80","message_id":"msg_01J9…","CLAUDE_ENV_FILE":"/Users/me/.factory/temp/env/droid-env-a1d4e7f0-…-k3j9.sh"}
```
Response (exit 0):
```json
{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"[subcortex] Context before compaction:\n…last 6 messages…"}}
```
All hooks' contexts are joined with `\n\n` and queued for the session (requires exit 0). Session start waits only a bounded budget; late results are applied afterwards. `CLAUDE_ENV_FILE` may receive `export KEY=value` lines. `source` ∈ `startup|resume|clear|compact` (+ `previous_session_id`, `calling_session_id` when applicable).

## 5. Exit codes and output parsing (BIN 0.223.0 + docs)

| Situation | Effect |
|---|---|
| exit 0, stdout starts with `{` | whole stdout `JSON.parse`d; parse failure ⇒ warning, treated as plain text |
| exit 0, plain text | UserPromptSubmit / SessionStart: **used as context**; other events: shown in transcript only |
| exit 0, empty | no effect |
| exit 1 / other non-zero / spawn error / cancelled | non-blocking; PostToolUse exit 1 with stderr ⇒ user-visible warning |
| **exit 2** | PreToolUse: blocks tool. PostToolUse: stderr fed to the model. UserPromptSubmit: **blocks prompt**. Stop/SubagentStop: prevents stopping. **PreCompact: blocks compaction.** Others: stderr to user |
| **exit 3** (undocumented) | PostToolUse: **aborts the agent** (`AgentAbortError`). UserPromptSubmit: blocks prompt. PreCompact: blocks compaction |
| timeout (default 60 s) | killed, exit forced to 1 ⇒ non-blocking |
| stderr | used as reason/feedback on exit 2/3; warning on exit 1 (PostToolUse) |

**Never emit:**
- exit codes `2` or `3`
- `"continue": false` (+`stopReason`): UserPromptSubmit blocked; **PostToolUse throws HookStopError and stops the turn**; generally "Droid stops processing"
- `"decision": "block"` (UserPromptSubmit blocks; Stop/SubagentStop refuses to stop; PostToolUse feedback)
- `hookSpecificOutput.permissionDecision` of `"deny"`/`"ask"` (and `"allow"` — bypasses permissions)
- `hookSpecificOutput.updatedInput` (rewrites tool input / the user's prompt)
- `systemMessage` (harmless warning, but user-visible noise); `suppressOutput: true` is fine
- plain-text stdout on UserPromptSubmit/SessionStart unless it is intended context

Adapter contract: always exit 0; one JSON object (`{}` no-op) on stdout; logs to stderr/file; internal budget < registered `timeout`.

## 6. MCP fallback (`subcortex mcp`)

`~/.factory/mcp.json` (user; `$FACTORY_HOME_OVERRIDE/.factory/mcp.json` when relocated) or `<project>/.factory/mcp.json`:
```json
{
  "mcpServers": {
    "subcortex": { "type": "stdio", "command": "/Users/me/.local/bin/subcortex", "args": ["mcp"], "env": {}, "disabled": false }
  }
}
```
Fields: `type` (`stdio` default | `http` | `sse`), `command`, `args`, `env`, `disabled`, `disabledTools`, `timeout` (ms per tool call), `connectTimeout` (ms; stdio default 30000). CLI: `droid mcp add subcortex "/Users/me/.local/bin/subcortex mcp"` (stdio default; writes the user config) / `droid mcp remove subcortex` / `droid mcp list`. Droid reloads when mcp.json changes. Project servers cannot be removed via CLI.

## 7. Unverified / caveats

- `droid --version` output format; `--settings <file>` honoring a `hooks` key.
- `FACTORY_HOME_OVERRIDE` is undocumented (found in BIN in several resolvers); could change without notice.
- Exact Execute `tool_response` shape (string per BIN) and whether PostToolUse fires for non-zero exits.
- SessionStart wait budget value not determined.
- Whether project `.factory/hooks.json` requires folder trust.
- Old builds (≤ ~0.26) auto-imported Claude Code hooks (changelog 0.26.3 "Claude Code Hooks Auto-Migration", translating `bash_tool`→`Execute`); no trace of it in 0.223.0, but if a user migrated earlier, `subcortex hook claude …` entries may exist in Droid config — the Droid installer should detect and remove/rewrite them, and the Claude adapter should no-op when `DROID_PROJECT_DIR` is set.
- The `PreToolUse` `updatedInput` command-wrap alternative is untested.
