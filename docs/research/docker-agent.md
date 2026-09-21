# Docker Agent (formerly cagent): subcortex hook adapter spec

Target: **docker-agent v1.142.0** (tag `v1.142.0`, released 2026-09-21, commit `99bdcf84`). Everything below was checked against the Go source at that tag, cloned to `scratchpad/src2/docker-agent`, not only against the docs. File paths are relative to that repo root. Line numbers refer to v1.142.0.

> The repo was renamed: `github.com/docker/cagent` now redirects to `github.com/docker/docker-agent`. The binary was renamed from `cagent` to `docker-agent` in v1.30.0 (2026-03-09, CHANGELOG). Config and data directories still use the `cagent` name.

---

## 1. Sources, versions, detection

**Sources used**
- Docs, as copies in the repo:
  - `docs/configuration/hooks/index.md`: the hooks reference, 81 KB. Its canonical URL is https://docs.docker.com/ai/docker-agent/configuration/hooks/.
  - `docs/configuration/user-settings/index.md`
  - `docs/features/cli/index.md`
  - `docs/providers/custom/index.md`
  - `docs/providers/openai/index.md`
  - `docs/tools/mcp/index.md`
- Source:
  - `pkg/config/hooks_dropin.go`: the hooks.d loader.
  - `pkg/config/latest/types.go`: the `HooksConfig` schema at :2643 and `HookDefinition` at :2927.
  - `pkg/config/latest/hooks.go`: `Validate`.
  - `pkg/hooks/events/contracts.go`: the event capability table.
  - `pkg/hooks/executor.go`: `runHook` at :318, `aggregate` at :380.
  - `pkg/hooks/protocol.go`: stdout parsing and output validation.
  - `pkg/hooks/handler.go`: the command runner.
  - `pkg/hooks/types.go`: the `Input` and `Output` wire structs.
  - `pkg/shellpath/shellpath.go`
  - `pkg/runtime/hooks.go`, `pkg/runtime/loop.go`, `pkg/runtime/session_compaction.go`, `pkg/runtime/runtime.go`: the per-event dispatch sites.
  - `pkg/runtime/toolexec/dispatcher.go` and `pkg/runtime/toolexec/hooks_input.go`
  - `pkg/hooks/builtins/builtins.go`: auto-injected hooks.
  - `pkg/tools/builtin/shell/shell.go`
  - `pkg/session/{store.go,migrations.go}`
  - `cmd/root/{root.go,run.go,flags.go,backend.go,acp.go,serve.go,version.go}`
  - `pkg/paths/paths.go`
  - `pkg/cli/runner.go`
- Older tags, fetched with `gh api …/contents?ref=` and compared: `pkg/hooks/executor.go` and `pkg/config/latest/types.go` at v1.100.0 and v1.137.0; `pkg/hooks/builtins/builtins.go` at v1.100.0 and v1.137.0.
- Release notes: `gh release view v1.142.0` and `CHANGELOG.md`, which only goes up to v1.141.0.

**Latest version:** v1.142.0 (2026-09-21). Releases come every few days: v1.141.0 (09-16), v1.140.0 (09-15) and so on. v1.142.0 changes nothing about hooks. Its main changes are native Anthropic compaction, WASM runtime work, and token-accounting fixes.

**Version milestones that matter:**

| Version | Date | What |
|---|---|---|
| v1.30.0 | 2026-03-09 | `cagent` renamed to `docker-agent` |
| v1.53.0 | 2026-04-28 | `before_compaction` / `after_compaction` events added |
| v1.74.0 | 2026-06-09 | `user_steering_messages_submit` / `user_followup_submit` events added |
| **v1.100.0** | 2026-07-07 | **`hooks.d` drop-ins** and `DOCKER_AGENT_CONFIG_DIR` added. **Hard minimum.** Every event and field used in §3 exists here: `name`, `type`, `command`, `timeout`, `on_error`. |
| v1.103.0 | 2026-07-09 | A hook event may be written as a single mapping (we always write lists) |
| **v1.137.0** | 2026-09-09 | `tool_response_transform` becomes a **sequential pipeline**. Before this it was concurrent and "first rewrite in config order wins" (see §4b). **Minimum for B2.** |
| v1.138.0 | 2026-09-10 | Event-contract catalog, dedup and `strict_output` added. Exit semantics become the ones in §5. Before this, non-zero exits other than 2 were silently ignored. |

**Binary and detection**
- Standalone binary: `docker-agent`, installed via Homebrew `brew install docker-agent` or a GitHub release.
- Docker CLI plugin: `docker agent`. The same binary is symlinked as `~/.docker/cli-plugins/docker-agent`, and Docker Desktop 4.63+ ships it pre-installed.
- Legacy binary `cagent`: only for versions before v1.30. Those are below the v1.100 minimum, so do not install there.
- **Version string** (`cmd/root/version.go`). The version is the git tag, including the `v` (Dockerfile `-X …version.Version=$GIT_TAG`).
  - `docker-agent version` prints `docker-agent version v1.142.0` and then `Commit: <sha>`.
  - `docker agent version` prints `docker agent version v1.142.0`.
  - Parse with `version v?(\d+)\.(\d+)\.(\d+)`. Local builds print `dev`; treat those as unknown and install anyway.
