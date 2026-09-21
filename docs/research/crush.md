# Crush (charmbracelet/crush) — subcortex adapter spec

Target: **Crush v0.96.1** (released 2026-09-21T16:22Z). Every claim below was checked against the source at tag `v0.96.1` (shallow clone), unless it is marked **[UNVERIFIED]**.

**Short version.** v0.96.1 has **one** hook event: `PreToolUse`. It has no prompt-submit, post-tool, pre-compact or session-start hooks. So:
- Behavior 1 (prompt hint): **no native path.** The degraded path is PreToolUse `context`, which gets appended to the *result* of a tool call. The other options are static prompt text (an MCP `instructions` block or a global context file).
- Behavior 2 (replace tool output): **impossible after the fact.** Crush already truncates bash output natively (head/tail at 30k chars). An optional pre-exec rewrite through `updated_input` is possible; see §4.
- Behavior 3/4 (compaction snapshot and re-inject): **no hook exists.** The only route is out-of-band: read Crush's SQLite DB and re-inject through PreToolUse `context` (§4c).

---

## 1. Sources, versions, detection

Sources:
- https://github.com/charmbracelet/crush/blob/v0.96.1/docs/hooks/README.md (normative doc; same as the fetched copy)
- https://github.com/charmbracelet/crush/blob/v0.96.1/docs/hooks/FUTURE.md (planned, not implemented: `UserPromptSubmit`, `context_files`, sub-agent opt-in)
- https://github.com/charmbracelet/crush/blob/v0.96.1/docs/config/README.md (crushrc; JSON is now "legacy")
- Source files: `internal/hooks/{hooks.go,input.go,runner.go}`, `internal/agent/hooked_tool.go`, `internal/agent/coordinator.go` (runner is built only for `hooks.EventPreToolUse`), `internal/config/{config.go,load.go}`, `internal/agent/agent.go` (auto-summarize), `internal/agent/tools/bash.go` (output truncation), `schema.json`
- Release notes: v0.63.0 "Baby's First Hook" (https://github.com/charmbracelet/crush/releases/tag/v0.63.0)

Versions:
| Item | Value |
|---|---|
| Latest | v0.96.1 (2026-09-21). Hook events: `PreToolUse` only (`const EventPreToolUse = "PreToolUse"` is the only event constant) |
| Min version with hooks | **v0.63.0** (2026-04-27, PR #2598) |
| v0.64.0 | Fixes a **matcher bug**: on v0.63.0 hooks ran for *every* tool and ignored `matcher`. Treat **v0.64.0 as the practical minimum**, or have the handler tolerate any `tool_name` |
| v0.67.0 | Hooks run through the embedded POSIX shell (`mvdan.cc/sh`) instead of `sh -c`. Shebang scripts go through `os/exec` |
| v0.77.0 | `name` field added. On older versions it is an unknown JSON field; Go's `json.Unmarshal` ignores it, so it is harmless |
| v0.80.0 | Claude-style `hookSpecificOutput.additionalContext` maps onto `context` |
| v0.88.0 | crushrc `hook add/remove` builtins |

Version detection: `crush --version` (also `-v`, through charm `fang`). Non-TTY output is `crush version 0.96.1`. Goreleaser injects `{{.Version}}` with no leading `v`. `go install` builds and AUR builds show `v0.96.1`. Local builds show `devel`. On a TTY a colored logo line comes first. Parse with `/v?(\d+)\.(\d+)\.(\d+)/`. Crush exposes `$CRUSH_VERSION` to crushrc scripts, but not to hooks.

## 2. Config paths, format, env relocation, trust

Load order (low → high priority; later wins for scalars). From `lookupConfigs()` + `Load()`:
1. `/etc/crush/crush.json` (unix only)
2. **Global user JSON:** `$CRUSH_GLOBAL_CONFIG/crush.json`, else `${XDG_CONFIG_HOME:-~/.config}/crush/crush.json`. This is `~/.config/crush/crush.json` on macOS too, not `~/Library`.
3. Global crushrc: the sibling `crushrc` in the same dir
4. Global *data* JSON (machine-owned state; do **not** write hooks here): `$CRUSH_GLOBAL_DATA/crush.json`, else `$XDG_DATA_HOME/crush/crush.json`, else `~/.local/share/crush/crush.json` (Windows: `%LOCALAPPDATA%\crush\crush.json`)
5. Project files, found walking up from cwd to the git worktree root (bounded). Priority within a dir: `.crushrc` > `crushrc` > `.crush.json` > `crush.json`. Nearer dirs win.
6. Workspace data JSON `<DataDirectory>/crush.json`, default `<project>/.crush/crush.json` (highest). This is Crush-written state.

Merge semantics: `github.com/qjebbs/go-jsons` `Merge`. Objects merge recursively, scalars are overwritten by later files, and **arrays are concatenated**. `hooks.PreToolUse` entries from global and project files therefore **accumulate**, they don't override. At runtime, identical `command` strings are **deduplicated**, so each runs once.

Format: **strict JSON.** `json.Valid()` is enforced, so comments or a trailing comma in any file make Crush **fail to start** ("invalid JSON in config file"). The docs show jsonc only as illustration. `$schema: "https://charm.land/crush.json"`. JSON is labeled "legacy / deprecated, supported for the foreseeable future". The new primary format is `crushrc` (Bash run by the embedded interpreter). Both produce the same `hooks` map.

Hook-config validation is fatal. `ValidateHooks()` runs at load and on reload. Any hook with an empty `command`, or a `matcher` that doesn't compile as a Go RE2 regex, fails with "invalid hook configuration", and **Crush will not start**. The installer must write a non-empty command and a valid RE2 matcher. Event keys are normalized only for `pretooluse` variants (`pre_tool_use`, `PRETOOLUSE`, …). **Unknown event keys** (for example `PostToolUse`) are still validated but kept inert: they never fire.

Env vars for isolated e2e tests:
| Var / flag | Effect |
|---|---|
| `CRUSH_GLOBAL_CONFIG=<dir>` | global `crush.json` and `crushrc` dir |
| `CRUSH_GLOBAL_DATA=<dir>` | global data `crush.json` and provider catalog cache |
| `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME` | fallbacks for the above |
| `CRUSH_CACHE_DIR`, `CRUSH_SKILLS_DIR` | cache and skills dirs |
| `-D/--data-dir <dir>` | project data dir (default `.crush`, holds `crush.db`, `crush.lock`, workspace `crush.json`, logs) |
| `-c/--cwd <dir>` | working dir |
| `CRUSH_DISABLE_PROVIDER_AUTO_UPDATE=1` | no catwalk network fetch |
| `CRUSH_DISABLE_METRICS=1` | no telemetry |
| `CRUSH_SKIP_DATADIR_LOCK` | skip datadir flock (read in source; semantics **[UNVERIFIED]**) |
| `CRUSH_CLIENT_SERVER=1` | opt-in client/server mode. Default is in-process |

Headless: `crush run [-q] [-m provider/model] "prompt"` uses the same coordinator, so hooks fire. `-y/--yolo` exists on the root cmd.

Trust/enable: **no trust prompt and no enable flag.** Project `crush.json` and `crushrc` are "trusted code" and run unconditionally. Hooks are read when the agent is built (at startup and on model switch). No file watcher exists, so **restart Crush after install/uninstall**. Hooks fire only for **top-level agent** tool calls. Sub-agents (`agent`, `agentic_fetch`) are not intercepted, although the outer sub-agent tool call is.

## 3. Registration snippet and tagging

Recommended: global JSON `~/.config/crush/crush.json` (or `$CRUSH_GLOBAL_CONFIG/crush.json`). Add or merge only the `hooks.PreToolUse` key:

```json
{
  "$schema": "https://charm.land/crush.json",
  "hooks": {
    "PreToolUse": [
      {
        "name": "subcortex",
        "matcher": "^bash$",
        "command": "/Users/me/.local/bin/subcortex hook crush pre-tool-use || true",
        "timeout": 10
      }
    ]
  }
}
```

- `matcher`: Go RE2 regex, **unanchored** `MatchString` against the tool name. Anchor it (`^bash$`). Omit it or use `""` to match all tools. **Never** use `"*"`: it is an invalid regex and makes Crush **refuse to start**.
- `timeout`: **integer seconds**. `<=0` or omitted means 30.
- `command`: parsed by the embedded POSIX shell with cwd = project working dir. A relative path resolves against cwd, so a **global config must use an absolute path**. `|| true` is deliberate belt-and-braces: it turns any accidental exit 2 or 49 into 0 (see §5). stdout is still parsed. Python `argparse` usage errors and `python: can't open file` both exit **2**, and 2 blocks the tool call.
- Tool names (v0.96.1): `bash, edit, multiedit, write, view, ls, glob, grep, fetch, web_fetch, web_search, download, sourcegraph, agent, agentic_fetch, job_output, job_kill, todos, question, crush_info, crush_logs, list_mcp_resources, read_mcp_resource, lsp_*` and MCP tools `mcp_<server>_<tool>`.

Equivalent crushrc form (for users on crushrc; append as a marked block):
```bash
# >>> subcortex >>>
hook add PreToolUse --name subcortex --matcher '^bash$' --timeout 10 \
  --command '/Users/me/.local/bin/subcortex hook crush pre-tool-use || true'
# <<< subcortex <<<
```

Tagging and uninstall:
- **Tag:** `"name": "subcortex"`, or a `"subcortex:"` prefix. The name is shown in the TUI "Hook" line on every matched call, truncated to 30 cols, so keep it short. Also check that `command` contains `subcortex hook crush `.
- **No custom fields.** `schema.json` sets `HookConfig.additionalProperties: false`. The runtime ignores unknown keys, but editors and schema validation flag them.
- **Uninstall:** strict-parse the file. For **every** key under `hooks` (users may have written `pre_tool_use`), drop array items where `name == "subcortex"` or `name` starts with `"subcortex:"` or `command` matches `/\bsubcortex\b.*\bhook crush\b/`. Delete the event key if it is now empty, and `hooks` if that is empty. Preserve everything else byte-for-byte where possible. For a crushrc, delete the marker block. `hook remove PreToolUse --name subcortex` also works inside crushrc, but **without `--name` it clears every PreToolUse hook defined in that crushrc**.
- **Install:** idempotent. Run the uninstall step first, then append. If the file does not parse, abort and don't write; Crush couldn't load it anyway.

## 4. Events → subcortex behaviors

| subcortex behavior | Crush v0.96.1 event | Status |
|---|---|---|
| prompt-submit hint | *(none)* | Not possible. `UserPromptSubmit` exists only in FUTURE.md and draft PR #3825 (unmerged) |
| after-tool replace | *(none)* | Not possible. `PostToolUse` exists only in open PR #3146 (unmerged) |
| pre-compact snapshot | *(none)* | Not possible. Auto-summarize has no hook |
| session-start re-inject | *(none)* | Not possible |
| (only event) | `PreToolUse` | fires before every top-level tool call, before permission checks |

### PreToolUse stdin (exact struct `hooks.Payload`; field order as marshalled)
```json
{"event":"PreToolUse","session_id":"3f6c2a4e-8b1d-4c57-9e0a-2d7b5f1c9a83","cwd":"/Users/me/src/app","tool_name":"bash","tool_input":{"description":"Run Go tests","command":"go test ./... 2>&1","working_dir":""}}
```
`tool_input` is the raw JSON the model sent. The bash schema is `description, command, working_dir?, run_in_background?, auto_background_after?`, and file tools use `file_path`. Invalid JSON is replaced by `{}`. `session_id` is a UUID. There is **no** turn id, transcript path, prompt, or `hook_event_name`: the key is `event`, not Claude's `hook_event_name`.

Env passed to the hook: all of `os.Environ()`, plus `CRUSH=1 AGENT=crush AI_AGENT=crush CRUSH_EVENT CRUSH_TOOL_NAME CRUSH_SESSION_ID CRUSH_CWD CRUSH_PROJECT_DIR` (in v0.96.1 `CRUSH_PROJECT_DIR` == `CRUSH_CWD` == working dir), plus `CRUSH_TOOL_INPUT_COMMAND` if `tool_input.command` exists and `CRUSH_TOOL_INPUT_FILE_PATH` if `tool_input.file_path` exists.

### Response envelope (stdout, exit 0), as parsed by `parseStdout`
Crush format: `{"version":1,"decision":"allow"|"deny"|null,"halt":bool,"reason":str,"context":str|[str],"updated_input":{…}|"<json string>"}`.
Claude-compat: if a top-level key `hookSpecificOutput` exists, **only** it is parsed and all top-level fields are ignored. Parsed keys are `permissionDecision` (allow/deny; `ask` → none), `permissionDecisionReason`, `updatedInput`, `additionalContext`.

**(a) Context injection.** Emit `{"context":"<hint>"}`. Crush runs the tool, then does `resp.Content += "\n" + context`. The model sees the hint **appended after the tool's result**, on the next step; it is not a pre-turn user or system message. It is also visible in the TUI tool output. With `decision` omitted, the normal permission flow is unchanged. Degraded use for behavior 1: emit the hint once per turn on the first tool call. There is no turn id, so dedupe by `session_id` plus time or a daemon-side turn heuristic. The hint arrives after the model has already chosen its first action.
Static alternatives:
- The MCP server's `InitializeResult.instructions` is appended to the system prompt on **every** run (`<mcp-instructions>…</mcp-instructions>`), but it is fixed per connection.
- A global context file (`~/.config/crush/CRUSH.md`, `~/.config/AGENTS.md`, or `options.context_paths`) is read when the system prompt is built (process start / model switch), so it is not dynamic.

**(b) Tool-output replacement: impossible in v0.96.1.** No hook sees tool output. Emitting anything is safe; it just can't shrink the output. Notes:
- Native: `bash` stdout and stderr are each capped at `MaxOutputLength = 30000` chars. Crush keeps the head 15k + `... [N lines truncated, full output: <spill path>] ...` + the tail 15k, and writes the full output to a spill file. subcortex only adds value for outputs under 30k chars, or with smarter disposal rules.
- Optional pre-exec approximation, **off by default**: return `{"updated_input":{"command":"<wrapped>"}}` for `tool_name=="bash"` with `run_in_background` not true. The merge is shallow, so other keys are kept. The wrapper runs the original command and pipes the combined output through a subcortex squeezer that preserves the exit status. Caveats:
  - stdout and stderr get merged unless handled carefully.
  - The permission prompt, UI and permission allowlists see the **rewritten** command, and the UI marks it "input rewrite".
  - It runs before the output size is known.
  - Moving the original command into an external process bypasses Crush's in-shell command block list, so keep it in-shell, e.g. `{ ORIG
} >"$F" 2>&1; rc=$?; subcortex squeeze "$F"; (exit $rc)`.
  - It is a semantic change, not a Crush feature. Never combine it with `"decision":"allow"`.

**(c) Compaction: no hook and no veto.** Crush auto-summarizes when remaining context ≤ 20k tokens (window > 200k) or ≤ 20% of the window (otherwise). The user can also trigger "Summarize Session". `options.disable_auto_summarize` turns it off. The summary becomes an assistant message with `is_summary_message=1`, and `sessions.summary_message_id` is set; later turns send only messages from the summary onward. Out-of-band fallback (**design proposal**, relies on internal and unstable schema):
- Read `<DataDirectory>/crush.db` (default `<cwd>/.crush/crush.db`, SQLite) read-only.
  - Tables: `sessions(id, summary_message_id, …)`, `messages(id, session_id, role, parts JSON, is_summary_message, created_at, …)`.
- Snapshot: on each PreToolUse, or from the daemon, record the last N messages.
- Re-inject: when `summary_message_id` for `session_id` changes, return the snapshot once as `{"context":…}` on the next PreToolUse.

Datadir ownership is a flock on `crush.lock`, not on the DB, so concurrent SQLite reads are fine **[UNVERIFIED under WAL/ncruces driver]**.

## 5. Exit codes and output semantics (from `runner.runOne` / `parseStdout`)

| Outcome | Result |
|---|---|
| exit 0, empty/whitespace stdout | no opinion. Tool proceeds through the normal permission flow |
| exit 0, valid JSON object | parsed as envelope |
| exit 0, invalid JSON / non-object (array, string) / unknown fields | no opinion (silently ignored) |
| exit 0, `updated_input` not a JSON object | patch rejected with a warning and ignored |
| **exit 2** | **DENY this tool call.** stderr (trimmed) is the reason, or "blocked by hook". stdout ignored |
| **exit 49** | **HALT the turn** (deny + `StopTurn`). stderr is the reason, or "turn halted by hook" |
| exit 1 / any other non-zero / 127 command-not-found / shell parse error / spawn error | non-blocking error. Logged, no opinion (parse and spawn errors map to 1) |
| timeout (default **30 s**) or parent cancel | no opinion. Shebang subprocesses are killed; in-process ones are abandoned after a 1 s grace |
| stderr | used only as the reason on exit 2 or 49; otherwise logged |

Aggregation: all matching hooks run in parallel, deduplicated by `command`, and are combined in config order. Deny beats allow, which beats none. `halt` is sticky. `context`/`reason` are joined with `\n`. `updated_input` patches are shallow-merged in order, and dropped on deny or halt. **Our output is combined with the user's other hooks**, and their denials still apply.

**Never emit (each of these blocks, denies, stops, or changes approval):**
1. Exit code **2** → tool call denied.
2. Exit code **49** → whole turn halted.
3. `{"decision":"deny"}`, case-insensitive (`DENY`, `Deny`).
4. `{"halt":true}`. It halts the turn and **also blocks the tool call**, even with no deny.
5. `{"hookSpecificOutput":{"permissionDecision":"deny"}}`.
6. `{"decision":"allow"}` or `{"hookSpecificOutput":{"permissionDecision":"allow"}}`. These don't block, but they **auto-approve** the tool call and skip the user's permission prompt, which is a security side effect. Never emit them.
7. Any `updated_input` / `hookSpecificOutput.updatedInput` other than a deliberately designed rewrite, because it changes what the tool executes.

Ignored, so harmless: top-level `continue:false`, `decision:"block"` (Crush knows only allow/deny), `systemMessage`, `suppressOutput`, a `version` above 1.
**Safe outputs:** empty stdout, `{}`, `{"context":"…"}`. Always exit 0.

## 6. MCP server registration (fallback `subcortex mcp`)

JSON (`~/.config/crush/crush.json`, merged under `mcp`; `type` is **required** by the schema):
```json
{
  "mcp": {
    "subcortex": {
      "type": "stdio",
      "command": "/Users/me/.local/bin/subcortex",
      "args": ["mcp"],
      "env": {"SUBCORTEX_TUI": "crush"},
      "timeout": 10,
      "disabled": false
    }
  }
}
```
Other fields in the `MCPConfig` struct:
- `disabled_tools`, `enabled_tools`: string arrays.
- `timeout`: int seconds, default 10 (30 if `oauth`).
- `url`, `headers`, `oauth`, `oauth_client_id`, `oauth_client_secret`, `oauth_callback_port`, `sessionless`: http/sse only.

`command` and `args` are shell-expanded at load, so avoid a literal `$` in args. crushrc form: `mcp add subcortex --type stdio --command /Users/me/.local/bin/subcortex --args mcp --timeout 10`. The `mcp` key is a map keyed by server name, so uninstall deletes `mcp.subcortex`.
- Tool names reach the model and hooks as `mcp_subcortex_<tool>`. Make sure our own PreToolUse matcher (`^bash$`) doesn't match them.
- Permissions: `permissions.allowed_tools` (exact tool names or `tool:action`, no globs) skips the prompts, e.g. `"mcp_subcortex_snapshot"` **[UNVERIFIED that MCP permission requests use the full `mcp_<server>_<tool>` name]**.
- Server `instructions` (from the initialize result) are appended to the system prompt on every turn (see §4a).
- Hidden, experimental: `--channels server:subcortex` together with the MCP capability `experimental["claude/channel"]` lets a server push `notifications/claude/channel` (≤64 KB). In v0.96.1 the event is published but no consumer was found that injects it into the session **[UNVERIFIED / do not rely on]**.

## 7. Pending upstream and unverified items

- **Open PR #3146** "add more lifecycle hooks" (open, updated 2026-09-20; not in v0.96.1). It would add `PostToolUse` (`updated_input` **replaces** the tool output; `halt` supported), `SessionStart`, `SessionEnd` (2 s timeout), `TurnStart`, `TurnEnd` (`text`), `StopFailure`, `Interrupt`, `PermissionRequest`, `PermissionResult`, `PreCompact` (**`halt:true` prevents compaction, so never emit it there**) and `PostCompact`. Lifecycle payloads would carry only `{event, session_id, cwd}`. If it merges, behaviors 2 and 3 become possible and behavior 4 only partly (SessionStart would be observe-only). Gate on version detection. Don't pre-register these keys now: they are inert today but would activate automatically on upgrade, and the payload shapes may change.
- **Draft PR #3825** `UserPromptSubmit`: `context` is **prepended** to the outbound prompt, and deny/halt are ignored. Unmerged.
- **[UNVERIFIED]** Exact TTY `--version` decoration. The TTY output's first line is a logo; parse with the regex.
- **[UNVERIFIED]** Whether a model switch (UpdateModels → buildTools) re-reads hooks mid-session. The code path suggests it does. Tell users to restart.
- **[UNVERIFIED]** `CRUSH_SKIP_DATADIR_LOCK` behavior, and whether concurrent SQLite reads of `crush.db` are safe under the ncruces/modernc drivers.
- **Verified from code, but may change:** the `crush.db` schema (internal, migrated by goose; latest migration 20260912).
