# Kiro CLI (AWS, `kiro-cli`): subcortex hook adapter spec

**Target:** Kiro CLI **2.22.1**, the latest stable (checked 2026-09-21).
- The manifest at `https://prod.download.cli.kiro.dev/stable/latest/manifest.json` reports `"version":"2.22.1"`.
- The tarball's `BUILD-INFO` reads `BUILD_DATE=2026-09-18T01:55:30Z` and `BUILD_HASH=4ede1ca42f41b2c8557a6f2501d7f3063c74ead9`.
- 2.22.0 shipped on 2026-09-16.

**Method.** Kiro CLI is closed source, so I verified against the shipped binary, not just the docs:
- I downloaded `kirocli-aarch64-linux.tar.gz` for 2.22.1 into `scratchpad/src2/kiro/bin/` and ran `strings` on `kiro-cli-chat`.
- The V3 engine ships inside that binary as a gzip-compressed tar, `kas-bundle.tar`, at file offset 59741579. It holds `node_modules/@kiro/agent@0.66.4/dist/server/acp-server.js`, the Kiro Agent Server (KAS). I extracted it and read the hook code directly.
- For the V1 and V2 engines I also used the public ancestor, `aws/amazon-q-developer-cli` at `15cc8f3` (2026-04-23): `crates/chat-cli` is V1 and `crates/agent` is V2.
- Nothing was installed or run.

> **Bottom line**
> - **The default harness in 2.22.1 is V2, not V3.** V2 has **no standalone hook files**; hooks exist only inside agent JSON.
> - The clean drop-in `~/.kiro/hooks/*.json` works **only in V3**. V3 is opt-in through `--v3` or `chat.agentEngine=v3`.
> - In every engine only B1 (prompt-submit context) is native. B2 is impossible. B3 and B4 are possible only as a transcript-polling workaround.

---

## 1. Sources, versions, detection

**Sources**
- Binary strings, `kiro-cli-chat` 2.22.1:
  - The `--agent-engine` help text: `Agent engine to use: "v1", "v2" (default), or "v3"`.
  - The `chat.agentEngine` setting description: `Default agent engine: 'v1', 'v2' (default), or 'v3'`.
  - The `--v2` help: `Use the V2 agent engine (the pre-3.0 default)`.
  - The compiled rollout flag `v3_prompt`: *"There is no feature that flips the default engine to V3 during the ease-in — the prompt is the only path — so a user is never defaulted onto V3 without being asked."* It has `treatment_percent: 10` and `segment: "internal"`.
- KAS source, `@kiro/agent` 0.66.4, `dist/server/acp-server.js`. I dumped the hook module to `scratchpad/src2/kiro/kas_hooks_region.js`. Key functions (minified names):
  - `Hyr`: exit-code policy.
  - `qyr`: stdin payload.
  - `kDe.execute`: executor.
  - `DDe`: standalone loader.
  - `tde`/`l3n`: zod schema.
  - `GDe`: global dir.
  - `FQi`: the in-process provider used by the CLI.
  - `zDe.spawn`: process runner.
- Q CLI ancestor:
  - `crates/chat-cli/src/cli/agent/hook.rs` and `crates/chat-cli/src/cli/chat/cli/hooks.rs` (V1).
  - `crates/agent/src/agent/{mod.rs,task_executor/mod.rs,agent_config/definitions.rs}` (V2).
- Docs, fetched 2026-09-21:
  - https://kiro.dev/docs/hooks.md , /hooks/types.md , /hooks/actions.md
  - /cli/v3.md , /cli/v3/hooks-migration.md , /cli/2x-reference.md
  - /custom-agents/configuration-reference.md , /reference/cli-commands.md , /reference/settings.md
  - /cli/headless.md , /cli/chat/session-management.md , /mcp/configuration.md
  - Changelog pages 2-22, 2-21, 2-21-4 and 2-3.