- Detection signals:
  - `command -v docker-agent`, or `docker agent version` exiting 0;
  - `~/.config/cagent/` exists.
- The first run writes `<config-dir>/.cagent_first_run` (`cmd/root/root.go:350`).

---

## 2. Config paths, format, relocation, trust

| What | Path | Format |
|---|---|---|
| User config | `<config-dir>/config.yaml`. Global hooks go under `settings.hooks`. | YAML |
| **Hook drop-ins (what we own)** | **`<config-dir>/hooks.d/*.yaml` or `*.yml`** | YAML: a bare hooks block (the same schema as `settings.hooks`) |
| Agent hooks | `agents.<name>.hooks` in the agent YAML | YAML |
| Session DB | `<data-dir>/session.db` (SQLite, WAL). `--session-db` overrides it. | SQLite |
| Data dir | `~/.cagent` (`paths.go:112`) | – |
| Cache | `os.UserCacheDir()/cagent`: `~/Library/Caches/cagent` on macOS, `$XDG_CACHE_HOME/cagent` on Linux | – |

**How `<config-dir>` is resolved** (`cmd/root/root.go:43-53`, `pkg/paths/paths.go:96-104`):
1. `--config-dir` flag
2. `DOCKER_AGENT_CONFIG_DIR`
3. `CAGENT_CONFIG_DIR` (legacy)
4. `$HOME/.config/cagent`, built from `os.UserHomeDir()`.

**`XDG_CONFIG_HOME` is not honored.** Never derive the config dir from XDG. Mirror the env-var order above in the installer.

**Other relocation**
- `--data-dir` sets the data dir. There is no env var for it.
- `--cache-dir` sets the cache dir.
- `HOME` relocates everything whose default comes from it.

**Drop-in loader semantics** (`pkg/config/hooks_dropin.go`)
- `LoadHookDropIns()` reads `filepath.Join(paths.GetConfigDir(), "hooks.d")` (:22).
- `os.ReadDir` returns names in lexicographic order. The loader skips subdirectories and any extension other than `.yaml`/`.yml` (:37-43).
  - So `50-subcortex.yaml.bak` is ignored, which gives a cheap way to "disable".
- Each file is parsed with `yaml.UnmarshalWithOptions(data, &hooks, yaml.Strict())` and then `hooks.Validate()` (:58-71).
  - **Any unknown key, unknown event name, bad `type`, bad `on_error`, negative `timeout` or invalid matcher regex causes the whole file to be skipped**, with `slog.Warn("Skipping invalid hooks drop-in file")`. The skip is silent in the UI.
  - Other files and the user's own hooks are unaffected: "A broken drop-in must never break the run" (:47).
  - **Do not put custom tag keys in the file.** Use `name:` and YAML comments instead.
- An empty or comment-only file yields nil hooks, which is fine.

**Merge order** (`cmd/root/run.go:404`, `pkg/teamloader/teamloader.go:425`)
- `GlobalHooks = MergeHooks(settings.hooks, LoadHookDropIns())`
- Per agent: `MergeHooks(MergeHooks(agentConfig.Hooks, globalHooks), cliHooks)`.
- The resulting order is:
  1. agent YAML
  2. `settings.hooks`
  3. hooks.d, in file order
  4. `--hook-*` flags
  5. auto-injected builtins, appended last by `ApplyAgentDefaults` (`pkg/hooks/builtins/builtins.go:171-211`).
- Global hooks are applied to **every agent in the team**, including sub-agents. Agents cannot opt out.

**When config is loaded**
- Drop-ins are read **once per process**, in `runOrExec`. After editing a file, start a new `docker agent` process. New TUI tabs reuse the loaded config.

**Where user-level hooks do and do not apply**
- `settings.hooks` and hooks.d are loaded **only** by `docker agent run` (TUI) and `docker agent run --exec`. `GlobalHooks` is set only at `cmd/root/run.go:404`.
- **They do not apply** to `docker agent serve acp|api|mcp|a2a|chat`. For example, Zed's ACP integration will not fire our hooks.
- Not verified: `--remote`, where the runtime is remote, and `--sandbox`, where the agent runs inside a Docker sandbox and our absolute hook path probably does not exist. If `subcortex-hook` is missing, the shell exits 127, `on_error: ignore` swallows the failure, and nothing happens.

**Trust**
- There is no trust or consent gate for hooks from any source: no `trust` code path in `pkg/hooks` or `pkg/teamloader`.
- Hooks are always on when configured.

**How commands run** (`pkg/hooks/handler.go:137-151, 224-259`; `pkg/shellpath/shellpath.go:51-57, 87-92`)
- `exec.CommandContext($SHELL, "-c", command)`. The shell is **`$SHELL` or `/bin/sh`**, not always `sh`.
- On Windows it is `pwsh`/`powershell -NoProfile -NonInteractive -Command`, or `cmd /C`.
- Stdin: the JSON input.
- Working directory: the runtime/session working dir, or the hook's `working_dir`.
- Env: the process env, plus the runtime env (for example `--env-from-file`), plus the hook's `env:` values (only `${env.X}` is expanded), plus OTel trace-context vars.
- **No host-identifying env var** is set, so the TUI name must travel in argv.
- Stdout and stderr are fully captured. Stderr is surfaced only as the block message on exit 2, or in fail-closed messages.

