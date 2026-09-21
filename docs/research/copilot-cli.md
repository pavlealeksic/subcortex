# GitHub Copilot CLI (`copilot`) — subcortex hook adapter spec

Status: verified 2026-09-21 against the official GitHub docs (fetched via `docs.github.com/api/article/body`), the
official changelog (`github/copilot-cli/changelog.md`), the Copilot SDK types (`github/copilot-sdk`), GitHub issues, and
strings in the shipped 1.0.87 darwin-arm64 binary (core runtime is native Rust in `runtime.node`, so only string-level
evidence — marked BIN). Nothing was executed.

## 1. Sources, versions, detection

| Item | Value |
|---|---|
| Hooks reference | https://docs.github.com/en/copilot/reference/hooks-reference |
| How-to | https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/use-hooks |
| Config dir reference | https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference |
| Command reference (env vars, MCP) | https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference |
| MCP how-to | https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers |
| Context mgmt (compaction, large output) | https://docs.github.com/en/copilot/concepts/agents/copilot-cli/context-management |
| Changelog | https://github.com/github/copilot-cli/blob/main/changelog.md |
| SDK hook types | https://github.com/github/copilot-sdk/blob/main/nodejs/src/types.ts |
| Relevant issues | github/copilot-cli #2652 (additionalContext dropped; closed as fixed in 1.0.85), #3727 (userPromptSubmitted context regression, open), #1139, #1138 |
| Latest version | **1.0.87 (2026-09-21)** — npm `@github/copilot@latest` = 1.0.87; changelog top entry. |
| Version detection | `copilot --version` → `GitHub Copilot CLI 1.0.87.` (note trailing period; prerelease builds look like `1.0.81-5`). The CLI self-updates its runtime, so the reported version can differ from the npm package version (issue #3727). Hook payloads carry **no** version. |
| Min version with hooks | repo `.github/hooks` + `preToolUse` ~0.0.396 (2026-01-27); user-level `~/.copilot/hooks/` **0.0.422** (2026-03-05); `preCompact` **1.0.5**; hooks inside settings.json 1.0.8; `sessionStart.additionalContext` injected **1.0.11**; PascalCase/Claude nested format 1.0.6; `postToolUse.additionalContext` 1.0.49/1.0.51; **`userPromptSubmitted.additionalContext` → model 1.0.65**; **timeouts fail-open 1.0.67**; preToolUse exit 2 = deny 1.0.70; malformed item dropped instead of whole file 1.0.71; extension-hook `additionalContext` fixes 1.0.85. `modifiedResult` from command hooks: documented, used live by tokenjuice on 1.0.35 (no changelog entry). |
| Recommended floor | **≥ 1.0.67** (fail-open timeouts); features 1 & 4 need ≥ 1.0.65. |

## 2. Config paths, relocation, trust

Sources, all merged and all run (docs): policy `/etc/github-copilot/policy.d/*.json` (root-owned) → repo `.github/hooks/*.json` → **user `$COPILOT_HOME/hooks/*.json` (default `~/.copilot/hooks/`)** → inline `hooks` in `.github/copilot/settings.json`, `.github/copilot/settings.local.json`, and cross-tool **repo** `.claude/settings.json` / `.claude/settings.local.json` → inline `hooks` in `~/.copilot/settings.json` → plugin `hooks.json`.

- Relocation: **`COPILOT_HOME=<dir>` replaces the whole `~/.copilot`** (settings.json, hooks/, mcp-config.json, logs/, session-state/, fallback token file). `--config-dir` is deprecated (still works). Cache: `COPILOT_CACHE_HOME`. Auth for tests: `COPILOT_GITHUB_TOKEN` (> `GH_TOKEN` > `GITHUB_TOKEN`).
- Hook config is read **at CLI start** — restart after install.
- Trust: repo-level hooks and workspace MCP load only after folder trust. In `-p` mode repo hooks load if the folder is already trusted, or `GITHUB_COPILOT_PROMPT_MODE_REPO_HOOKS=true`, or `COPILOT_ALLOW_ALL` is set. User-level hooks are not trust-gated (only repo sources are described as gated).
- Kill switches that will silence us: `"disableAllHooks": true` in repo settings.json (CLI-wide for that repo) or in our own file; policy `allowManagedHooksOnly`.
- Hook env: `COPILOT_CLI=1` is set for CLI subprocesses (changelog 0.0.421, stated for git hooks); since 1.0.81 command hooks also get OTel trace env vars and inputs gain `traceparent`/`tracestate`.

**Isolated e2e recipe:** `COPILOT_HOME=$TMP/copilot COPILOT_GITHUB_TOKEN=… copilot -p "…" --allow-all-tools` from `$TMP/repo` (user hooks under `$TMP/copilot/hooks/` load without trust; `userPromptSubmitted`/`sessionStart` fire in `-p`; `sessionEnd` fires once per completed turn in `-p`/piped mode since 1.0.78). For `/compact`-driven `preCompact` tests use an interactive session (tmux) and type `/compact`.

## 3. Registration — own file `$COPILOT_HOME/hooks/subcortex.json`

```json
{
  "version": 1,
  "hooks": {
    "userPromptSubmitted": [
      { "type": "command", "bash": "/Users/me/.local/bin/subcortex hook copilot userPromptSubmitted", "powershell": "& 'C:\\Users\\me\\bin\\subcortex.exe' hook copilot userPromptSubmitted", "timeoutSec": 5 }
    ],
    "postToolUse": [
      { "type": "command", "matcher": "bash|powershell", "bash": "/Users/me/.local/bin/subcortex hook copilot postToolUse", "powershell": "& 'C:\\Users\\me\\bin\\subcortex.exe' hook copilot postToolUse", "timeoutSec": 10 }
    ],
    "preCompact": [
      { "type": "command", "bash": "/Users/me/.local/bin/subcortex hook copilot preCompact", "powershell": "& 'C:\\Users\\me\\bin\\subcortex.exe' hook copilot preCompact", "timeoutSec": 10 }
    ],
    "sessionStart": [
      { "type": "command", "bash": "/Users/me/.local/bin/subcortex hook copilot sessionStart", "powershell": "& 'C:\\Users\\me\\bin\\subcortex.exe' hook copilot sessionStart", "timeoutSec": 10 }
    ]
  }
}
```

- `version: 1` (optional since 1.0.5, but required by cloud agent — keep it).
- Command fields: `bash` / `powershell` / cross-platform `command` (1.0.2+), or CLI-only `exec` + `args` (no shell; don't combine with the others). `cwd` (relative to repo root or absolute), `env` (supports `$VAR` expansion).
- **`timeoutSec` in seconds, default 30**; `timeout` is an alias used only if `timeoutSec` is absent.
- `matcher`: regex compiled `^(?:PATTERN)$` against the full `toolName` (postToolUse, preToolUse, permissionRequest), `trigger` (preCompact), `agentName` (subagentStart), `notification_type`; **invalid regex ⇒ that entry is skipped**. Honored for postToolUse since 1.0.63. Tool names: `bash` (Unix), `powershell` (Windows), `view`, `create`, `edit`, `grep`, `glob`, `task`, `web_fetch`, `ask_user`, MCP tools. (Omitting the matcher and filtering in subcortex is equally valid.)
- Use **camelCase event names** ⇒ camelCase payloads below. (PascalCase names `UserPromptSubmit`, `PostToolUse`, … switch to VS Code/Claude snake_case payloads and Claude matcher semantics, e.g. `Bash`.)
- A malformed item in a directory hook file drops only that item (1.0.71+); invalid JSON / bad `version` / non-array event list rejects the whole file. Inline `hooks` in settings.json is strict (one bad item rejects the whole `hooks` field) — don't install there.

**Tagging / uninstall:** the file `$COPILOT_HOME/hooks/subcortex.json` is entirely ours; uninstall = delete it. Never edit `settings.json`.

**Cross-tool hazard:** Copilot also executes Claude-format hooks from the **repo's** `.claude/settings.json`/`.claude/settings.local.json` (not `~/.claude`). If subcortex's Claude adapter is installed at project scope, it will run under Copilot with PascalCase/snake_case payloads (`hook_event_name`, `session_id`, `tool_result.text_result_for_llm`, `tool_name:"Bash"`). The Claude adapter must detect Copilot (e.g. `COPILOT_CLI=1` env — unverified for hook processes — or `timestamp` being an ISO string alongside `session_id` and no `transcript_path` on UserPromptSubmit) and no-op.

## 4. Events (camelCase payloads)

Common fields: `sessionId`, `timestamp` (epoch ms), `cwd` (+ `traceparent`/`tracestate` since 1.0.81).

### 4.1 Prompt submit — `userPromptSubmitted`

```json
{"sessionId":"8c5d7b2e-3f41-4d0a-9c6b-1e2f3a4b5c6d","timestamp":1790019371523,"cwd":"/Users/me/proj","prompt":"rename getUser to fetchUser in src/api.ts"}
```
(a) context injection: `{"additionalContext":"prefer the most direct, minimal path"}` — injected into the model-facing prompt since **1.0.65** (changelog; SDK type `UserPromptSubmittedHookOutput{modifiedPrompt?, additionalContext?, suppressOutput?}`). **Conflict:** the reference page still says "Command and HTTP config-file userPromptSubmitted hooks have their output dropped". Treat as unverified until e2e passes. Robust fallback on the same CLI: register **`userPromptTransformed`** (documented as honored: "can rewrite the model-facing content"; input adds `transformedPrompt`) and return `{"modifiedTransformedPrompt":"<transformedPrompt>\n\n<hint>"}` — mutation-only, cannot block; replaces what the model sees and what is persisted (replayed on resume); empty string is rejected. Use one mechanism, not both.

### 4.2 After tool — `postToolUse` (fires only after **successful** tool calls since 1.0.15)

```json
{"sessionId":"8c5d7b2e-…","timestamp":1790019412087,"cwd":"/Users/me/proj","toolName":"bash","toolArgs":{"command":"npm test 2>&1","description":"Run the test suite"},"toolResult":{"resultType":"success","textResultForLlm":"> proj@1.0.0 test\n> vitest run\n … 4,812 lines …\n Test Files  212 passed (212)\n"}}
```
`toolArgs` may arrive as an object or as a JSON string (docs' debug example uses a string) — accept both.

(b) **Replacement IS supported** (docs: "modifiedResult is honored by both SDK programmatic hooks and command/HTTP config-file postToolUse hooks"):
```json
{"modifiedResult":{"resultType":"success","textResultForLlm":"> proj@1.0.0 test\n> vitest run\n…\n[subcortex: 4,640 lines elided; full output in ~/.subcortex/spill/8c5d7b2e-…/0007.txt]\n…\n Test Files  212 passed (212)\n"}}
```
- `resultType` **must stay `"success"`** — `"failure"` routes the call to the failure path and fires `postToolUseFailure`. Copy the other original `toolResult` fields through (spread the object, overwrite `textResultForLlm`).
- `additionalContext` (string) instead **appends** after the tool output (multiple hooks joined with `\n\n`, capped at 10 KB).
- `{}` or empty stdout keeps the original result.
- Copilot already spills any tool output > 20 KiB (`COPILOT_LARGE_OUTPUT_THRESHOLD_BYTES`, default 20480) to a temp file and gives the model a preview (`"Output too large to read at once (…). Saved to: … Preview (first …"` — BIN). If `textResultForLlm` already contains that marker, pass through; whether postToolUse sees pre- or post-spill text is unverified.

### 4.3 Pre-compaction — `preCompact` (matcher optional: `manual|auto`)

```json
{"sessionId":"8c5d7b2e-…","timestamp":1790020125511,"cwd":"/Users/me/proj","transcriptPath":"/Users/me/.copilot/session-state/8c5d7b2e-…/events.jsonl","trigger":"auto","customInstructions":""}
```
(c) Notification only ("cannot block it" — BIN settings description); output ignored. Emit `{}`. Snapshot source: `transcriptPath` JSONL of session events; take the last N `user.message` / `assistant.message` events (other types include `tool.execution_complete`, `session.compaction_start`, `session.compaction_complete`). Auto-compaction starts in the background at ~80 % context and tool calls keep running; manual `/compact` also fires it.

### 4.4 Session start — `sessionStart`

```json
{"sessionId":"8c5d7b2e-…","timestamp":1790019360004,"cwd":"/Users/me/proj","source":"startup","initialPrompt":"fix the flaky test"}
```
`source` ∈ `startup | resume | new` — **there is no `compact` source; compaction replaces history in place (plus a checkpoint) and does not start a session.** Response: `{"additionalContext":"…"}` (injected since 1.0.11). Fires once per session in interactive mode (1.0.22+).

**Post-compaction re-inject (behavior 4) — emulate:** `preCompact` writes snapshot + pending marker keyed by `sessionId`; on the next `userPromptSubmitted` for that session, if `transcriptPath` shows a `session.compaction_complete` event newer than the snapshot, return the snapshot as `additionalContext` (or via `userPromptTransformed`) once, then clear the marker. Avoid injecting from `postToolUse` while background compaction is running (it can be summarized away). `sessionStart` with `source:"resume"` may also consume a pending marker.

## 5. Exit codes and output parsing (docs)

| Situation | Effect |
|---|---|
| exit 0 | stdout scanned line-by-line; single-line `{"type":"progress",…}` objects are removed; the rest is concatenated, trimmed and parsed as **one** JSON document |
| empty stdout / leftover not valid JSON / two JSON objects | treated as **no output** → default behavior (fail-open) |
| **exit 2** | default: *warning* — stderr shown to the user, run continues. `preToolUse`/`permissionRequest`: **deny** (even if stdout says allow). `postToolUseFailure`: stdout appended as additionalContext. For our 4 events: no block, but user-visible noise |
| other non-zero | logged as hook failure, continues (fail-open). Exception: `preToolUse` ⇒ fail-closed deny |
| timeout (`timeoutSec`, default 30 s) | killed, warning, **fail-open for every event** (1.0.67+) |
| stderr | surfaced on exit 2; otherwise logged |
| output size | bounded at 10 MiB per invocation (truncated) |
| non-string `modifiedPrompt`/`modifiedTransformedPrompt`/`responseContent` | ignored with a warning (1.0.76+); `null` additionalContext = absent |

**Never emit:**
- exit code `2` (and never register `preToolUse`/`permissionRequest`, where any non-zero exit denies)
- `permissionDecision: "deny"` / `"ask"` (preToolUse); `behavior: "deny"` or `interrupt: true` (permissionRequest — interrupt stops the agent)
- `decision: "block"` on `agentStop`/`subagentStop` (forces another turn; up to 8)
- `modifiedResult` with `resultType` ≠ `"success"`
- `modifiedPrompt` (rewrites the user's prompt), `handled`/`responseContent` on userPromptSubmitted (answers without calling the model — changelog 1.0.44), empty-string `modifiedTransformedPrompt`
- `additionalContext` from a `notification` hook (injected as a user message; can wake an idle agent)
- Claude-style `continue:false` / `hookSpecificOutput.permissionDecision` (BIN shows Claude fields are parsed; semantics under Copilot undocumented — don't send them)
- more than one JSON object on stdout; progress lines are fine but unnecessary

Adapter contract: exit 0 always; exactly one JSON object (`{}` for no-op); logs to stderr/file only; internal budget well under `timeoutSec`.

## 6. MCP fallback (`subcortex mcp`)

`$COPILOT_HOME/mcp-config.json` (default `~/.copilot/mcp-config.json`):
```json
{
  "mcpServers": {
    "subcortex": { "type": "local", "command": "/Users/me/.local/bin/subcortex", "args": ["mcp"], "env": {}, "tools": ["*"] }
  }
}
```
Local server fields: `command` (req), `args` (req), `tools` (req; `["*"]`), `env` (supports `$VAR`, `${VAR}`, `${VAR:-default}`; env values referencing host vars need the `$` form since 0.0.340), `cwd`, `timeout` (ms, default 30000), `type` `"local"`/`"stdio"`. CLI equivalents: `copilot mcp add subcortex -- /Users/me/.local/bin/subcortex mcp` / `copilot mcp remove subcortex` / `copilot mcp get subcortex --json`. Per-session (tests): `copilot --additional-mcp-config @/tmp/mcp.json`. Project `.mcp.json` / `.github/mcp.json` also work but need folder trust (in `-p`: `GITHUB_COPILOT_PROMPT_MODE_WORKSPACE_MCP=true`). The built-in GitHub MCP server is always present.

## 7. Unverified / caveats

- `userPromptSubmitted` → `additionalContext` for **command** hooks: changelog 1.0.65 says yes, the reference page says command-hook output is dropped, issue #3727 reports the model only sometimes acting on it. Needs e2e; fallback `userPromptTransformed` (no changelog entry found for when it was added).
- `modifiedResult` support version floor unknown (evidence: docs + tokenjuice on 1.0.35).
- Whether `postToolUse` sees full output or the > 20 KiB spill preview.
- Exact `textResultForLlm` framing of bash results (exit-code suffix etc.) not verified.
- `COPILOT_CLI=1` presence in hook processes; `copilot --no-auto-update --version` flag (seen in an issue only).
- Sandbox (experimental `/sandbox`) may wrap hook processes; snapshot/spill writes outside allowed paths could fail — must fail open.
- No post-compaction event exists; the emulation in §4.4 is a design, not a platform feature.
