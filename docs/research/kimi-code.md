# Kimi Code CLI (Moonshot) — subcortex hook adapter spec

Target: `@moonshot-ai/kimi-code` **2.0.2** (released 2026-09-19). Verified against the upstream source at git tag `@moonshot-ai/kimi-code@2.0.2`, not only the docs.

> **Different product with the same binary name.** The old Python **kimi-cli** (`MoonshotAI/kimi-cli`, last release 1.51.0, repo archived) also installs a binary called `kimi`. It keeps its config in `~/.kimi/config.toml`. This spec covers only the new TypeScript **Kimi Code CLI** (`MoonshotAI/kimi-code`), which keeps its config in `~/.kimi-code/`. The two CLIs have different hook engines. See §1 for how to tell them apart.

---

## 1. Sources, versions, detection

**Sources used**
- Docs:
  - https://www.kimi.com/code/docs/en/kimi-code-cli/customization/hooks.html
  - …/configuration/config-files.html
  - …/customization/mcp.html
  - …/customization/plugins.html
  - Repo copies of the docs: `docs/en/configuration/{data-locations,env-vars}.md`
- Source at tag `@moonshot-ai/kimi-code@2.0.2` (https://github.com/MoonshotAI/kimi-code):
  - `packages/agent-core-v2/src/features/externalHooks/`
    - `configSection.ts`: the zod schema.
    - `internal/types.ts`: the event list.
    - `internal/runHook.ts`: exit codes, JSON parsing, timeout.
    - `internal/matchHooks.ts`: matcher, dedupe, payload.
    - `internal/userPrompt.ts`: how prompt injection is rendered.
    - `agent/agentExternalHooksService.ts`: per-event wiring.
    - `session/sessionExternalHooksService.ts`: SessionStart and SessionEnd.
    - `app/externalHooksRunnerService.ts`: loads config hooks and plugin hooks.
  - `src/app/bootstrap/bootstrap.ts`: resolves `KIMI_CODE_HOME`.
  - `src/app/config/configService.ts`: validates config sections.
  - `src/app/mcpConfig/configLoader.ts` and `src/mcpCore/config-schema.ts`: MCP config.
  - `src/app/plugin/{manager,store,types}.ts`: plugins.
- Release notes: `docs/en/release-notes/changelog.md` and `apps/kimi-code/CHANGELOG.md`.

**Latest version:** 2.0.2 (2026-09-19). The 2.0.0 release (2026-09-17) followed 0.43.1; nothing changed in the hooks.

**Minimum versions**
- **0.2.0** (2026-05-26, the first public release) already had `[[hooks]]`, including UserPromptSubmit, PostToolUse, PreCompact, PostCompact and SessionStart. It ran on the legacy `packages/agent-core` engine.
- **0.33.0** (2026-08-05) made `agent-core-v2` the default engine. Everything in this spec was checked against v2 code. **Recommended minimum: 0.33.0.**
- At 2.0.2 the legacy engine no longer exists in the tree, so `KIMI_CODE_LEGACY_FLAG` is gone.
- Other hook milestones:
  - 0.6.0: hook results are persisted into model context.
  - 0.8.0: PermissionRequest and PermissionResult events.
  - 0.14.0: Interrupt event.
  - 0.20.1: hooks inside plugin manifests.
  - 0.32.0: TurnStarted, UserPromptQueued, TaskStarted and SessionHeartbeat events.

**Version detection:** run `kimi --version` (or `-V`). It uses commander, so it prints the bare version, e.g. `2.0.2`.
- The legacy Python CLI prints `kimi, version 1.51.0` instead.
- Rules:
  - Bare output that matches `^\d+\.\d+\.\d+` means Kimi Code.
  - Output that matches `^kimi, version ` means legacy kimi-cli. Do not install this adapter there.
- Binary name: `kimi`. npm package: `@moonshot-ai/kimi-code`. User-Agent: `kimi-code-cli/<ver>`. Hook payloads carry `client_type: "kimi_code_cli"`.

---

## 2. Config paths, relocation, trust

| What | Path | Format |
|---|---|---|
| Hooks (user level only) | `$KIMI_CODE_HOME/config.toml`, default `~/.kimi-code/config.toml`, under the `[[hooks]]` array of tables | TOML |
| MCP (user) | `$KIMI_CODE_HOME/mcp.json` | JSON `{"mcpServers":{…}}` |
| MCP (project) | `<cwd>/.kimi-code/mcp.json`, plus `<git-root>/.mcp.json` (Claude-compatible). Later layers override by server name. | JSON |
| Plugins | `$KIMI_CODE_HOME/plugins/installed.json` and `$KIMI_CODE_HOME/plugins/managed/<id>/` | JSON |
| Sessions | `$KIMI_CODE_HOME/sessions/<workDirKey>/<sessionId>/agents/main/wire.jsonl`. `workDirKey` is `wd_<slug>_<sha256(workDir)[:12]>`. `sessionId` is `session_<uuid>`. `$KIMI_CODE_HOME/session_index.jsonl` has one line per session with `sessionId`, `sessionDir` and `workDir`. | JSONL |
| Project-local config | `<project>/.kimi-code/local.toml`. It only holds `[workspace] additional_dir` and **cannot hold hooks**. | TOML |

- **There are no project-level hooks.** Hooks come from exactly two places:
  - the user `config.toml`;
  - the manifests of enabled plugins, which are also per user.
- **Relocation:**
  - `KIMI_CODE_HOME=/tmp/x` moves everything to `/tmp/x`: config, sessions, logs, credentials, plugins and `mcp.json`. The resolution order is `homeDir ?? env.KIMI_CODE_HOME ?? ~/.kimi-code`.
  - Generic `~/.agents/*` resources (skills, `AGENTS.md`) and the first-launch migration from legacy `~/.kimi` still use the real OS home (`os.homedir()`, which follows `$HOME`).
- **Isolated e2e environment:**

  ```sh
  HOME=$T/home KIMI_CODE_HOME=$T/kimi \
  KIMI_CODE_NO_AUTO_UPDATE=1 KIMI_DISABLE_TELEMETRY=1 KIMI_CODE_WATCH=0 \
  KIMI_MODEL_NAME=mock KIMI_MODEL_API_KEY=x KIMI_MODEL_PROVIDER_TYPE=openai KIMI_MODEL_BASE_URL=http://127.0.0.1:<mock>/v1 \
  kimi -p "…" --output-format stream-json
  ```

  - The `KIMI_MODEL_*` variables create an in-memory provider, so no login is needed.
  - Hooks fire the same way in the TUI, in `kimi -p`, in `kimi web` and in `kimi acp`, because they all run the v2 engine.
  - To force compaction:
    - in the TUI, run `/compact`;
    - otherwise, set a small `KIMI_MODEL_MAX_CONTEXT_SIZE` together with `[loop_control] reserved_context_size`.
- **Trust and enablement:**
  - Hooks in `config.toml` need no trust prompt and no enable flag. Hooks are on whenever they are configured.
  - A project `.kimi-code/mcp.json` stdio server needs the folder to be trusted through the workspace trust prompt.
  - A user-level `mcp.json` needs no trust.
- **When changes take effect:**
  - The hook runner loads `config.hooks` plus the plugin hooks once at startup (`ExternalHooksRunnerService.load`).
  - It reloads on `plugins.onDidReload` only. A `config.toml` watcher event does not reload it.
  - Docs say "start a new session". Assume a **new `kimi` process** is needed. `/reload` may also work (unverified; see §7).
- **Config validation hazard (critical for the installer):**
  - `HookDefSchema` is `z.object({event: enum, matcher?: string, command: string.min(1), timeout?: int 1..600}).strict()`.
  - If any single `[[hooks]]` entry is invalid, `ConfigService.buildValidated` logs "Ignored invalid config section 'hooks'" and **drops the entire `hooks` section, including the user's own hooks**. Invalid means an unknown key such as `name`, `id` or `enabled`, a misspelled event name, a timeout of 0 or more than 600, or a non-integer timeout.
  - The docs say the config then "fails to load". The source says the section is ignored with a warning. Either way, **never add extra keys and never write a bad event name.**

---

## 3. Registration (TOML) and tagging

TOML is snake_case, but the hook fields are single words. Timeout is in **seconds**, must be an integer from 1 to 600, and defaults to 30.

```toml
# >>> subcortex (managed; do not edit) >>>
[[hooks]]
event = "UserPromptSubmit"
command = "subcortex hook kimi-code UserPromptSubmit"
timeout = 5

[[hooks]]
event = "PreCompact"
command = "subcortex hook kimi-code PreCompact"
timeout = 10

[[hooks]]
event = "PostCompact"
command = "subcortex hook kimi-code PostCompact"
timeout = 5

[[hooks]]
event = "SessionStart"
command = "subcortex hook kimi-code SessionStart"
timeout = 5

# Optional, observe-only (stats). It CANNOT replace output, see §4.
[[hooks]]
event = "PostToolUse"
matcher = "^Bash$"
command = "subcortex hook kimi-code PostToolUse"
timeout = 5
# <<< subcortex <<<
```

**Matcher behaviour**
- The matcher is a JavaScript `new RegExp(matcher).test(value)`. It is unanchored.
- An empty or missing matcher matches everything.
- **An invalid regex silently never matches**, so the hook never runs.
- What the matcher is tested against depends on the event:

  | Event | Value tested |
  |---|---|
  | UserPromptSubmit | The prompt's text parts joined with spaces |
  | PreToolUse, PostToolUse | The tool name (`Bash`, `Read`, `Edit`, `Write`, `mcp__srv__tool`, …) |
  | PreCompact, PostCompact | `manual` or `auto` |
  | SessionStart | `startup` or `resume` (never `fork`, because forks skip SessionStart) |
  | SessionEnd | `exit` or `archive` |

**Dedupe and ordering**
- Hooks are deduped by `(cwd, command)`, so identical commands run once. Our commands differ per event, so none of ours collide.
- All matching hooks run in parallel.

**Tagging for uninstall**
- The strict schema forbids a `name` field, so the tag must be the command string itself.
- Uninstall removes every `[[hooks]]` table whose `command` starts with `subcortex hook kimi-code `.
- The comment fences are a convenience only. Kimi's `tomlWriteback` makes line-level edits and seems to keep comments in untouched regions, but don't rely on them.
- Parse the file with a real TOML parser, filter the `hooks` array, and write it back. Leave every other key untouched.

**Alternative: package as a plugin (clean ownership, but TUI-driven)**
- Put a manifest at `<dir>/kimi.plugin.json`:

  ```json
  {"name":"subcortex","version":"0.1.0",
   "hooks":[{"event":"UserPromptSubmit","command":"subcortex hook kimi-code UserPromptSubmit","timeout":5}],
   "mcpServers":{"subcortex":{"command":"subcortex","args":["mcp"]}}}
  ```

- Install it from the TUI with `/plugins install <dir>`. That copies the directory to `$KIMI_CODE_HOME/plugins/managed/subcortex/`.
- Plugin hooks differ from config hooks in two ways:
  - They run with **cwd = plugin root**. The payload's `cwd` is still the session directory.
  - They get the extra environment variables `KIMI_CODE_HOME` and `KIMI_PLUGIN_ROOT`.
- There is **no CLI `kimi plugin install`**; the only commands are `/plugins …` inside the TUI.
- Writing `installed.json` by hand is technically possible: `{"version":1,"plugins":[{"id":"subcortex","root":"<abs>","source":"local-path","enabled":true,"installedAt":"<iso>"}]}`. This is **undocumented**, so avoid it. Prefer `config.toml`.

---

## 4. Events: names, payloads, responses

**Payload basics**
- The payload goes to the hook on stdin as one line, `JSON.stringify(input)`, followed by EOF.
- Top-level keys are converted to snake_case. **Nested keys are not converted**: `tool_input` is exactly what the model sent, and prompt parts keep `imageUrl` in camelCase.
- Keys whose value is `undefined` are omitted.
- Every payload carries `hook_event_name`, `session_id`, `cwd` (the session cwd) and `client_type`.
- Agent-scoped events also carry `session_title` once a title exists.
- **The payload never includes `transcript_path` or an agent id.**

**Event name mapping for subcortex**

| subcortex behaviour | Kimi event | Blocking-capable? | Output used? |
|---|---|---|---|
| prompt submit | `UserPromptSubmit` | yes (awaited) | **yes**: context injection |
| after tool | `PostToolUse` (and `PostToolUseFailure`) | no (fire-and-forget) | **ignored** |
| pre-compact | `PreCompact` | awaited (compaction waits up to the timeout) | **ignored** |
| session start | `SessionStart` (`startup` or `resume` only) | awaited (session creation waits) | **ignored** |
| (post-compact marker) | `PostCompact` | fire-and-forget | ignored |

### 4.1 UserPromptSubmit
- The hook runs only when the prompt's origin is `user`. Subagent, cron, task and system prompts do not trigger it.

Stdin:

```json
{"hook_event_name":"UserPromptSubmit","session_id":"session_4f0c2a8e-9b1d-4c3e-8a77-2f5d0e6b1c90","cwd":"/Users/me/proj","client_type":"kimi_code_cli","session_title":"Fix login bug","prompt":[{"type":"text","text":"rename getUser to fetchUser in src/api.ts"}],"is_steer":false}
```

- **`prompt` is an array of content parts, not a string.** A part is `{"type":"text","text":…}` or `{"type":"image_url","imageUrl":{"url":…}}`.
- To classify, concatenate the `text` parts.
- `is_steer` is always `false` on this path.

**(a) Context injection.** On exit 0, the hook's "message" is added to the context as a **user-role** message: `<hook_result hook_event="UserPromptSubmit">\n{message}\n</hook_result>`, with origin `hook_result`. The TUI also shows it as a hook-result card.
- **Where the message comes from:**
  1. If stdout parses as a JSON object and has a string field `message`, that is used. `hookSpecificOutput.message` also works.
  2. Otherwise the **whole trimmed stdout is used verbatim**. This includes non-JSON text, and also JSON without a `message` field.
- **What to emit:**
  - Plain text: `Prefer the most direct, minimal path.`
  - Or JSON: `{"message":"Prefer the most direct, minimal path."}`
- **Traps:**
  - `{}` gets injected as the literal text `{}`.
  - `{"hookSpecificOutput":{"additionalContext":"…"}}` in Claude style is **not understood**. The raw JSON would be injected as text.
- The message is dropped if the exit code is not 0, if the hook timed out, or if it was aborted.
- The message goes into context **before** the user's prompt message in the same turn.
- Several hooks' messages are joined with a blank line, each wrapped in its own tag.
- Emit **empty stdout** when there is nothing to inject.

### 4.2 PostToolUse and PostToolUseFailure
- These fire **after** the result is delivered, through `notifyPostToolUse`, which is `fireAndForget`. That means **(b) replacing tool output is IMPOSSIBLE** via hooks.
- The same holds for every other kind of change: no replace, no block, no append. The return value is discarded entirely.
- `PreToolUse` cannot rewrite input either. Only `hookSpecificOutput.permissionDecision` and `message` are parsed; there is no `updatedInput`.
- The payload's `tool_output` is **truncated to 2000 chars**. Text parts are joined.
- The failure variant carries `error` (Kimi error payload) and no `tool_output`.

```json
{"hook_event_name":"PostToolUse","session_id":"session_4f0c…","cwd":"/Users/me/proj","client_type":"kimi_code_cli","session_title":"Fix login bug","tool_name":"Bash","tool_input":{"command":"npm test","timeout":120000},"tool_call_id":"call_01HZX…","tool_output":"> proj@1.0.0 test\n> vitest run\n … (≤2000 chars)"}
```

- Kimi already externalizes huge tool results itself: results over roughly 50k chars carry an `output_path`, and lists spill to `kimi-file://` files.
- Subcortex can only observe here, for example to record stats. **Recommendation:** skip registering PostToolUse, or register it with an `^Bash$` matcher for telemetry only.
- The MCP fallback (§6) can offer an opt-in `run_compact` tool, but it cannot intercept the built-in `Bash` tool.

### 4.3 PreCompact
- The call is awaited (`runner.trigger`), and compaction starts only after every PreCompact hook finishes or times out.
- **The return value is ignored**, including exit 2 and any JSON. That makes it the safe place to snapshot.

```json
{"hook_event_name":"PreCompact","session_id":"session_4f0c…","cwd":"/Users/me/proj","client_type":"kimi_code_cli","session_title":"Fix login bug","trigger":"auto","token_count":241337}
```

- **No transcript path is provided.** Find it like this:
  1. Read `$KIMI_CODE_HOME/session_index.jsonl`.
  2. Take the record with `sessionId == session_id`. Its `sessionDir` is the absolute session directory; verified in `sessionLifecycleService.appendSessionIndexEntry`.
     - Ignore tombstone records of the form `{"sessionId":…,"deleted":true}`.
  3. Read `<sessionDir>/agents/main/wire.jsonl`.
  4. As a fallback, glob `$KIMI_CODE_HOME/sessions/*/<session_id>/agents/main/wire.jsonl`.
  - `$KIMI_CODE_HOME` comes from the environment, defaulting to `~/.kimi-code`. Config hooks inherit the environment of the `kimi` process.
- **wire.jsonl format**
  - The first line is `{"type":"metadata","protocol_version":"1.5",…}`.
  - After that, each line is `{"type":…, …payload, "time":…}`.
  - Conversation content lives in these records:
    - `context.append_message` holds a `message` with `role`, `content[]` and `origin` (`user`, `hook_result`, `compaction_summary`, …).
    - `context.append_loop_event` holds an `event` of type `step.begin`, `content.part`, `tool.call`, `tool.result` or `step.end`.
  - Boundaries:
    - `context.apply_compaction` marks a compaction.
    - `context.undo` with a count N retracts messages.
    - `context.clear` resets the conversation.
  - Tail the file and take the last N user and assistant texts.
- **Caveat:** the service is agent-scoped, so a **subagent's** compaction also fires PreCompact with the same `session_id`. The payload does not distinguish main from subagent. Snapshotting `agents/main/wire.jsonl` is still correct.

### 4.4 SessionStart
- The call is awaited, and **the output is ignored**. It cannot inject context.
- `source` is `startup` or `resume`. Forks never fire it. Compaction does **not** start a new session, so SessionStart **never fires after compaction**.

```json
{"hook_event_name":"SessionStart","session_id":"session_4f0c…","cwd":"/Users/me/proj","client_type":"kimi_code_cli","source":"resume","model":"kimi-code/k3","profile":"default"}
```

### 4.5 PostCompact
- Fire-and-forget. The output is ignored.

```json
{"hook_event_name":"PostCompact","session_id":"session_4f0c…","cwd":"/Users/me/proj","client_type":"kimi_code_cli","session_title":"Fix login bug","trigger":"auto","estimated_token_count":18422}
```

**(c) Compaction strategy for Kimi**
1. `PreCompact` writes the snapshot and exits 0 with empty stdout.
2. `PostCompact` or `PreCompact` sets a `pending_reinject[session_id]` flag.
3. On the **next `UserPromptSubmit`** for that session, the hook prints the snapshot, plus the simple-path hint if needed, as plain text or `{"message": …}`. Then it clears the flag.

- **Limitation:** after an auto-compaction in the middle of a turn, the model keeps going without the snapshot until the user's next prompt.
- Kimi's compaction prompt already appends a "Context Recovery" footer that points the model at `wire.jsonl`. The model can therefore still recover exact text on its own.

---

## 5. Exit codes and output semantics (v2 `runHook.ts`)

| Result | Effect |
|---|---|
| exit 0, empty stdout | Allow; nothing injected. |
| exit 0, stdout is not JSON | Allow; for UserPromptSubmit, the stdout text is injected. |
| exit 0, JSON object | `message` or `hookSpecificOutput.message` is used as the message. **If `hookSpecificOutput.permissionDecision === "deny"`, it BLOCKS.** |
| exit 0, JSON that fails the loose schema (an array or a bare scalar) | Treated as non-JSON, so the raw stdout is injected (UserPromptSubmit). |
| **exit 2** | **BLOCK on any event.** The reason is stderr trimmed, or "Blocked by <event> hook". Observation events ignore the block, but UserPromptSubmit, PreToolUse and Stop honour it. |
| exit 1, or any other non-zero code | Allow (fail-open). For UserPromptSubmit the message is **discarded**. |
| Timeout | Allow. The process group gets SIGTERM, then SIGKILL 100 ms later. Stdout is discarded for injection. |
| Spawn error, crash, abort | Allow. |
| stderr | Used **only** as the block reason on exit 2. Otherwise it is ignored and not shown. |

- **Default timeout:** 30 s. It is set per hook in integer seconds from 1 to 600.
- **Environment:** config hooks run through `spawn(command, {shell:true, detached:true (non-Windows)})` with the inherited environment and cwd set to the session cwd.
- **Responses that block, deny or stop. The adapter must NEVER emit any of these on any event:**
  1. **Exit code `2`.** On UserPromptSubmit it skips the model call for that turn and puts a "blocked" assistant message in context. On PreToolUse it denies the tool. On Stop it forces the turn to continue.
  2. **Exit 0 with stdout `{"hookSpecificOutput":{"permissionDecision":"deny", …}}`**, on any event, including UserPromptSubmit.
  3. For Stop hooks (subcortex doesn't register them): either of the above keeps the agent looping.
- **Safe adapter contract for every Kimi event:**
  - Always exit 0.
  - Stdout is empty, or plain text, or `{"message":"…"}`. The last two are for UserPromptSubmit only.
  - Diagnostics go to stderr or to a log file.
  - Catch every exception and still exit 0 with empty stdout.
  - Stay well under the timeout; aim for less than 1 s.
  - Never write Claude-style JSON (`decision`, `hookSpecificOutput.*`) to stdout.

---

## 6. MCP fallback: `subcortex mcp`

User-level file `$KIMI_CODE_HOME/mcp.json`:

```json
{
  "mcpServers": {
    "subcortex": {
      "command": "subcortex",
      "args": ["mcp"],
      "startupTimeoutMs": 10000
    }
  }
}
```

- An entry with `command` is a stdio server. `transport: "stdio"` is inferred.
- **Optional fields:**

  | Field | Notes |
  |---|---|
  | `env` | object of strings |
  | `cwd` | |
  | `enabled` | boolean |
  | `deferred` | boolean |
  | `startupTimeoutMs` | default 30000 |
  | `toolTimeoutMs` | default 60000 |
  | `enabledTools` | array |
  | `disabledTools` | array |
  | `executor` | `local` or `kaos` |

- Unknown keys are stripped silently. The zod object is non-strict.
- **Invalid JSON or an invalid entry makes the whole file fail with `CONFIG_INVALID`.** Merge carefully.
- The tag is the server key `subcortex`. Uninstall deletes `mcpServers.subcortex`.
- There is **no `kimi mcp add` CLI command**; the interactive route is `/mcp-config` in the TUI. Edit the JSON directly.
- Tools are exposed as `mcp__subcortex__<tool>`. Permission rules take patterns like `mcp__subcortex__*`.
- A plugin can declare the same server under `mcpServers` in `kimi.plugin.json`.
- Servers added in the middle of a session attach only to **new** sessions.

---

## 7. Unverified or flagged

- **Whether `/reload` re-reads `[[hooks]]` from config.toml:** the runner's reload is only wired to `plugins.onDidReload`. Assume a restart is needed.
- **Whether `tomlWriteback` keeps our comment fences** when Kimi rewrites `config.toml` (for example after `/model` or `/login`): the line-edit design suggests yes, but we did not test it. Uninstall matches on the command prefix, not the comments.
- **Whether a whole-config load failure can also happen:** the docs say extra fields make "the config file fail to load", while the source shows only the `hooks` section being ignored with a warning. Both are bad, so avoid both.
- **Session lookup:**
  - Verified in the source: the directory segment is the workspace id, and `workspaceService.ts` computes it as `encodeWorkDirKey(root)`.
  - That root is the workspace root, which may not equal the payload's `cwd`. So don't recompute the key yourself.
  - Use `session_index.jsonl` first, then the glob fallback.
- **Plugin install by writing `installed.json` directly:** this is inferred from `store.ts`, is not a documented path, and was not tested.
- **Hook behaviour in `kimi web` and `kimi acp`:** the same engine is used, but we did not run it.
- **Claims that come from reading source rather than running it:**
  - the 2000-character `tool_output` truncation;
  - the subagent PreCompact firing;
  - the order of the injected message relative to the prompt.