---

## 3. Registration: one drop-in file we own

**File path:** `<config-dir>/hooks.d/50-subcortex.yaml`. With the default config dir that is `~/.config/cagent/hooks.d/50-subcortex.yaml`.
- Create `hooks.d/` if it is missing.
- Never touch `config.yaml`.
- To uninstall, delete the file.

**Schema** (`HookDefinition`, `types.go:2927`; validation at `types.go:3029-3060`)

| Field | Meaning |
|---|---|
| `type` | **Required.** `command`, `builtin` or `model`. |
| `command` | The command string run under `$SHELL -c`. |
| `timeout` | Integer seconds. `0` or omitted means **60 s**. A negative value is invalid. |
| `name` | Label used in logs and warnings. |
| `on_error` | `warn` (default), `ignore` or `block`. `block` is rejected on events that cannot block. |
| `env` | Map of extra environment variables. |
| `working_dir` | Working directory override. |
| `args` | Only used by builtins. |
| `strict_output` | Added in v1.138. **Do not use**, because strict YAML parsing on older versions would reject the whole file. |

- **Tool events** (`pre_tool_use`, `post_tool_use`, `permission_request`, `tool_input_transform`, `tool_guard`, `tool_response_transform`) take a list of `{matcher, hooks:[…]}`.
  - The matcher is compiled as `^(?:<matcher>)$` (anchored).
  - `""` or `"*"` matches everything.
  - It is case-sensitive and tested against the **tool name only** (`executor.go:106-124`).
- **All other events** take a bare list of hook definitions.

**YAML quoting gotcha.** Our standard command starts with `'`. A plain YAML scalar starting with `'` is parsed as a single-quoted YAML string, and the file breaks, so it gets skipped. **Always emit `command:` as a YAML double-quoted scalar.** YAML double-quoted strings accept JSON string escapes, so JSON-encoding the command string is safe.

```yaml
# Managed by subcortex (subcortex install docker-agent). Do not edit.
# Uninstall: delete this file. Disable: rename to *.yaml.bak
user_prompt_submit:
  - name: "subcortex:user_prompt_submit"
    type: command
    command: "'/ABS/subcortex-hook' docker-agent user_prompt_submit 2>/dev/null || true"
    timeout: 5
    on_error: ignore
user_steering_messages_submit:
  - name: "subcortex:user_steering_messages_submit"
    type: command
    command: "'/ABS/subcortex-hook' docker-agent user_steering_messages_submit 2>/dev/null || true"
    timeout: 5
    on_error: ignore
user_followup_submit:
  - name: "subcortex:user_followup_submit"
    type: command
    command: "'/ABS/subcortex-hook' docker-agent user_followup_submit 2>/dev/null || true"
    timeout: 5
    on_error: ignore
tool_response_transform:
  - matcher: "shell"
    hooks:
      - name: "subcortex:tool_response_transform"
        type: command
        command: "'/ABS/subcortex-hook' docker-agent tool_response_transform 2>/dev/null || true"
        timeout: 10
        on_error: ignore
before_compaction:
  - name: "subcortex:before_compaction"
    type: command
    command: "'/ABS/subcortex-hook' docker-agent before_compaction 2>/dev/null || true"
    timeout: 10
    on_error: ignore
after_compaction:
  - name: "subcortex:after_compaction"
    type: command
    command: "'/ABS/subcortex-hook' docker-agent after_compaction 2>/dev/null || true"
    timeout: 5
    on_error: ignore
turn_start:
  - name: "subcortex:turn_start"
    type: command
    command: "'/ABS/subcortex-hook' docker-agent turn_start 2>/dev/null || true"
    timeout: 5
    on_error: ignore
```

**Why each setting**
- **`on_error: ignore`.** Otherwise every timeout or crash emits a user-visible `Warning` ("<event> hook failed to execute: …"). The warning shows in the TUI and on stderr in `--exec` (`pkg/runtime/hooks.go:132`, `pkg/cli/runner.go:222`). None of the events above is fail-closed, so `ignore` means "silently no-op".
- **`|| true`** guarantees exit 0 even if the binary exits 2. Exit 2 would block `user_prompt_submit`, the steering and follow-up events, and `before_compaction` (§5).
- **Exotic shells.** If `$SHELL` is nushell, which has no `||`, the command fails to parse, exits non-zero, and `on_error: ignore` turns it into a silent no-op. That is fail-open, but the hooks do nothing.
- **The `name:` field is the tag.** Hook identity for dedup covers every field (`executor.go:288-301`), so a user entry that is textually identical would run only once. That is harmless.
- **`turn_start` runs before every model call** (each loop iteration), and `tool_response_transform` runs on every shell call and is sequential. Both add latency directly. The hook must return in well under 100 ms in the common no-op case.
- **Why `before_compaction` and not `pre_compact`** for B3: see §4c.

---

