# Cursor CLI (`agent` / `cursor-agent`) — subcortex hook adapter spec

Status: verified 2026-09-21 against the official docs **and** by static reading of the shipped CLI bundle
`2026.09.18-9a7762b` (darwin/arm64, `dist-package/index.js`, `190.index.js`, `1218.index.js`). "BUNDLE" below
means "read directly from that JS". Nothing was executed.

## 1. Sources, versions, detection

| Item | Value |
|---|---|
| Hooks reference | https://cursor.com/docs/hooks (markdown version: https://cursor.com/docs/hooks.md) |
| Third-party (Claude Code) hooks | https://cursor.com/docs/reference/third-party-hooks.md |
| CLI changelog | https://cursor.com/docs/cli/changelog.md |
| CLI config / params / MCP | https://cursor.com/docs/cli/reference/configuration.md, …/parameters.md, https://cursor.com/docs/cli/mcp.md, https://cursor.com/docs/mcp.md |
| Forum (CLI hook coverage) | https://forum.cursor.com/t/cursor-cli-doesnt-send-all-events-defined-in-hooks/148316 (staff posts Jan–Jun 2026) |
| Third-party probe | https://github.com/entireio/cli/blob/main/cmd/entire/cli/agent/cursor/AGENT.md (live probe of CLI 2026.02.13, 2026-03-02) |
| Latest build | **2026.09.18-9a7762b** (pinned in `https://cursor.com/install`, tarball `https://downloads.cursor.com/lab/2026.09.18-9a7762b/<os>/<arch>/agent-cli-package.tar.gz`). Latest changelog heading: "August 26, 2026 release". |
| Binaries | Installer symlinks both `~/.local/bin/agent` (primary) and `~/.local/bin/cursor-agent` (legacy) → `~/.local/share/cursor-agent/versions/<build>/cursor-agent`. |
| Version detection | `agent --version` → `2026.09.18-9a7762b`. Inside a hook: payload `cursor_version` and env `CURSOR_VERSION` carry the same build string (BUNDLE). IDE sends a semver-style app version instead, so a `^\d{4}\.\d{2}\.\d{2}-[0-9a-f]+$` value ⇒ CLI. |
| Min version with hooks | Jan 2026 builds: only `beforeShellExecution`/`afterShellExecution` fired (staff, 2026-01-08). Apr 2026: staff lists `beforeShellExecution, afterShellExecution, afterFileEdit, postToolUse, stop, sessionStart`; Claude-format responses accepted (changelog "April 2026"). **May 20, 2026: "Hooks accept payloads over stdin"** (earlier builds fed the payload through a shell heredoc — still stdin from the hook's view). |
| Recommended floor | **≥ 2026.05.20**. `beforeSubmitPrompt.additional_context` is confirmed only in 2026.09.18 (BUNDLE); gate that feature on an e2e check, not on a version number. |

## 2. Config paths, relocation, trust

Load order / merge (BUNDLE `$()` + loader): enterprise → team → **project `<ws>/.cursor/hooks.json`** → **user `~/.cursor/hooks.json`** → claude-project-local `<ws>/.claude/settings.local.json` → claude-project `<ws>/.claude/settings.json` → claude-user `~/.claude/settings.json` → plugin hooks. **All matching entries from all sources run in parallel** and their responses are merged.

- Enterprise: macOS `/Library/Application Support/Cursor/hooks.json`, Linux `/etc/cursor/hooks.json`, Windows `C:\ProgramData\Cursor\hooks.json`.
- User path is hard-coded `path.join(os.homedir(), ".cursor", "hooks.json")` (BUNDLE). **`CURSOR_CONFIG_DIR` / `XDG_CONFIG_HOME` only relocate `cli-config.json`, not hooks.json or mcp.json.**
- Working dir of the hook process: user hooks → `~/.cursor/`; project/claude/runtime hooks → workspace root. ⇒ always register an **absolute** subcortex path.
- Project hooks (`loadProjectHooks`) only in a trusted workspace. Headless runs in an untrusted workspace fail unless `--trust` (or `--force`/`--yolo`) is passed.
- Hook config is watched and hot-reloaded (docs); CLI changelog also says plugin hooks refresh on reload.
- Env passed to every hook (BUNDLE `buildHookEnvironment`): `CURSOR_PROJECT_DIR`, `CURSOR_VERSION`, `CURSOR_USER_EMAIL` (if logged in), `CURSOR_TRANSCRIPT_PATH` (if known), `CLAUDE_PROJECT_DIR` (alias), plus env returned by a `sessionStart` hook. Plugin hooks also get `CURSOR_PLUGIN_ROOT`/`CLAUDE_PLUGIN_ROOT`. Hooks are spawned through the user's shell with sandbox policy `insecure_none` (not sandboxed).

**Isolated e2e recipe:** `HOME=$TMP/home` (redirects `~/.cursor/hooks.json`, `~/.cursor/mcp.json`, `~/.cursor/projects/*` transcripts, and hides `~/.claude/settings.json`), `CURSOR_CONFIG_DIR=$TMP/home/.cursor` for cli-config, `CURSOR_API_KEY=<key>` for auth (macOS keychain login may not be reachable after HOME change — use the key), then `agent -p --trust --force --workspace $TMP/repo "…"`. Caveat: in `-p` mode `beforeSubmitPrompt` and `stop` do **not** fire (entire.io live probe, Feb 2026; BUNDLE shows the interactive path only) — test prompt-submit via an interactive session under tmux. `sessionStart`, `sessionEnd`, `preToolUse`, `postToolUse` do fire in `-p`.

## 3. Registration (`~/.cursor/hooks.json`)

```json
{
  "version": 1,
  "hooks": {
    "beforeSubmitPrompt": [
      { "command": "/Users/me/.local/bin/subcortex hook cursor beforeSubmitPrompt", "timeout": 5 }
    ],
    "postToolUse": [
      { "command": "/Users/me/.local/bin/subcortex hook cursor postToolUse", "matcher": "^Shell$", "timeout": 10 }
    ],
    "preCompact": [
      { "command": "/Users/me/.local/bin/subcortex hook cursor preCompact", "timeout": 10 }
    ],
    "sessionStart": [
      { "command": "/Users/me/.local/bin/subcortex hook cursor sessionStart", "timeout": 10 }
    ]
  }
}
```

Rules (BUNDLE validators `te`/`Z`):
- `version` must be a positive integer number (`1`). Missing/invalid ⇒ **whole file rejected**.
- Top-level `hooks` object; **any unknown event key ⇒ whole file rejected** ("Unknown hook type"). Valid keys: `beforeShellExecution beforeMCPExecution afterShellExecution afterMCPExecution beforeReadFile afterFileEdit beforeTabFileRead afterTabFileEdit stop beforeSubmitPrompt afterAgentResponse afterAgentThought sessionStart sessionEnd preCompact subagentStart subagentStop preToolUse postToolUse postToolUseFailure workspaceOpen`.
- Per entry: `command` (string, required unless `type:"prompt"`), `type` (`"command"`|`"prompt"`, default command), `timeout` (**seconds**, number > 0; default **60** = `yB`), `matcher` (string; must compile as JS RegExp or the file is rejected), `loop_limit`, `failClosed` (boolean). Unknown per-entry keys are not validated (tolerated) but don't rely on them.
- Matcher is **unanchored** `new RegExp(m).test(value)`; `""`/`"*"` = all. Value tested: `postToolUse` → `tool_name` (`Shell`, `Read`, `Write`, `Grep`, `Delete`, `Task`, `MCP:<tool>`…); `beforeSubmitPrompt` → `"UserPromptSubmit"`; `preCompact`, `sessionStart` → no matcher value (always run).
- File is parsed as JSONC with a naive `//.*$` strip ⇒ **never put `//` inside any string** (e.g. `http://`), or the whole file fails to parse.
- Never set `failClosed: true`.

**Tagging / uninstall:** identify ours purely by `command` (regex `(^|[\\/])subcortex(\.exe)?["']?\s+hook\s+cursor\s+`). Uninstall: read (JSONC-tolerant), drop matching entries, delete now-empty event arrays, keep `version` and all foreign entries/keys, write atomically (tmp + rename) with a `.bak`. If the file would become `{"version":1,"hooks":{}}` and it did not exist before install, delete it.

**Cross-tool hazard (important):** Cursor also runs Claude Code hooks from `~/.claude/settings.json` / `<ws>/.claude/settings*.json` (mapping `UserPromptSubmit→beforeSubmitPrompt`, `PostToolUse→postToolUse` with `Bash→Shell`, `PreCompact→preCompact`, `SessionStart→sessionStart`), with Claude nested `hookSpecificOutput` honored (`enableClaudeNestedHookSpecificOutputCompatibility: true` in the CLI, BUNDLE). Dedupe happens only when the command string is byte-identical. So once subcortex's Claude adapter **and** Cursor adapter are both installed, Cursor runs **both** ⇒ duplicate hints/snapshots. The Claude adapter must no-op when `CURSOR_VERSION` is set in env or the payload has `cursor_version`. Also: in the interactive CLI, `beforeSubmitPrompt`/`stop` are only dispatched if a **native** user or project hooks.json has an entry for that step (BUNDLE `1218.index.js`), so the native registration above is required anyway.

## 4. Events

Common stdin fields (BUNDLE executor adds): `conversation_id`, `generation_id`, `model`, optional `model_id`/`model_params`, `session_id` (= session_id ?? conversation_id), `hook_event_name`, `cursor_version`, `workspace_roots` (single entry: the CLI workspace), `user_email`, `transcript_path` (path if the transcript file exists, else `null`; CLI layout `~/.cursor/projects/<sanitized-ws>/agent-transcripts/<conversation_id>.jsonl`).

### 4.1 Prompt submit — `beforeSubmitPrompt`

stdin (interactive CLI):
```json
{"conversation_id":"5b0d6c1e-8a4f-4f1e-9d8e-2b7f1c3a9e10","generation_id":"f2a9c0d4-1b7e-4c55-a0e2-6d3b8e71c4aa","model":"composer-2.5","prompt":"rename getUser to fetchUser in src/api.ts","attachments":[],"session_id":"5b0d6c1e-8a4f-4f1e-9d8e-2b7f1c3a9e10","hook_event_name":"beforeSubmitPrompt","cursor_version":"2026.09.18-9a7762b","workspace_roots":["/Users/me/proj"],"user_email":"me@example.com","transcript_path":"/Users/me/.cursor/projects/Users-me-proj/agent-transcripts/5b0d6c1e-8a4f-4f1e-9d8e-2b7f1c3a9e10.jsonl"}
```
(a) context injection — **supported in 2026.09.18 but undocumented on cursor.com**: schema accepts `continue` (bool), `user_message` (string), `additional_context` (string); top-level camelCase `additionalContext` is accepted as an alias; Claude `hookSpecificOutput.additionalContext` also accepted. Emit:
```json
{"additional_context":"prefer the most direct, minimal path"}
```
Limit: after trim, **> 10,000 chars ⇒ the carrier is dropped entirely** (`HookAdditionalContextTooLargeError`), not truncated. Multiple hooks' contexts are joined with `\n\n---\n\n`. No-op: print `{}` (or nothing). Do **not** include `"continue"` at all (merge is AND; `false` blocks).

### 4.2 After tool — `postToolUse` (matcher `^Shell$`)

stdin (Shell success; BUNDLE `createSuccessOutput`):
```json
{"conversation_id":"5b0d6c1e-…","generation_id":"f2a9c0d4-…","model":"composer-2.5","tool_name":"Shell","tool_input":{"command":"npm test","cwd":"/Users/me/proj"},"tool_output":"{\"output\":\"> proj@1.0.0 test\\n> vitest run\\n … 4,812 lines …\\n Test Files  212 passed (212)\\n\",\"exitCode\":0}","duration":48213,"tool_use_id":"call_9Qx…\nctc_71b…","cwd":"/Users/me/proj","session_id":"5b0d6c1e-…","hook_event_name":"postToolUse","cursor_version":"2026.09.18-9a7762b","workspace_roots":["/Users/me/proj"],"user_email":"me@example.com","transcript_path":"/Users/me/.cursor/projects/Users-me-proj/agent-transcripts/5b0d6c1e-….jsonl"}
```
Notes: `tool_output` is a **JSON string** `{"output","exitCode"}`; `postToolUse` fires only for exit code 0 — a non-zero exit fires `postToolUseFailure` with `error_message` = output (or "Command failed with exit code N"). `tool_use_id` can contain a newline.

(b) tool-output replacement: **IMPOSSIBLE for Shell.** `postToolUse` response is reduced to `additional_context` only (appended after the tool result — adds tokens). `updated_mcp_tool_output` is honored **only for MCP tools** (string, or `{content:[{type:"text",text}]}`). `afterShellExecution`'s response is ignored entirely (BUNDLE: awaited, return value discarded). ⇒ Adapter for behavior 2 should emit `{}` (no-op) for Shell.
  Optional, NOT recommended by default: pre-execution wrapping via `preToolUse` `{"updated_input":{"command":"<wrapped>"}}` (the tokenjuice approach). Hazards: `preToolUse` is a *permission hook* — invalid JSON or a schema-invalid response **blocks the tool**; adding `"permission":"allow"` auto-approves commands the user would otherwise be asked about. If used: never output `permission`, always print exactly one valid JSON object, exit 0.

### 4.3 Pre-compaction — `preCompact`

Dispatched in the CLI through the backend "executeHook" exec request (BUNDLE `index.js` case `preCompact`); not live-verified (needs a long context).
```json
{"conversation_id":"5b0d6c1e-…","generation_id":"0c1d…","model":"composer-2.5","trigger":"auto","context_usage_percent":91,"context_tokens":182340,"context_window_size":200000,"message_count":146,"messages_to_compact":118,"is_first_compaction":true,"session_id":"5b0d6c1e-…","hook_event_name":"preCompact","cursor_version":"2026.09.18-9a7762b","workspace_roots":["/Users/me/proj"],"user_email":"me@example.com","transcript_path":"/Users/me/.cursor/projects/Users-me-proj/agent-transcripts/5b0d6c1e-….jsonl"}
```
(c) Observational only; cannot block or modify. Only accepted output field: `user_message` (string). Emit `{}`. Snapshot source: `transcript_path` JSONL, lines like `{"role":"user","message":{"content":[{"type":"text","text":"<user_query>\n…\n</user_query>"}]}}` / `{"role":"assistant",…}` (may be written asynchronously — tolerate partial last line). If `transcript_path` is null, derive the path from `workspace_roots[0]` + `conversation_id`.

### 4.4 Session start — `sessionStart`

Fires once when the CLI creates a **new** chat (not on `--resume`/`--continue`, **not after compaction**) (BUNDLE `1218.index.js`: `!resumeId && hasHooksForStep(sessionStart)`). Fire-and-forget for blocking, but `additional_context` and `env` are consumed.
```json
{"conversation_id":"5b0d6c1e-…","generation_id":"5b0d6c1e-…","model":"composer-2.5","is_background_agent":false,"composer_mode":"agent","session_id":"5b0d6c1e-…","hook_event_name":"sessionStart","cursor_version":"2026.09.18-9a7762b","workspace_roots":["/Users/me/proj"],"user_email":"me@example.com","transcript_path":null}
```
Response: `{"additional_context":"…"}` (≤ 10,000 chars) and optional `"env":{…}`.

**Post-compaction re-inject (behavior 4) has no native event in Cursor.** Emulation: `preCompact` writes the snapshot plus a pending marker keyed by `conversation_id`; the next `beforeSubmitPrompt` for that conversation returns the snapshot as `additional_context` (≤ 10,000 chars, one-shot) and clears the marker. (Using `postToolUse` instead would require an unfiltered matcher and may fire before the server finishes summarizing.)

## 5. Exit codes, output parsing, failure semantics (BUNDLE `executeCommandHook`)

| Situation | Effect |
|---|---|
| exit 0, stdout empty/whitespace | "empty_stdout" failure → **ignored** (no effect) unless `failClosed` |
| exit 0, JSON | stdout trimmed, `JSON.parse`; if that fails and text ends with `}`, the last `{…}` suffix that parses is used |
| exit 0, invalid JSON / schema-invalid (e.g. non-string `additional_context`) | permission hooks (`beforeShellExecution, beforeMCPExecution, beforeReadFile, beforeTabFileRead, subagentStart, preToolUse`) ⇒ **BLOCK**; all other steps (incl. our 4) ⇒ response dropped, fail-open |
| **exit 2** | "block": permission hooks ⇒ `permission:"deny"`; **`beforeSubmitPrompt` and `sessionStart` ⇒ `{continue:false}` (prompt is rejected)**; `stop` ⇒ `{}`; others (postToolUse, afterShellExecution, preCompact) ⇒ no response but logged as blocked. stderr becomes the block reason. |
| other non-zero / signal | failed ⇒ ignored (fail-open) unless `failClosed:true` |
| timeout (default 60 s, `timeout` seconds) | killed ⇒ failed ⇒ ignored unless `failClosed:true` |
| stderr | only used as the reason text on exit 2; otherwise logged |

Hooks for a step run in parallel; merge: `permission` deny>ask>allow; `continue` AND-ed; `user_message`/`agent_message` concatenated; `additional_context` joined `\n\n---\n\n`; other fields last-wins.

**Never emit (any of these blocks, denies, loops, or auto-approves):**
- exit code `2`; `failClosed: true` in registration
- `"continue": false` (beforeSubmitPrompt blocks submission; sessionStart schema accepts it)
- `"permission": "deny"` / `"ask"` (and avoid `"allow"` — it bypasses approval) on preToolUse/before*/subagentStart
- Claude-format `{"decision":"block","reason":…}` or `hookSpecificOutput.permissionDecision:"deny"` (Cursor maps these to deny / to an auto-submitted follow-up)
- `followup_message` (stop/subagentStop auto-submits a new user turn — loop)
- invalid JSON on any permission hook; `additional_context` > 10,000 chars (silently dropped, not a block)
- any `//` sequence inside hooks.json strings; unknown event keys in hooks.json (both kill every user hook)

Adapter contract: always exit 0; print exactly one JSON object (`{}` when nothing to do); logs to stderr or a file, never stdout; internal timeout well under the registered `timeout`.

## 6. MCP fallback (`subcortex mcp`)

`~/.cursor/mcp.json` (global; same `os.homedir()` rule) or `<ws>/.cursor/mcp.json` (project):
```json
{
  "mcpServers": {
    "subcortex": { "type": "stdio", "command": "/Users/me/.local/bin/subcortex", "args": ["mcp"], "env": {} }
  }
}
```
No `agent mcp add` command exists — write the file (merge `mcpServers`, remove only key `subcortex` on uninstall). Global servers load without approval in the CLI (changelog Apr 2026); project servers need `agent mcp enable subcortex` or `--approve-mcps`. Check with `agent mcp list` / `agent mcp list-tools subcortex`. `${env:NAME}`, `${userHome}`, `${workspaceFolder}` interpolation supported in `command/args/env`.

## 7. Unverified / caveats

- `beforeSubmitPrompt.additional_context` is **not in public docs**; confirmed only by reading build 2026.09.18. Needs a live interactive e2e check; unknown in which build it landed.
- `preCompact` in the local CLI verified only as a code path (server-requested exec); no live capture.
- `beforeSubmitPrompt`/`stop` not firing in `-p` mode is from a Feb 2026 third-party probe; may have changed.
- Whether the "Include Third-Party Plugins, Skills, and Other Configs" toggle can disable Claude-hook loading in the CLI was not determined (on by default per docs).
- `HOME` override behavior for auth/keychain on macOS during e2e is assumed; use `CURSOR_API_KEY`.
- The `preToolUse` `updated_input` wrap alternative was not live-tested; `updated_input` without `permission` is applied per BUNDLE logic.
- Docs say sessionStart is "fire-and-forget"; BUNDLE shows the CLI awaits it for `additional_context`/`env` — a slow hook delays nothing critical, but keep it fast.