**Three engines in one binary.** Select one with `--agent-engine v1|v2|v3`, `--v2`/`--v3`, or `kiro-cli settings chat.agentEngine v3`.

| Engine | Code | UI | Hooks come from | Default? |
|---|---|---|---|---|
| **V1** | Rust `crates/chat-cli` (Q CLI lineage) | classic UI (`--classic` / `--legacy-ui` / `chat.ui=classic`) | agent JSON `hooks` | no |
| **V2** | Rust `crates/agent` + `crates/chat-cli-v2`, over ACP to the JS TUI | TUI (default) | agent JSON `hooks` **only** | **yes (2.22.1)** |
| **V3** ("CLI 3.0", "KAS", unified harness shared with Kiro IDE/Web) | Node `@kiro/agent` spawned as `node --experimental-wasm-modules acp-server.js --transport=stdio --auth=acp-callback` | TUI | `.kiro/hooks/*.json` + `~/.kiro/hooks/*.json` + agent-profile hooks | opt-in |

- The docs page `/cli/v3.md` agrees: "An early release of Kiro CLI v3 is now available. Try it out with: `kiro-cli --v3` … your current setup is unchanged until you opt in."
- The earlier sweep's question of which harness is the default is **settled: V2**.

**Detection**
- Binaries:
  - `kiro-cli`, the launcher.
  - `kiro-cli-chat`, the chat engine.
  - `kiro-cli-term`.
  - Legacy shims `q` and `qchat`, which run `kiro-cli --show-legacy-warning …`.
- Version: `kiro-cli --version` (clap, `-V`). The expected output is `kiro-cli 2.22.1` (UNVERIFIED exact string; I did not run it).
- Engine in use: `kiro-cli settings chat.agentEngine`. Empty or unset means `v2`. This setting is stored in Kiro's settings DB/JSON, not in a file we own.
- Hook payload discriminators:
  - V1/V2 `hook_event_name` is camelCase (`userPromptSubmit`).
  - V3 is PascalCase (`UserPromptSubmit`).
  - V1 payloads have **no `session_id`**. V2 and V3 have it.

**Minimum versions**
- Hooks in agent JSON exist from Kiro 1.x (Q CLI lineage).
- `KIRO_HOME` arrived in **2.3.0** (2026-05-12).
- The V2/V3 split and `--v3` arrived in the 2.1x line. `--v2` arrived in **2.21.4** (2026-09-11).
- Recommended minimum: **2.21.4**.

---

## 2. Config paths, relocation, trust

| What | Path | Engine |
|---|---|---|
| Agent configs (hooks live here) | `$KIRO_HOME/agents/<name>.json` (default `~/.kiro/agents/`); workspace `<cwd>/.kiro/agents/<name>.json` (a workspace agent wins a name conflict) | V1, V2 (V3 also reads them as "agent-profile" hooks) |
| **Standalone hook files** | **`~/.kiro/hooks/*.json`** (global) and `<workspace>/.kiro/hooks/*.json` | **V3 only** |
| MCP | `~/.kiro/settings/mcp.json` (user), `<cwd>/.kiro/settings/mcp.json` (workspace) | all |
| Settings | `kiro-cli settings <key> [value]` (`chat.agentEngine`, `chat.defaultAgent`, `chat.ui`) | all |
| V2 transcripts | `$KIRO_HOME/sessions/cli/<session_id>.json` (metadata: `session_id`, `cwd`, `created_at`, `updated_at`) and `…/<session_id>.jsonl` (log lines `{"kind":"Prompt"\|"AssistantMessage"\|"ToolResults"\|"Compaction",…}`) | V2 |
| V3 transcripts | `$HOME/.kiro/sessions/<workspaceHash>/<session_id>/{session.json,messages.jsonl,sub-executions/*.jsonl,tool-outputs/*.txt}`. `messages.jsonl` holds lines `{id,timestamp,payload:{type:"user"\|"assistant"\|"tool_call"\|"tool_result"\|"turn_start"\|"turn_end"\|"session_event"\|…}}`. | V3 |