## 4. Per-event stdin and the exact response for each behavior

**Wire format** (`pkg/hooks/types.go:261-459`)
- stdin is one JSON object with **snake_case** keys.
- Common fields: `session_id` (a UUID), `cwd` (filled from the executor's working dir when empty) and `hook_event_name`.
- `dispatchHook` also auto-fills `agent_name` for every runtime event (`pkg/runtime/hooks.go:109-114`).
- Every other field is `omitempty`, so zeros, false values and empty strings are **absent**.
- Field order in the JSON follows the struct order.

**Response protocol** (`protocol.go:13-36`, `executor.go:361-377`, `executor.go:420-427`)
- Empty stdout or `{}` means a no-op.
- Stdout whose trimmed text starts with `{` must be **exactly one** JSON object. Trailing text makes it a failure (`hook output must contain a single JSON object`).
- Any other stdout is plain text. It becomes `additional_context` **only** on events whose contract has `Context:true`: `session_start`, `user_prompt_submit`, `user_steering_messages_submit`, `user_followup_submit`, `turn_start`, `pre_compact`, `worktree_create`. On every other event it is ignored.
- Unknown JSON fields are ignored in non-strict mode. So the camelCase Claude-shaped `hookSpecificOutput` is silently ignored and **does nothing**. Always use `hook_specific_output`.

### (a) Prompt-submit context injection: ✅

- The event is `user_prompt_submit`, dispatched from `pkg/runtime/loop.go:443` via `executeUserPromptSubmitHooks` (`pkg/runtime/hooks.go:674`).
- It fires once per real user message, after the message is added to the session and after `session_start`, and before the first model call.
- It does **not** fire for sub-sessions (transfer_task, background agents, skills).

```json
{"session_id":"5b1f…","cwd":"/work/repo","hook_event_name":"user_prompt_submit","agent_name":"root","prompt":"fix the failing test"}
```

**Response**

```json
{"hook_specific_output":{"additional_context":"<subcortex hint>"}}
```

- Plain-text stdout also works.
- `hook_event_name` inside the output is optional and not checked in non-strict mode.

**Effect**
- The context becomes one `system` message (`contextMessages`, hooks.go:231).
- It is stored in `ls.userPromptMsgs` and threaded into **every model call of that RunStream**, all tool-loop iterations included (loop.go:450, 810).
- It is **not persisted** to the session and is gone on the next user message.
  - Exception: with `settings.cache_stable_prompts: true`, it is recorded as instruction context under the key `hooks/user-prompt` (hooks.go:296-309).
- Contexts from several hooks are joined with `\n` in config order.

**Messages sent while the agent is busy**
- `settings.busy_send_mode` defaults to `steer`. A message sent while the agent is working does **not** fire `user_prompt_submit`. It fires `user_steering_messages_submit`, with `"steering_messages":["…","…"]` (string array, in submission order).
- In `queue` mode it fires `user_followup_submit`, with `"prompt":"…"`.
- Both take the same response and inject transient context for the steered or follow-up turn (loop.go:611, 1063, 1112, 1136).
- Register all three events for full B1 coverage.

### (b) Replace shell tool output: ✅ genuine, non-blocking (v1.137.0 or later)

- The event is `tool_response_transform`, with contract `{ToolMatched, Rewrite: RewriteToolResponse}`. It is sequential and **cannot block** (`contracts.go:65`).
- It is dispatched in `pkg/runtime/toolexec/dispatcher.go:1133` → `applyToolResponseTransform` (:1161).
- It runs **before** the `tool_call_response` event is emitted, the tool message is recorded in the session and SQLite, `post_tool_use` runs, and the next LLM call is made.
- **So the rewrite is what the model sees, what the TUI/`--json` shows, and what is persisted.** Unlike Claude Code, the change is not model-only.

**The shell tool**
- It is named **`shell`** (`pkg/tools/builtin/shell/shell.go:31`) with category `"shell"`.
- Its arguments are `{"cmd": string, "cwd"?: string, "timeout"?: int}` (:95-97).
- Its output is combined stdout and stderr, trimmed.
  - On a non-zero exit the output is `"Error executing command: exit status N\nOutput: …"`, **still with `tool_error` false**, because it is a `ResultSuccess` (:339-360).
  - Empty output becomes `"<no output>"`.
  - A timeout gives `"Command timed out after …\nOutput: …"`.
- `run_background_job` returns a job handle, not output, so it is not worth matching.
- Matcher: `shell`.

```json
{"session_id":"5b1f…","cwd":"/work/repo","hook_event_name":"tool_response_transform","agent_name":"root",
 "tool_category":"shell","tool_name":"shell","tool_use_id":"call_7Qk…","tool_input":{"cmd":"go test ./..."},
 "safety_policy":"autonomous","tool_response":"=== RUN TestA\n…(full raw output)…"}
```

- `tool_response` is a **plain string** (`hooks_input.go:29-36`). It is absent when empty.
- `tool_error:true` appears only on synthesized error responses: validation failures, user rejections, hook blocks and cancellations (`errorResponse`, dispatcher.go:1254).
  - **Skip rewriting when `tool_error` is true.**
- `safety_policy` is one of `strict|balanced|restricted|autonomous`. It is absent for the legacy default.

**Response**

```json
{"hook_specific_output":{"updated_tool_response":"<head>\n…[subcortex: N lines elided; id=…]…\n<tail>"}}
```

- The value is a string. It **replaces** the output.
- Omitting the field, or returning `{}`, keeps the original.
- **An explicit `""` clears the output**, and the recorder then stores `"(no output)"` (dispatcher.go:1195-1199). Never send an empty string.

**Pipeline order** (v1.137+)

| Step | Hook | What it sees or does |
|---|---|---|
| 1 | Agent-YAML transformers, then `settings.hooks`, then hooks.d | **Ours runs here and sees the raw, un-redacted output** |
| 2 | Auto `redact_secrets` | On by default. Also scrubs our rewrite. |
| 3 | Auto `limit_large_tool_results` | Spills anything over 50 KiB or 2000 lines to a temp file and keeps the tail (`builtins.go:195-211`) |

Keep the replacement well under 50 KiB and 2000 lines. Each hook sees the previous rewrite (`pipeline.go`).

**Before v1.137.0:** transformers ran concurrently on the raw output and the **first non-nil rewrite in config order won** (v1.100.0 `executor.go:538`). The limiter was *prepended* (v1.100 builtins.go:171).
- For outputs over the limit, the limiter's rewrite wins and ours is dropped.
- For smaller outputs, ours wins and **bypasses `redact_secrets`**.
- ⇒ **Enable B2 only on v1.137.0 or later.**

### (c) Pre-compaction snapshot: ◐ The event exists but carries no transcript

Two events fire before every compaction. Every compaction goes through `compactWithReason`: manual `/compact`, the proactive threshold, tool-result overflow, context-overflow recovery, and native Anthropic compaction (`pkg/runtime/runtime.go:2073-2094`, `session_compaction.go:71-190`).

1. **`pre_compact`** (`runtime.go:2083`, `hooks.go:738`)

   ```json
   {"session_id":"…","cwd":"…","hook_event_name":"pre_compact","agent_name":"root","source":"auto"}
   ```

   - `source` is `manual`, `auto` or `overflow`.
   - `tool_overflow` is documented but **never emitted** in v1.142.0: the tool-overflow path passes `compactionReasonThreshold`, which becomes `"auto"` (`preCompactSourceFor`, runtime.go:2111; loop.go:1657).
   - **Hazard:** the contract has `Context:true`. **Any plain stdout or `additional_context` is appended to the compaction prompt** (`joinPrompts`, runtime.go:2092), so it changes the summary. The event can also block, which cancels compaction.
2. **`before_compaction`** (`session_compaction.go:75`, `hooks.go:622`). It fires inside `doCompact`, after `pre_compact` allowed the compaction.

   ```json
   {"session_id":"…","cwd":"…","hook_event_name":"before_compaction","agent_name":"root",
    "input_tokens":91234,"output_tokens":2100,"context_limit":100000,"compaction_reason":"threshold"}
   ```

   - `compaction_reason` is `threshold`, `overflow` or `manual`.
   - `context_limit` is absent or 0 when unknown.
   - The contract has `Context:false`, so **plain stdout is ignored**. Only JSON `summary`, `decision:"block"`, `continue:false` or exit 2 act.

**Recommendation:** register **`before_compaction`**, observe only, with empty stdout. It is the event where stray stdout cannot leak into the summary prompt.
- Never return `hook_specific_output.summary`. A non-empty summary replaces the LLM summary verbatim and skips the model call; the first non-empty one in config order wins (executor.go:480-492).
- Never block. Blocking on `compaction_reason:"overflow"` leaves the session unable to make progress, per the docs.

**No transcript is passed.** No `transcript_path` is sent, and `messages` is populated only for `before_llm_call` (types.go:333-339). To snapshot recent messages, use one of these:

| Option | How | Notes |
|---|---|---|
| 1. **Read the session DB read-only** (preferred) | Default `$HOME/.cagent/session.db` (`cmd/root/flags.go:55-60`). `SELECT message_json FROM session_items WHERE session_id=? AND item_type='message' ORDER BY position DESC LIMIT N` (schema: `pkg/session/migrations.go:324`). `message_json` is a `chat.Message`: `{"role":"user|assistant|tool|system","content":"…",…}`. | The DB runs in WAL mode with `busy_timeout(5000)`, so open read-only (`mode=ro`, not `immutable`), use a busy timeout, and never write. Messages are persisted incrementally by the persistence observer (`pkg/runtime/persistence_observer.go:93-110`), so the pre-compaction rows are present. **Limitation:** the hook cannot see a `--data-dir` or `--session-db` override. No env var exposes it. When the file or session is missing, give up silently. |
| 2. Rolling buffer | Record `prompt` from `user_prompt_submit`, `stop_response` from the observational `after_llm_call`, and our own truncated `tool_response_transform` results into subcortex's own state, keyed by `session_id`. | No SQLite dependency. It costs one more registered event (`after_llm_call`). |
| 3. `before_llm_call` `messages` | The full array arrives on every call. | Not recommended. It is sequential and can block, and it sends the entire transcript through a pipe on every model call. |

**What survives compaction anyway:**
- The LLM compaction path keeps a verbatim tail of min(20k tokens, context/5) (`compactor.go:56,75-80`).
- Native Anthropic compaction keeps no tail.

### (d) Post-compaction re-injection: ◐ Via `after_compaction` plus the next `turn_start`

- `session_start` is **not** re-fired with a `compact` source (`session_compaction.go:66-70`).
- `session_start` always carries `"source":"startup"`, and it fires **once per RunStream**: once per user message in the TUI, because `pkg/app/app.go:563-587` starts a RunStream per message, and once per message argument in `--exec`.
- `session_end` (`"reason":"stream_ended"`) likewise fires at the end of every RunStream (loop.go:218). Neither event marks the session lifecycle.

**Step 1: `after_compaction`** (`session_compaction.go:190`). It fires only after a summary has actually been persisted, and its output is ignored.

```json
{"session_id":"…","cwd":"…","hook_event_name":"after_compaction","agent_name":"root",
 "input_tokens":91234,"output_tokens":2100,"context_limit":100000,"compaction_reason":"threshold",
 "summary":"<the applied summary text>"}
```

- The token counts are the *pre*-compaction values.
- Use this event to mark "session X compacted" in subcortex state.

**Step 2: `turn_start`** (`hooks.go:151`, dispatched at `loop.go:806` inside `runTurn`). It fires before every model call.

```json
{"session_id":"…","cwd":"…","hook_event_name":"turn_start","agent_name":"root"}
```

- Ordering (loop.go:597-630): threshold compaction runs at the top of an iteration, before `runTurn`, so `after_compaction` → `turn_start` happen back-to-back in the same iteration. Overflow and tool-overflow compactions are followed by the next iteration's `turn_start`.

**Response**

```json
{"hook_specific_output":{"additional_context":"<re-injected snapshot>"}}
```

- This becomes a **transient** system message for **that model call only**. It is never persisted, except as instruction context under `cache_stable_prompts`.
- For the snapshot to stay visible beyond one call, keep emitting it on every `turn_start` for that `session_id` until the next `user_prompt_submit`, or for N turns.
- The no-op path, `{}` or empty stdout, must be fast.
- Sub-agent turns carry the child's `session_id`, so key all state by `session_id`.

**Alternative:** have the `pre_compact` hook return the snapshot as `additional_context`, which steers the summary to include it. This persists through the summary, but it depends on the LLM and does not apply to native compaction.

---

## 5. Exit codes, failures, and every blocking response to avoid

### Exit and output handling (v1.138+; `executor.go:318-522`)

| Outcome | Handling |
|---|---|
| Exit 0 | Stdout is parsed (see the §4 protocol) and then validated by `validateOutput` (`protocol.go:39-84`). |
| `decision` other than `""` or `"block"` | **Failure.** For example Claude's `"decision":"approve"`. |
| `permission_decision` other than `allow`, `ask`, `deny` or `""` | **Failure.** |
| Exit 2 on an event with `CanBlock` | **Block.** Trimmed stderr becomes the block message. |
| Exit 2 on a non-blocking event | Only a warning: "returned exit 2, but this event cannot block". |
| Any other non-zero exit (1, 127, …), exec failure, **timeout**, malformed JSON, JSON followed by text, invalid verdict | **Failure.** Any output is discarded. |
| Failure on a fail-closed event (`pre_tool_use`, both lanes, and `tool_guard`) | **Always deny**, regardless of `on_error` (`contracts.go:41,67`; `executor.go:395`). |
| Failure on any other event | Follows `on_error`: `warn` (default) shows a Warning in the UI or on stderr in `--exec`; `ignore` is silent; `block` blocks on CanBlock events. |
| Parent cancellation, e.g. Ctrl+C | Reported as cancellation, not as a policy denial. |

**Before v1.138:**
- Exit 2 set `Allowed=false` on *any* event.
- Other non-zero exits were ignored silently.
- `continue:false` or `decision:block` set `Allowed=false` on any event (v1.100 `executor.go:430-480`).

Our wrapper exits 0 with `{}` or an allowed rewrite, so every version is safe.

**Timeouts:** the default is 60 s per hook (`types.go:2994`), in whole seconds. A timeout counts as a failure (see the table above).

### Responses that block, stop or veto

**Never emit these:**
- `"continue": false`. It blocks on every CanBlock event and only warns elsewhere.
- `"decision": "block"`.
- Exit 2.
- `"permission_decision": "deny"`, and `"ask"` as well. `ask` forces a prompt, and in non-interactive or `--json` mode the prompt resolves to a reject.

| Event | Can block? | What a block does |
|---|---|---|
| `user_prompt_submit`, `user_steering_messages_submit`, `user_followup_submit` | yes | **Stops the run** (`emitHookDrivenShutdown`, loop.go:444-449) |
| `post_tool_use` | yes | Stops the run loop after the current tool batch (`turn_end` reason `hook_blocked`). Its output is otherwise unused, so **do not register it**. |
| `before_llm_call` | yes | Stops the run. Non-empty `updated_messages` replaces the whole message array. |
| `pre_compact` | yes | Skips compaction. Stdout goes into the summary prompt. |
| `before_compaction` | yes | Vetoes compaction. A non-empty `summary` replaces the LLM summary. |
| `pre_tool_use`, `tool_guard` | yes | **Fail-closed on any failure.** Never register them. |
| `tool_input_transform` | yes | Blocks the tool call. `updated_input` rewrites the arguments. |
| `permission_request` | yes | `deny` rejects the call; `allow` auto-approves it. |
| `worktree_create` | yes | Aborts the run. |
| Non-blocking events (`session_start`, `turn_start`, `turn_end`, `after_compaction`, `tool_response_transform`, `stop`, …) | no | A block only produces a warning. `tool_response_transform` with `updated_tool_response:""` **erases the output**. |

**Also avoid:**
- `system_message`. It is surfaced as a user-visible Warning on every event.
- `suppress_output`. It has no effect.
- Stray stdout text. On context events it is injected into the model's context, and on `pre_compact` into the compaction prompt.
  - Watch out for a `$SHELL` rc file that prints to stdout: `zsh -c` sources `.zshenv`. Such output would be injected on context events, and on other events it causes a parse failure (ignored, with `on_error: ignore`).

---

## 6. MCP registration

- **There is no user-level or global MCP config.**
  - `config.yaml` has no MCP or toolset section (`pkg/userconfig/userconfig.go`).
  - `hooks.d` holds hooks only.
  - Reusable top-level `mcps:` entries live in the agent YAML and accept only `docker:` refs (`pkg/config/mcps.go:45-50`).
- MCP servers are per-agent **toolsets** in an agent YAML:

  ```yaml
  agents:
    root:
      toolsets:
        - type: mcp
          command: /ABS/subcortex        # absolute path, so no PATH lookup is needed
          args: ["mcp"]
          version: "false"               # opt out of aqua auto-install if the command is missing
          # optional: tools: [...], env: {...}, working_dir: ..., instruction: "..."
  ```

- Options:
  - Remote: `type: mcp` + `remote: {url, transport_type: streamable|sse, headers}`.
  - Global opt-out of auto-install: `DOCKER_AGENT_AUTO_INSTALL=false`.
- **Built-in agents cannot be extended.** `docker agent run` without a file uses the built-in `default` agent, and `coder` is also built in. Neither can take an extra MCP server without the user authoring their own agent YAML. That means editing a user-owned file, or shipping our own agent file and asking users to run it. `~/.agents/*.yaml` is only listed in `--agent-picker` (`cmd/root/agent_picker.go:45`); it is not a global include.
- ⇒ For Docker Agent, MCP is **documentation-only** (tell users which toolset to add). Do not auto-install it.

---

## 7. e2e recipe (non-interactive, custom OpenAI-compatible base URL)

**Mechanics**
- `docker-agent run --exec [--yolo] [--json] <agent.yaml> "<msg>" ["<msg2>" …]`
  - Each message is a separate RunStream.
  - `--json` prints every runtime event as NDJSON (`pkg/cli/runner.go:130-160`).
  - In `--json` mode a tool confirmation is auto-**rejected**, so **pass `--yolo`** (safety `autonomous`).
  - Without `--json`, confirmation is prompted on stdin.
- `--exec` sets a fixed title, so there is no title-generation LLM call.

**Custom provider**
- An agent-YAML `providers:` entry with `base_url` implies `bypass_models_gateway`.
- With `token_key`, the named env var must be set, or config fails with "<KEY> environment variable is required".
- Without `token_key`, a custom provider sends no auth (`pkg/model/provider/openai/client.go:92-107`).
- Force Chat Completions with `api_type: openai_chatcompletions`. Otherwise model names like gpt-5 or gpt-4.1 auto-select the Responses API.
- Requests are streamed: `client.Chat.Completions.NewStreaming`, with `stream_options.include_usage` when tracking usage (client.go:400-407, 565). The mock must serve SSE and should send a final `usage` chunk.
- The threshold trigger uses the session's reported usage.

**Forcing compaction**
- Set `provider_opts.context_size` on the model, so the context limit is known without models.dev (`session_compaction.go:326-354`), and a low `compaction_threshold` on the agent (0 < x ≤ 1).
- Make the mock report a large `prompt_tokens`, or make the shell tool print a lot (the tool-overflow estimator).
- The summary request also goes to the mock, so answer it with plain text.

**Relocation**
- Relocate **via `HOME` only**, so the hook's default `$HOME/.cagent/session.db` lookup matches the DB the agent actually writes.
- Avoid `--data-dir` and `--session-db` in B3 tests.

```sh
T=$(mktemp -d); mkdir -p "$T/home/.config/cagent/hooks.d"
cp 50-subcortex.yaml "$T/home/.config/cagent/hooks.d/"          # from §3, with /ABS resolved
cat > "$T/agent.yaml" <<EOF
providers:
  mock:
    base_url: http://127.0.0.1:${PORT}/v1
    token_key: MOCK_API_KEY
    api_type: openai_chatcompletions
models:
  m:
    provider: mock
    model: mock-model
    provider_opts:
      context_size: 8000
agents:
  root:
    model: m
    instruction: You are a test agent.
    compaction_threshold: 0.5
    toolsets:
      - type: shell
EOF
HOME="$T/home" MOCK_API_KEY=x TELEMETRY_ENABLED=false DOCKER_AGENT_HIDE_TELEMETRY_BANNER=1 \
DOCKER_AGENT_AUTO_INSTALL=false \
  docker-agent run --exec --yolo --json --working-dir "$T" "$T/agent.yaml" "run seq 1 5000"
```

**Assertions from the NDJSON**
- `{"type":"hook_finished","hook_event":"tool_response_transform","allowed":true,…}`. Also `hook_started`. Both are emitted per dispatch (`pkg/runtime/event.go:1058-1105`).
- `{"type":"tool_call_response","response":"<our head+marker+tail>",…}`. The response is post-transform.
- After compaction: a `hook_finished` for `before_compaction` and for `after_compaction`. The next mock request should contain our `turn_start` system message.
- In the first-prompt request: a system message carrying the B1 hint.

**Scripted mock sequence**
1. Tool call `shell` `{"cmd":"seq 1 5000"}` with a high `usage.prompt_tokens`.
2. The compaction summary request: answer with text.
3. Final answer.

Self-update is opt-in (`DOCKER_AGENT_AUTO_UPDATE`), so nothing needs to be disabled for it.

Alternative, untested: `--fake <cassette>` replays go-vcr cassettes (`cmd/root/run.go:186`). Upstream's own e2e tests use a recording proxy through `--models-gateway` (`e2e/helpers_test.go`).

---

## 8. Corrections to sweep.md §2.6

| sweep.md §2.6 claim | Verdict |
|---|---|
| Version v1.142.0 (2026-09-21) | ✔ |
| Drop-ins at `~/.config/cagent/hooks.d/*.yaml` | ✔. Also `*.yml`. Since v1.100.0. Strict parse, and an invalid file is skipped whole. The dir follows `--config-dir`, `DOCKER_AGENT_CONFIG_DIR` and `CAGENT_CONFIG_DIR`, **not** `XDG_CONFIG_HOME`. |
| Events and output are snake_case | ✔ |
| B1: `hook_specific_output.additional_context` | ✔. Also register `user_steering_messages_submit` and `user_followup_submit`, because the default `busy_send_mode: steer` routes busy-time messages there. |
| B2: `tool_response_transform` returns `updated_tool_response` | ✔, but it lives under `hook_specific_output` and is a string. It is also shown to the user and persisted. Use v1.137.0 or later only; before that it could bypass `redact_secrets` or be overridden by the limiter. |
| B3: `pre_compact`, and `before_compaction` can replace the summary | ✔, but no transcript is passed. `pre_compact` stdout is **injected into the summary prompt**, so use `before_compaction` with empty stdout. Get the messages from `session.db` or a rolling buffer. `tool_overflow` is never emitted. |
| B4: `session_start` is `startup` only; re-inject via `turn_start` after `after_compaction` | ✔. Note that `session_start` and `session_end` fire per **RunStream** (each user message), not per session. `turn_start` context lasts one model call, so repeat it. |
| A `pre_tool_use` failure blocks by default | ✔, and it ignores `on_error`. `tool_guard` is fail-closed too. |
| Avoid exit 2 | ✔. Also avoid `continue:false`, `decision:block`, invalid `decision` values, any `permission_decision`, `summary`, and `updated_tool_response:""`. |
| "Exact field names beyond those quoted are UNVERIFIED" | Now verified (§4). |
| (missing) | User-level hooks do **not** load under `serve acp/api/mcp/a2a/chat`. The command runs under `$SHELL -c`. The default timeout is 60 s. There is no global MCP config. |

---

## 9. Unverified or residual

1. Whether `--sandbox` (Docker sandbox) and `--remote` runs see hooks.d, and whether our absolute path resolves there. Either way they fail open.
2. Whether Docker Desktop's bundled plugin can run with a different config dir, for example if Desktop sets `DOCKER_AGENT_CONFIG_DIR`. The same binary and the same defaults are assumed.
3. `session.db` freshness at `before_compaction`. Rows are written by the persistence observer as events flow, so a race on the very last message is possible. Streaming assistant rows are updated in place.
4. `OPENAI_BASE_URL` with plain `openai/<model>`, without a YAML provider. The openai-go SDK default may honor it, but docker-agent's provider construction was not traced for this. Use the YAML `providers:` block.
5. The exact SSE and usage behavior the mock needs to trigger threshold compaction was reasoned from source and not run.
6. How the TUI renders `hook_started`/`hook_finished` for frequent `turn_start` hooks. It could be visual noise; this is cosmetic.
7. Behavior with non-POSIX `$SHELL` (nushell, xonsh, elvish) and on Windows (PowerShell does not understand `2>/dev/null || true`). Expected to fail open to a no-op under `on_error: ignore`, but not tested.
8. Harness agents (`harness:`, which delegate to the claude-code or codex CLIs): `turn_start` fires (`pkg/runtime/harness.go:49`), but tool hooks for the harness's internal tools do not. Not relevant to the default agents.