**Relocation**
- `KIRO_HOME` replaces `~/.kiro` for "global agents, prompts, skills, steering, settings, and sessions" (docs `/reference/settings.md`; TUI code `iCt(){return process.env.KIRO_HOME || join(HOME,".kiro")}`).
- **KAS (V3) does not read `KIRO_HOME`.** The string does not appear in `acp-server.js`. The global hooks dir is `path.join(homeDir,".kiro","hooks")`, where `homeDir = --home-dir=<arg> ?? os.homedir()`. The CLI launcher passes no `--home-dir` (spawn args: `--experimental-wasm-modules <server> --transport=stdio --auth=acp-callback [--endpoint=…]`).
- So under V3, relocate with **`HOME`**, not `KIRO_HOME`.
- Other environment variables: `KIRO_DATA_DIR`, `KIRO_API_KEY` (headless auth), `KIRO_NO_AUTO_UPDATE`, `KIRO_DISABLE_TELEMETRY`, `KIRO_KAS_SERVER_PATH`, `KIRO_KAS_NODE_PATH`, `KIRO_KAS_ENDPOINT`.

**Trust**
- V1/V2 have no trust gate for hooks. Agent files load as they are.
- V3 gates execution on `featureFlags.v2Hooks && workspaceTrusted` (`$vr`). If the gate fails, all triggers become no-ops (`oZa()`).
- However, the stdio server entry `Abu()` hard-codes `workspaceTrusted:!0`, and the CLI TUI advertises `hooks:{enabled:!0,v2:!0}` in `initialize` (`LLe()`). So **in CLI V3, global and workspace hook files always run.**
- ("v2Hooks" is KAS's internal name for the new standalone format. Do not confuse it with the V2 engine.)

**When changes take effect**
- V1/V2: agent files are read at session start and on `/agent swap`, so restart after installing.
- V3: KAS watches `.kiro/hooks` under home and under each workspace (`XE` file watcher, debounced `reloadAll()`), so dropping or removing a file **hot-reloads**.

**Hazard: the Kiro IDE.** The IDE (≥1.0), Kiro Web local sessions and CLI V3 are the same harness. Per the docs, "Global hooks in `~/.kiro/hooks/` … shipped … to all surfaces at once". **A file in `~/.kiro/hooks/` will also fire inside the Kiro IDE**, with the same KAS payload shape. The adapter must tolerate that, e.g. a different `cwd` and IDE tool IDs.

---

## 3. Registration snippets (files we own) and tagging

### 3a. V3: `~/.kiro/hooks/subcortex.json` (preferred; fully owned; create or delete)

Schema, from zod `l3n`/`tde`:

```
{version: literal "v1", hooks: [ {
    name: string ≥1 (required),
    description?: string,
    trigger: string,
    matcher?: string (JS RegExp),
    action: {type:"command", command: string ≥1} | {type:"agent", prompt: string ≥1},
    timeout?: int ≥0 (SECONDS; default 60; 0 = no timeout),
    enabled?: bool,
    confirm?: …
} ≥1 ]}
```

- `Jt` is `z.object`, not strict, so unknown keys are stripped rather than fatal.
- If the whole file fails validation, it is skipped with the warning "Hook file does not match v2 schema". Other files are unaffected.
- An unknown trigger drops only that hook.
- Trigger aliases are accepted through `SV`/`DMi`: `agentSpawn`→`SessionStart`, `userPromptSubmit`→`UserPromptSubmit`, `postToolUse`→`PostToolUse`, and more.
- Files load in sorted filename order. Only `*.json` files are read.

```json
{
  "version": "v1",
  "hooks": [
    {
      "name": "subcortex-UserPromptSubmit",
      "description": "managed-by: subcortex",
      "trigger": "UserPromptSubmit",
      "action": { "type": "command", "command": "'/ABS/subcortex-hook' kiro UserPromptSubmit 2>/dev/null || true" },
      "timeout": 10
    }
  ]
}
```

- **Tagging:** ownership is the file itself, `subcortex.json`. Additionally set `name` to the `subcortex-` prefix and `description` to `"managed-by: subcortex"`.
- **Registering other triggers:**
  - Do **not** register `Stop` (§5).
  - `PostToolUse` is useless (§4b).
  - `SessionStart` is optional: its output is folded into message 0 once per session.
- The command runs through Node `spawn(cmd,{shell:true})`, i.e. `/bin/sh -c` on POSIX.
- The environment is `process.env` plus `AWS_SDK_UA_APP_ID`. The working directory is the workspace root.
- The literal `${WORKSPACE_ROOT}` inside `command` is substituted, so avoid it.

### 3b. V2 (the default): our own agent file, opt-in only

V2 reads hooks **only** from the active agent's JSON. There is no global or standalone hook file.
- The built-in default agent `kiro_default` is compiled in.
- A file named `kiro_default.json` does not override it. In Q lineage the in-memory default is inserted last and wins (UNVERIFIED for Kiro).
- The only way to use a file we own is `$KIRO_HOME/agents/subcortex.json` (object-form hooks) plus one of:
  - the user launches with `kiro-cli --agent subcortex`;
  - `kiro-cli settings chat.defaultAgent subcortex`. This **edits the user's settings and replaces their chosen default agent**, so it must be opt-in and reverted with `kiro-cli settings --delete chat.defaultAgent`, restoring any previous value.

```json
{
  "name": "subcortex",
  "description": "kiro_default + subcortex hooks (managed-by: subcortex)",
  "tools": ["*"],
  "includeMcpJson": true,
  "resources": ["file://AGENTS.md", "file://README.md"],
  "hooks": {
    "userPromptSubmit": [
      { "command": "'/ABS/subcortex-hook' kiro userPromptSubmit 2>/dev/null || true", "timeout_ms": 10000, "max_output_size": 10240 }
    ]
  }
}
```

**V2 hook fields**
- Object form: `command`, `matcher` (a **glob** via `matches_any_pattern`, plus `*`, `@builtin` and `@server`), `timeout_ms`, `max_output_size`, `cache_ttl_seconds` (0 means no cache; agentSpawn is cached forever).
- The array form (the "KAS hook document", `WireHookDocument`: `name`, `trigger`, `matcher`, `action`, `timeout` in seconds, `maxOutputSize`, `cacheTtlSeconds`, `enabled`) is also accepted.
- `action.type:"agent"` is "Unsupported by the CLI runtime (skipped on load)".
- Commands run through `bash -c` with the JSON on stdin and `USER_PROMPT` in the environment (sanitized, first 4096 chars).

**Defaults, from the Q lineage and UNVERIFIED for 2.22.1:**

| Default | V1 | V2 |
|---|---|---|
| `timeout_ms` | 30 000 | 10 000 |
| `max_output_size` | 10 240 bytes; longer output is truncated and ends with `… truncated` | 10 240 bytes; longer output is truncated and ends with `… truncated` |

**Always set `timeout_ms` and `max_output_size` explicitly.**

A copy of `kiro_default` loses its built-in agent prompt and default resources. That costs some fidelity, so treat V2 support as "opt-in agent".

---

## 4. Per-event stdin and responses

The hook reads JSON on stdin and writes **plain text** on stdout. No engine parses JSON output for B1; JSON is parsed only for V3 `PreToolUse` `permissionDecision:"ask"` and for `Stop` `decision:"block"`.

### (a) Prompt submit → inject context: ✅ in all engines

- **V2 stdin** (docs samples plus `crates/agent` strings `hook_event_name session_id USER_PROMPT tool_input tool_response assistant_response`):

  ```json
  {"hook_event_name":"userPromptSubmit","cwd":"/repo","session_id":"<uuid>","prompt":"user text"}
  ```

  - The response is exit 0 with non-empty stdout. The text is appended to that user message as an extra text block (`send_prompt_impl` → `ContentBlock::Text(output)`).
  - Only exit-0 outputs are used (`per_prompt_hooks()` filters `is_success() && output.is_some()`).
- **V1:** the same payload **without `session_id`**.
  - Output is wrapped in `--- CONTEXT ENTRY BEGIN --- … This section … I have gathered this context from valuable programmatic script hooks … --- CONTEXT ENTRY END ---`.
  - It is attached as `additional_context` for that turn, "not stored in conversation history".
- **V3 stdin** (`qyr`):

  ```json
  {"session_id":"<chatSessionId>","hook_event_name":"UserPromptSubmit","cwd":"/repo","prompt":"user text (earlier <HOOK_INSTRUCTION> stripped)"}
  ```

  - The response is exit 0 with non-empty stdout. `FQi.executeHookAction` adds a **new human message** `<HOOK_INSTRUCTION>\n{stdout}\n</HOOK_INSTRUCTION>`, which becomes part of the conversation context and is persisted.
  - **V3 hazard:** if stdout is **empty**, or the exit code is non-zero, the same code path still adds a human message `Output:\n{stdout||stderr}\n\nExit Code: {n}`.
  - So in V3 **always print a short non-empty hint**. Otherwise every prompt gets an empty "Output: … Exit Code: 0" message.
  - The matcher is ignored for `UserPromptSubmit` (`Yyr` → `"none"`, with a warning), which contradicts the docs claim that it matches prompt text.
- Keep hints under about 3 KB. KAS truncates some hook outputs at 3000 chars (`dSr`, `jDi=3e3`), and V2 truncates at `max_output_size`.

### (b) Replace shell tool output: ✗ IMPOSSIBLE in every engine

| Engine | What PostToolUse output does | Source |
|---|---|---|
| V1 | Nothing. "Exit code is 0: nothing. stdout is not shown to user. We don't support processing the PostToolUse hook output yet." | `chat/mod.rs` |
| V2 | Nothing. `HookStage::PostToolUse` just forwards the unchanged `tool_results`. | `agent/mod.rs` |
| V3 | Nothing for command hooks. `Hyr` forwards neither stdout nor stderr for `PostToolUse`. Only `agent`-type hooks append text, as "PostToolUse hooks have additional instructions after "shell" completed … <HOOK_INSTRUCTION>". That is an append, not a replacement. | `Hyr`, `runPostToolUseHooks` |

- There is no `updatedToolOutput`-style field anywhere.
- For reference only, since registering it serves no purpose:
  - V2 `postToolUse` stdin adds `tool_name` (the shell tool is `execute_bash`, alias `shell`), `tool_input`, and `tool_response`.
    - Docs shape: `{"success":true,"result":[…]}`.
    - Q-lineage V2 shape: `{"items":[{"Text":"…"}]}`.
    - PostToolUse is sent only for successful executions.
  - V3 sends `tool_name:"shell"` (the tool id), `tool_input`, and `tool_response` as the result **message string**.

### (c) Pre-compaction snapshot: ✗ natively; ◐ workaround

- No engine has a pre- or post-compaction trigger. The V3 trigger enum is `SessionStart, Stop, PreToolUse, PostToolUse, PreTaskExec, PostTaskExec, UserPromptSubmit, PostFileCreate, PostFileSave, PostFileDelete, Manual`. V1/V2 have `agentSpawn, userPromptSubmit, preToolUse, postToolUse, stop`.
- No payload has `transcript_path`; the string is absent from both the binary and KAS.
- **Workaround:** on every `userPromptSubmit`, read the transcript by `session_id` and refresh a rolling snapshot of the last N messages. This is observe-only, so it can never veto anything.
  - V2: `$KIRO_HOME/sessions/cli/<session_id>.jsonl`, one JSON line per log entry, with `kind` among `Prompt`, `AssistantMessage`, `ToolResults` and `Compaction`.
  - V3: glob `$HOME/.kiro/sessions/*/<session_id>/messages.jsonl`, lines `{payload:{type:"user"|"assistant"|"tool_call"|"tool_result"|…}}`.
  - Parse tolerantly: inner field names are UNVERIFIED.

### (d) Post-compaction re-inject: ✗ natively; ◐ via the next prompt submit

- V2/2.x docs: compaction "creates a new session with compacted context; resume original via /chat resume". The `session_id` is therefore expected to **change** after compaction, and the new `.jsonl` should contain a `{"kind":"Compaction"}` entry (the kind string is confirmed in the TUI's session scanner).
- On `userPromptSubmit`, if `session_id` is new and its log starts with or contains `Compaction`, emit the saved snapshot. Look the snapshot up by the same `cwd`, from the most recent previous session in `sessions/cli/*.json` whose `cwd` matches.
- V3: the compaction marker in `messages.jsonl` is UNVERIFIED (no `compaction` payload type found). Detect by message-count drop or `session_event` category.
- `agentSpawn`/`SessionStart` is **not** a re-injection point:
  - V2 caches agentSpawn output forever and prepends it to every request. It is static, not per-compaction.
  - V3 folds SessionStart output once into message 0 (`NJa`, precomputed at session start).

---

## 5. Exit codes and every blocking response to avoid

The docs conflict. From source:

| Engine | Exit 0 | Exit 2 | Other non-zero / timeout / spawn error |
|---|---|---|---|
| V1 (`chat-cli`) | stdout used (agentSpawn, userPromptSubmit) | **blocks `preToolUse` only** ("PreToolHook blocked the tool execution: {stderr}") | warning `✗ … failed with exit code: N, stderr: …`; never blocks |
| V2 (`crates/agent`) | same | **blocks `preToolUse` only** (`has_failure_exit_code_for_tool`: `code == 2`) | output dropped; never blocks (timeout → `Err`, not exit 2) |
| V3 (KAS `Hyr`) | stdout forwarded only for `SessionStart` and `UserPromptSubmit` | **blocks `UserPromptSubmit`, `PreToolUse`, `PreTaskExec`** (stderr forwarded) | no block. **Except `Stop`:** exit **1** → `continue:true` (the agent keeps working, `FXa`) |

- **Answer to the docs conflict:** only **exit 2** blocks prompt submit, and only in V3. The actions-page sentence "any other exit code … the user prompt submission is blocked" is wrong for 2.22.1.
- A timeout is SIGTERM, then SIGKILL after 2 s, reported as exit code −1. It never blocks.

**Never emit:**
- Exit 2 on any event.
- Exit 1 on V3 `Stop`.
- `{"decision":"block"}` on `Stop`. It is honored in V3 (`LXa`, top level or inside `hookSpecificOutput`) and documented for the CLI.
- `{"hookSpecificOutput":{"permissionDecision":"ask"}}` on V3 `PreToolUse`. With no permission handler it becomes a **deny**: "Hook requested confirmation … no permission handler is available. Denying."
- A V3 hook with `"timeout": 0`, which disables the timeout.
- `Stop` hooks with a `confirm` block.
- Any `agent`-type action. It costs credits and re-prompts the model.

**What to register:** only `UserPromptSubmit`/`userPromptSubmit`, with `|| true` in the command. The shell wrapper then always exits 0, unless it is killed on timeout, which yields −1 and not 2.

---

## 6. MCP registration

- **File (V1/V2/V3):** `~/.kiro/settings/mcp.json`. Under V2 it moves with `KIRO_HOME` (`$KIRO_HOME/settings/mcp.json`). KAS watches `.kiro/settings/mcp.json` under `HOME` and hot-reloads.

  ```json
  {"mcpServers":{"subcortex":{"command":"/ABS/subcortex","args":["mcp"],"disabled":false}}}
  ```

  - Optional fields: `env`, `timeout`, `autoApprove`, `disabledTools`.
- **CLI:** `kiro-cli mcp add --name subcortex --scope global --command /ABS/subcortex --args mcp`. Remove with `kiro-cli mcp remove --name subcortex --scope global`.
- **Precedence:** agent `mcpServers` > workspace `mcp.json` > global `mcp.json`, merged by name. An agent entry with `disabled:true` suppresses the server.
- The built-in default agent includes the global `mcp.json`. A custom agent needs `"includeMcpJson": true`, as in our §3b file.
- `--require-mcp-startup` exits with code 3 if a server fails in non-interactive V3.

---

## 7. Unverified claims and open items

1. **`kiro-cli --version` output format.** I did not execute it.
2. **2.22.1 hook defaults.** The V1/V2 defaults for `timeout_ms` (30 000 / 10 000) and `max_output_size` (10 240) come from the April 2026 Q-CLI ancestor. So does the V2 `tool_response` shape. Set them explicitly.
3. **Whether 2.22.1's `crates/agent` still ignores non-zero exits on `userPromptSubmit`.** The ancestor does. The Kiro binary contains no "prompt blocked" string for V1/V2; the only block string is "PreToolHook blocked the tool execution".
4. **Whether V2 `stop` honors `{"decision":"block"}`.** The docs imply yes for the CLI. Moot, because we never register `stop`.
5. **Compaction markers.** V2 `session_id` rotation on compaction and the exact `Compaction` log-entry fields are unconfirmed, and no V3 compaction marker was found. The B3/B4 workaround needs an e2e check.
6. **`kiro_default.json` override.** Whether a user file named `kiro_default.json` overrides the built-in V2 default agent is Q-lineage behavior (it does not), unconfirmed for Kiro.
7. **`KIRO_HOME` under V3.** V3 appears to ignore `KIRO_HOME` for hooks, sessions and MCP (KAS uses `os.homedir()`). The Rust launcher could still adjust `HOME` for the KAS child; I found no evidence it does.
8. **End-to-end testing is not possible with a mock LLM.**
   - Headless needs `KIRO_API_KEY` (Pro-tier subscriptions) and runs `kiro-cli chat --no-interactive --trust-all-tools [--v3] "prompt"`.
   - `Q_MOCK_CHAT_RESPONSE`/`KIRO_MOCK_CHAT_RESPONSE` exist in the binary but are `cfg!(test)`-gated in the ancestor.
   - `KIRO_KAS_ENDPOINT` overrides the V3 model endpoint but speaks Kiro's runtime protocol, not the OpenAI API.
   - A real account is required for e2e.
9. **Kiro IDE global hooks.** The IDE also executing `~/.kiro/hooks/*.json` is taken from the docs ("Global hooks in `~/.kiro/hooks/` … all surfaces") plus the shared KAS code. I did not test it in the IDE.

### Corrections to sweep.md §2.7

- **Default harness:** V2 (it was marked UNVERIFIED).
- **V3 config:** `.kiro/hooks/*.json` is V3-only. V3 hooks load from both `~/.kiro/hooks/` and the workspace dir, and have **no** trust gate in the CLI.
- **Exit codes:**
  - Only exit 2 blocks.
  - Prompt-submit blocking happens only in V3.
  - New V3 hazard: `Stop` exit 1 means "continue".
- **V3 injection:** stdout goes into a *new* `<HOOK_INSTRUCTION>` human message. An empty stdout injects an "Output: … Exit Code" message.
- **Timeouts:** V3's default is 60 s, in `timeout` seconds. The 30 s / 10 s defaults belong to `timeout_ms` in V1 and V2.
- **B3/B4:** these are feasible only through the transcript-polling workaround described above.
