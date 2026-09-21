# Grok Build (xAI `grok`): subcortex hook adapter spec

**Target:** Grok Build **1.0.40**, the current `stable` and `alpha` channel as of 2026-09-21.

**How it was verified:**
- **Source:** `xai-org/grok-build` `main`, the monorepo sync of 2026-09-19 (GitHub commit `4247f66168`, `SOURCE_REV` `9bb727ccdff0a793ee73bcde4e2e09cbef6b5387`).
- **Released binary:** I downloaded `grok-1.0.40-macos-aarch64` into a scratch directory; nothing was installed. I ran it headless with `HOME` and `GROK_HOME` relocated, against a local mock of an OpenAI-compatible endpoint (§2.4).
- **What the e2e runs confirmed:** every claim marked **[e2e]** was observed on the 1.0.40 binary. That includes the hook payloads, B2 output replacement, B1 context being discarded, the PreCompact and PostCompact payloads, and the Claude-compat double execution.

All source paths below are relative to `crates/codegen/`.

> **Different product with the same binary name.** The community `grok-cli` (superagent-ai, npm `@vibe-kit/grok-cli`) also installs a `grok` binary and also uses `~/.grok/` (for `user-settings.json`). It has no hook engine. Tell the two apart by the `--version` output (§1).

---

## 1. Sources, version, detection

### Sources used

**Docs** (shipped in the repo, and extracted to `~/.grok/docs/user-guide/` on launch):
- `xai-grok-pager/docs/user-guide/10-hooks.md`
- `05-configuration.md`: the "Harness compatibility" section, lines 399–427
- `07-mcp-servers.md`
- `09-plugins.md`
- `11-custom-models.md`
- `14-headless-mode.md`
- `17-sessions.md`
- `26-config-reference.md`

**Hook engine** (`xai-grok-hooks/src/`):

| File | What it holds |
|---|---|
| `event.rs` | Event names and aliases, the envelope, payloads, caps |
| `config.rs` | JSON/TOML schema, timeouts, hook naming |
| `discovery.rs` | Directory scan and dedup |
| `matcher.rs` | Matcher semantics |
| `dispatcher.rs` | Per-gate dispatch |
| `runner/mod.rs` | Output JSON parsing |
| `runner/command.rs` | Process spawn, exit codes, timeout |

**Shell integration** (`xai-grok-shell/src/`):

| File | What it holds |
|---|---|
| `util/hooks.rs` | Hook source list, including the Claude and Cursor compat files |
| `session/acp_session_impl/hook_dispatch.rs` | Hook dispatch from the session |
| `session/acp_session_impl/post_tool_use_delivery.rs` | Validating and applying a replacement |
| `session/acp_session_impl/tool_calls.rs:1061-1095, 2950-2953` | Where the replacement lands in the model prompt |
| `session/acp_session_impl/turn.rs:904-911` | UserPromptSubmit dispatch |
| `session/compaction.rs:511-525, 932-935, 983-991, 1976-1983` | Transcript path, and PreCompact and PostCompact dispatch |
| `agent/mvp_agent/agent_ops.rs:5044` | The SessionStart source value |
| `claude_import.rs:610, 930-941` | `/import-claude` copying hooks |

**Other crates:**

| File | What it holds |
|---|---|
| `xai-grok-tools/src/types/output.rs:384-414, 570-610, 721` | The `BashOutput` and `ToolOutput` shape |
| `xai-grok-tools/src/implementations/grok_build/bash/mod.rs:367-400, 2032-2047` | How `output_for_prompt` is built |
| `xai-grok-tools/src/lib.rs:13` | `DEFAULT_TOOL_OUTPUT_CHARS = 20_000` |
| `xai-grok-tools/src/types/claude_alias.rs:52` | `Bash` → `run_terminal_command` |
| `xai-grok-agent/src/config.rs:119-123` | The shell tool renamed to `run_terminal_command` |
| `xai-grok-tools/src/types/compat.rs:163-168` | `GROK_CLAUDE_HOOKS_ENABLED` |
| `xai-dirs/src/lib.rs:49-72` | `GROK_HOME` |
| `xai-grok-pager-bin/src/main.rs:1990-1998` | `--version` output |
| `xai-grok-version/src/lib.rs` | Version constants |

**Installer:** `https://x.ai/cli/install.sh`. I read it but did not run it.

### Version

- `curl https://x.ai/cli/stable` returns `1.0.40`. `curl https://x.ai/cli/alpha` also returns `1.0.40`.
- Binary URL: `https://x.ai/cli/grok-<ver>-<os>-<arch>[.gz|.zst]`. `<os>` is `macos`, `linux` or `windows`. `<arch>` is `aarch64` or `x86_64`.
- In the source tree, crate `version` still reads `1.0.38`. That is expected: release builds inject `GROK_VERSION` (`xai-grok-version/src/lib.rs`, the `VERSION` const).
- The 1.0.40 binary contains the exact hook strings from this source, for example:
  - `updatedToolOutput does not match the tool's output shape`
  - `prompt hook JSON is invalid but exit 2 still blocks`
  - `toolResultTruncated`
  - `transcriptPath`
  - `GROK_CLAUDE_HOOKS_ENABLED`
- There are no GitHub releases.
- The public changelog at `https://x.ai/build/changelog` returned HTTP 403 to curl. The **minimum version** for `updatedToolOutput`, `PostCompact` and `transcriptPath` is therefore not known (§7). **Pin support to ≥ 1.0.40.**

### Detection

- **Binary:** `grok`, installed to `${GROK_BIN_DIR:-$HOME/.grok/bin}/grok`. The installer also creates an `agent` alias and symlinks both into `~/.local/bin` or `/usr/local/bin` when either is on `PATH`.
- **`grok --version` [e2e]:** prints `grok 1.0.40 (eb1a2256660d)\n`, meaning `grok <semver> (<12-hex commit>)`. When an update-channel pointer is cached it adds ` [stable]` or ` [alpha]` (`main.rs:1990`). Use the regex `^grok (\d+\.\d+\.\d+)(-\S+)? \([0-9a-f]{12}\)`.
- **Banner:** the interactive TUI prints `Grok Build (pager) - v…` to stderr. Do not parse it.
- **Home directory:** `$GROK_HOME`, else `$HOME/.grok` (§2).

---

## 2. Config paths, format, relocation, trust

### 2.1 Where hooks come from

All sources are merged; later sources do not override earlier ones. The shell's `discover_hooks` → `assemble_hooks` (`util/hooks.rs:140-175`) gathers them in this order:

| # | Source | Format | Trust gate | Hook name prefix |
|---|---|---|---|---|
| 1 | Config layers (each is a TOML file):<br>• `/etc/grok/requirements.toml`<br>• `/etc/grok/managed_config.toml`<br>• `$GROK_HOME/managed_config.toml`<br>• `$GROK_HOME/requirements.toml` (signed)<br>• `$GROK_HOME/config.toml` | TOML `[[hooks.<Event>]]`, structurally identical to the JSON form | none | `user:`, `managed:`, `requirements/…:` |
| 2 | **`$GROK_HOME/hooks/*.json`**: direct children, sorted by file name, not recursive | JSON (Claude-compatible) | **none, always trusted** | `global/<file-stem>:` |
| 3 | Absolute paths listed one per line in `$GROK_HOME/hooks-paths` (the `/hooks-add` registry) | JSON file or directory | none | `global/…` |
| 4 | `~/.claude/settings.json`, then `~/.claude/settings.local.json`. Only when `compat.claude.hooks` is on, which is the default, and no `/import-claude` marker is set. | JSON | none | `global/settings:` |
| 5 | `~/.cursor/hooks.json`, when `compat.cursor.hooks` is on (the default) | JSON | none | `global/hooks:` |
| 6 | These project-level files, each under `<git-root>/`:<br>• `.claude/settings.json` and `.claude/settings.local.json`<br>• `.grok/hooks/*.json`<br>• `.cursor/hooks.json` | JSON | **folder trust** (`~/.grok/trusted_folders.toml`) | `project/…` |
| 7 | Plugin `hooks/hooks.json` | JSON | the plugin must be trusted and enabled | `plugin/…` |

Notes on the table:
- **Rows 2 and 3** come from `xai-grok-config/src/global_hook_sources.rs:455-530` and are loaded by `discovery.rs:301-365`.
- **Files row 2 picks up** (`is_direct_hook_json_name`, `global_hook_sources.rs:542-553`): the name must end in `.json`. It must not start with `.`, and must not end in `~`, `.swp` or `.swo`.
- **Codex:** there is no `.codex` hook discovery. The `compat.codex.hooks` cell is inert (`05-configuration.md:425`).

**Dedup and order:**
- Specs are deduplicated on `(canonical event, command_raw, url_raw, matcher)` (`discovery.rs:213-258`). An identical copy from a higher-authority layer wins.
- Within one event, handlers run in the order of this table: config layers first, then `$GROK_HOME/hooks`, then Claude and Cursor, then project, then plugins.

### 2.2 The file we own

**`$GROK_HOME/hooks/subcortex.json`**, which is `~/.grok/hooks/subcortex.json` by default.

- It is a separate file, so installing and uninstalling only creates or deletes it. Nothing of the user's is edited.
- **Relocation:** `GROK_HOME`, when non-empty, relocates everything Grok owns: config, hooks, sessions, auth, plugins and logs (`xai-dirs/src/lib.rs:49-72`). The Claude and Cursor compat paths use the OS home (`$HOME`) instead.
- **Must be a regular file.** Under a sandbox profile, the global hook sources are write-protected. Symlinked or hard-linked hook JSON is rejected by the sandbox path (`global_hook_sources.rs:60-90`). Normal discovery tolerates symlinks (`reject_symlinks=false`, `util/hooks.rs:66`), but write a plain file anyway.
- **One malformed event kills the whole file.** A malformed event group in a JSON hook file, for example `hooks` that is not an array or a non-integer `timeout`, fails the entire file (`HooksMap::from_value` with `GroupErrorPolicy::Fail`, `config.rs:69-79`). The error is logged and that file contributes no hooks. Because the file is ours, this only ever affects us.
- **Unknown event keys** are skipped with a warning.
- **Unknown top-level keys** are ignored, because only `hooks` is read (`config.rs:466-485`).
- **Unknown handler and group keys** are ignored, because serde does not deny unknown fields.
- **When changes take effect:** hooks are loaded at session spawn (`session/acp_session_impl/spawn.rs:1568`). They are reloaded only by:
  - pressing `r` in the `/hooks` modal;
  - a folder-trust grant;
  - a plugin reload (`hooks_plugins.rs:726-745`).

  There is **no file watcher**, so running sessions need `/hooks` → `r` or a new session.
- **Per-hook disable:** the user can press Space in `/hooks`. That writes the hook's name to `$GROK_HOME/disabled-hooks` (`trust.rs`). Names are positional (§3), so **keep the group order stable** across upgrades.
- **Managed-only pin:** `allow_managed_hooks_only` (or `allowManagedHooksOnly`) in any policy layer skips every `~/.grok/hooks` file at dispatch. We cannot override it.

### 2.3 Trust

- `$GROK_HOME/hooks/*.json` is **always trusted**, with no prompt and no enable flag.
- Project `.grok/hooks/` needs `/hooks-trust`, `--trust`, or `GROK_FOLDER_TRUST=0`.
- Headless `-p` loads global hooks the same way the TUI does [e2e].

### 2.4 Isolated e2e environment (verified against 1.0.40)

The runs used a mock that implements `GET /v1/models` and `POST /v1/chat/completions` with SSE (`stream:true` is what Grok sends).

```sh
T=$(mktemp -d)
mkdir -p $T/home $T/grok/hooks $T/work
cat > $T/grok/config.toml <<EOF
[model.mock]
model = "mock"
base_url = "http://127.0.0.1:18765/v1"
api_key = "x"
api_backend = "chat_completions"
context_window = 200000
[models]
default = "mock"
EOF
# write $T/grok/hooks/subcortex.json (see §3)
HOME=$T/home GROK_HOME=$T/grok GROK_DISABLE_AUTOUPDATER=1 XAI_API_KEY=dummy \
  grok -p "run seq" -m mock --yolo --no-auto-update --output-format json --cwd $T/work
```

**What happens on each run:**
- The process exits 0 and prints `{"text":…,"sessionId":…}`.
- Grok also sends `GET /` and a third "dashboard line" completion after the turn. The mock must answer both.
- The tool name the model sees is `run_terminal_command`.

**How to force compaction** [e2e]:
- Run `grok -p "/compact" -r <sessionId> …`. Slash commands work in `-p`. This fires `PreCompact` (`source:"manual"`), then `PostCompact`, and SessionStart has `source:"load"`.
- The mock's reply to the summarization request must not be tiny. A 9-character reply produced `compact_failed: degenerate summary` with exit 1, and PostCompact did not fire.
- Auto-compaction triggers at `[session] auto_compact_threshold_percent` (default 85) of `context_window`, or `model.<id>.compaction_at_tokens`.

**Debugging:**
- `RUST_LOG=debug GROK_LOG_FILE=$T/grok.log`
- `GROK_HOOK_DEBUG=1` traces the stdin and stdout byte counts.

**Mock server and helper scripts** used for this spec: `scratchpad/src2/grok-e2e/{mock.py,hook.py,run.sh,show.py}`.

---

## 3. Registration snippet (file we own) and tagging

**`$GROK_HOME/hooks/subcortex.json`:**

```json
{
  "_subcortex": {"managed": true, "schema": 1},
  "hooks": {
    "UserPromptSubmit": [
      {"hooks": [{"type": "command", "command": "'/ABS/subcortex-hook' grok-build UserPromptSubmit 2>/dev/null || true", "timeout": 5}]}
    ],
    "PostToolUse": [
      {"matcher": "Bash|bash", "hooks": [{"type": "command", "command": "'/ABS/subcortex-hook' grok-build PostToolUse 2>/dev/null || true", "timeout": 10}]}
    ],
    "PreCompact": [
      {"hooks": [{"type": "command", "command": "'/ABS/subcortex-hook' grok-build PreCompact 2>/dev/null || true", "timeout": 10}]}
    ],
    "PostCompact": [
      {"hooks": [{"type": "command", "command": "'/ABS/subcortex-hook' grok-build PostCompact 2>/dev/null || true", "timeout": 5}]}
    ]
  }
}
```

### Field rules

**Handler keys:** `type` (`"command"` or `"http"`), `command`, `url`, `timeout`, `env` (`config.rs:100-112`).

**`timeout`:**
- It is **integer seconds** (`u64`). A float makes the event fail to parse, which drops the whole file.
- `0` or absent means the per-event default (`config.rs:122-179, 611-625`):

  | Event | Default |
  |---|---|
  | observe events | 5 s |
  | `UserPromptSubmit` | 30 s |
  | `PreToolUse` | 5 s |
  | `PostToolUse`, `Stop`, `SubagentStop` | **600 s** |
  | `SessionEnd` | 1.5 s (`GROK_SESSION_END_HOOKS_TIMEOUT_MS`, max 60 s) |

- **Always set it explicitly.**

**How the command runs:**
- A command containing a space, `|&;<>$`, or a leading `~` runs as `sh -c "<command>"` (`runner/command.rs:141-172`). Ours contains spaces, so the quoting and the `|| true` work.
- Keep `$` out of the command. `${VAR}` is expanded at load time, and an unresolved `$VAR` makes the hook fail with "required env var(s) not set" (`command.rs:150-166`).
- Working directory: the workspace root (`command.rs:218`).
- Env always set (reserved, so the hook `env` map cannot spoof them): `GROK_HOOK_EVENT` (snake_case event), `GROK_HOOK_NAME`, `GROK_SESSION_ID`, `GROK_WORKSPACE_ROOT`, and `CLAUDE_PROJECT_DIR`, which has the same value as `GROK_WORKSPACE_ROOT` (`command.rs:219-224`) [e2e].
- On Linux with a network-restricting sandbox profile, the hook child gets a seccomp network filter (`xai-grok-sandbox/src/child_net.rs:269`). Loopback TCP to a daemon may fail there. See §7.

**Event keys:** PascalCase, snake_case and camelCase spellings, and Cursor aliases, are all accepted (`event.rs:78-180`). Use PascalCase.

**Matcher** (`matcher.rs:5-90`):
- **Simple form** (only `[A-Za-z0-9_|]`): an **exact** match against each `|`-term, plus the Grok names that term aliases. So `"Bash"` matches exactly `Bash` and `run_terminal_command` (`claude_alias.rs:52`).
- **Anything else** is an unanchored regex. It is tested against the Grok name and the name's Claude aliases.
- `""` or `"*"` matches everything.
- **An invalid regex** turns into a matcher that never matches, and the load records an error.

**What each event's matcher tests:**

| Event | Matcher |
|---|---|
| `UserPromptSubmit`, `Stop` | **Ignored with a warning.** Do not set one. |
| `PreCompact`, `PostCompact` | The trigger: `manual` or `auto` |
| `SessionStart` | The source: **`new` or `load`** [e2e] |

The `SessionStart` values in the docs (`startup`, `resume`) are **wrong** (`agent_ops.rs:5044`).

**Shell tool names:**
- The default `grok-build` toolset, `codex` and `grok-computer` all use `run_terminal_command` (`xai-grok-agent/src/config.rs:119-123, 341-357`).
- The built-in `opencode` agent profile (`config.rs:523-537, 1505-1512`) names its shell tool **`bash`** (`xai-grok-tools/src/implementations/opencode/bash/mod.rs:315`).
  - It still returns `ToolOutput::Bash`.
  - Its `output_for_prompt` has **no** `exit: N` header line (`opencode/bash/mod.rs:444-458`).
- Hence the matcher `"Bash|bash"`: simple form, exact names `Bash`, `run_terminal_command` and `bash`.
- The `--tools` and `--disallowed-tools` flags use the internal ID `run_terminal_cmd`, but hooks never see that ID.

**If the B1/B4 workaround is enabled (§4):**
- Drop the `matcher` so PostToolUse fires for every tool, and branch on `toolName` in the adapter.
- **Do not** add a second PostToolUse group with the same command and a different matcher. The dedup key includes the matcher, so both groups would run on shell calls.

### Tagging

- **File name:** `subcortex.json`. Every hook in it is named `global/subcortex:<snake_event>[<group>].hooks[<i>]`, for example `global/subcortex:post_tool_use[0].hooks[0]` [e2e] (`config.rs:533,544`; `discovery.rs:176-180`).
- The same name is shown in `/hooks`, in `GROK_HOOK_NAME`, and in the model-visible `additionalContext` header (§4b).
- **The `_subcortex` top-level key** is ignored by Grok. Use it as an ownership marker.
- **Uninstall:** delete the file. Running sessions keep the hooks they already loaded until they reload them.

### Alternatives (not recommended)

- **TOML in the user's `config.toml`:** `[[hooks.PostToolUse]] matcher="Bash" hooks=[{type="command",command="…",timeout=10}]`. This edits the user's file.
- **A plugin under `~/.grok/plugins/subcortex/`** with `hooks/hooks.json` and `.mcp.json`. It is auto-trusted, but it still needs `[plugins].enabled` in `config.toml` (`09-plugins.md:14, 270, 419-424`).

---

## 4. Per-event stdin and responses

### Common envelope

The envelope is built in `event.rs:355-406`. Keys are camelCase and authoritative. Snake_case aliases are appended, and `hook_event_name` carries Claude's PascalCase value:

```json
{
  "hookEventName": "post_tool_use",
  "sessionId": "01a0c5b5-…",
  "cwd": "/abs/cwd",
  "workspaceRoot": "/abs/root",
  "timestamp": "2026-09-21T20:43:39.709048+00:00",
  "transcriptPath": "…/sessions/<urlenc-cwd>/<sid>/updates.jsonl",
  "promptId": "…",
  "permissionMode": "default|auto|plan|bypassPermissions",
  "…event fields…": "…",
  "hook_event_name": "PostToolUse",
  "session_id": "…",
  "transcript_path": "…",
  "permission_mode": "…",
  "tool_name": "…", "tool_input": {}, "tool_response": {}, "tool_use_id": "…", "duration_ms": 22
}
```

**When optional fields appear:**
- `transcriptPath` appears only if `updates.jsonl` already exists (`compaction.rs:518-525`). It is absent on the first UserPromptSubmit of a new session [e2e].
- `promptId` is present on UserPromptSubmit and Stop. It is **absent on PostToolUse, PreCompact, PostCompact and SessionStart** [e2e].
- `subagentType` appears inside subagent sessions.

**Payload caps:**
- `toolInput` and `toolResult` are each replaced by a string (the first 128 KiB of their JSON, plus ` [truncated]`) when their serialized form exceeds 128 KiB. `toolInputTruncated` and `toolResultTruncated` then become `true` (`event.rs:3, 615-632`).
- Stdout capture is capped at 1 MiB (`command.rs:33-35`).

### (a) Prompt submit: UserPromptSubmit. Context injection is IMPOSSIBLE.

**stdin [e2e]:**

```json
{"hookEventName":"user_prompt_submit","sessionId":"…","cwd":"…","workspaceRoot":"…","timestamp":"…","promptId":"7b73…","permissionMode":"bypassPermissions","prompt":"please run seq","hook_event_name":"UserPromptSubmit","session_id":"…","permission_mode":"…"}
```

**Why injection cannot work:**
- The prompt gate returns only the decision. `dispatch_prompt_gate` (`dispatcher.rs:353-374`) discards `SequentialGateOutcome.additional_context`.
- `parse_prompt_result` parses only `{decision, reason}` (`runner/mod.rs:370-394`; `command.rs:851-915`).
- The docs say so too: "stdout of an allowing hook is discarded (no `additionalContext`)" (`10-hooks.md:113, 481`).
- **[e2e]:** a hook that returned `hookSpecificOutput.additionalContext` produced text that never reached any model request.

**Response:** empty stdout, exit 0.
- Allowed but pointless: `{}` or `{"decision":"approve"}`.
- `systemMessage` only shows a UI annotation line, and the model never sees it. Do not use it.

**Workaround (optional, verified channel).** Record the hint here, keyed by `sessionId`. Deliver it as `additionalContext` from the next PostToolUse (b):
- It reaches the model only if the model calls a tool in that turn.
- PostToolUse has no `promptId`, so reset a "delivered" flag on every UserPromptSubmit.

### (b) After a shell tool: PostToolUse. Genuine replacement is POSSIBLE.

**When it fires:**
- For every tool call that ran, including a non-zero shell exit.
- Tool dispatch failures and MCP error results fire `PostToolUseFailure` instead, which is context-only (`hook_dispatch.rs:370-430`).
- It is awaited before the tool result is committed.

**stdin [e2e]** (`toolResult` abridged; `tool_response` is a verbatim copy of it):

```json
{"hookEventName":"post_tool_use","sessionId":"…","cwd":"…","workspaceRoot":"…","timestamp":"…",
 "transcriptPath":"…/updates.jsonl","permissionMode":"bypassPermissions",
 "toolName":"run_terminal_command","toolUseId":"call_1",
 "toolInput":{"command":"seq 1 2000","description":"print numbers"},
 "toolResult":{"type":"Bash","output":[49,10,50,10,…],"output_for_prompt":"exit: 0\n1\n2\n…\n2000\n",
   "exit_code":0,"command":"seq 1 2000","truncated":false,"signal":null,"timed_out":false,
   "description":"print numbers","current_dir":"/abs/cwd","output_file":"…/<sid>/terminal/call_1.log","total_bytes":8893},
 "toolInputTruncated":false,"toolResultTruncated":false,"durationMs":22,"isBackgrounded":false,
 "hook_event_name":"PostToolUse","session_id":"…","transcript_path":"…","permission_mode":"…",
 "tool_name":"run_terminal_command","tool_input":{…},"tool_use_id":"call_1","tool_response":{…same as toolResult…},"duration_ms":22}
```

**Notes on `toolResult`:**
- `output` is the raw bytes as a **JSON array of integers**, because `Vec<u8>` has no serde attribute.
- `output_for_prompt` is **exactly what the model sees**: `ToolOutput::Bash(b) => b.output_for_prompt.clone()` (`output.rs:721`).
- Its format is `exit: <code>[ annotations]\n<ANSI-stripped, soft-wrapped output>`, built by `format_default_prompt` (`bash/mod.rs:367-400`).
- An annotation such as `[truncated: showing first/last 19.6 KB of 195.3 KB - full output at: <file>]` appears when Grok already cut the output. The in-memory limit is 20,000 chars, head and tail halves (`lib.rs:13`; `terminal.rs:387-412`).
- A command auto-moved to the background has `signal:"backgrounded"`, and its `output_for_prompt` is a multi-line `[Command moved to background]…` block.
- Background launches arrive as `{"type":"BackgroundTaskStarted",…}`.

**Response (single JSON object on stdout, exit 0):**

```json
{"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": <toolResult with output_for_prompt replaced and "output": []>}}
```

**How Grok validates it** (`post_tool_use_delivery.rs:129-178`):
1. `serde_json::from_value::<ToolOutput>(value)` must succeed.
2. The value must be the **same enum variant** as the real output (`std::mem::discriminant`).
3. The model then sees `candidate.to_prompt_format()`, which for Bash is the new `output_for_prompt`, clipped at 64 Ki chars (`event.rs:248`). It is escaped for reminder tags and substituted into the tool message (`tool_calls.rs:2950-2953`).

**The shape (Rust):**

```rust
#[derive(Serialize, Deserialize)] #[serde(tag = "type")]
pub enum ToolOutput { Bash(BashOutput), BackgroundTaskStarted(..), … }   // output.rs:570
pub struct BashOutput {                                                    // output.rs:384
    pub output: Vec<u8>,
    #[serde(default)] pub output_for_prompt: String,
    pub exit_code: i32, pub command: String, pub truncated: bool,
    pub signal: Option<String>, pub timed_out: bool, pub description: Option<String>,
    pub current_dir: String, pub output_file: String, pub total_bytes: usize,
    #[serde(skip_serializing_if = "Option::is_none", default)] pub output_delta: Option<Vec<u8>>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")] pub was_bare_echo: bool,
}
```

**Required keys:** `type`, `output`, `exit_code`, `command`, `truncated`, `timed_out`, `current_dir`, `output_file`, `total_bytes`.

**The example in the docs does not work.**
- `10-hooks.md:325` shows `{"type":"Bash","command":…,"exit_code":0,"output_for_prompt":…}`.
- **[e2e]:** 1.0.40 **rejected** that shape (missing fields). The original output was kept, and the run was recorded as `Failed`.
- **[e2e]:** the minimal fixture shape from `post_tool_use_delivery_tests.rs:48-60` was **accepted**:
  ```json
  {"type":"Bash","output":[],"output_for_prompt":"…","exit_code":0,"command":"…","truncated":false,"timed_out":false,"current_dir":"/","output_file":"","total_bytes":0}
  ```

**Recommended algorithm:**
1. If `toolResultTruncated` is true, or `toolResult` is not an object with `type == "Bash"`, or `signal == "backgrounded"`, then print nothing. That is the fail-open path.
2. Otherwise copy `toolResult` and build the new `output_for_prompt`:
   - If the text starts with `exit: `, keep line 1 (the header) verbatim and replace the rest with head + marker + tail. This is the grok-build and codex case.
   - If it does not, treat the whole text as the body. This is the `opencode` profile's `bash`.

   Set `output` to `[]`, since it only bloats stdout and is unused for rendering.
3. Emit it. Keep the result under 64 Ki chars.
4. **[e2e]:** the model received `exit: 0\n1\n2\n3\n[SUBCORTEX elided 1995 lines]\n1999\n2000\n`.

**Truncated payloads are rare.** `toolResult` exceeds 128 KiB only when the output array serializes to more than about 4 chars per byte, which means mostly non-ASCII output. A 195 KB ASCII output that Grok had already cut to 19.6 KB still arrived untruncated [e2e]. If the payload is truncated, do nothing.

**Record versus model:**
- The replacement changes only the model's copy. `chat_history.jsonl` stores the replaced text, while `updates.jsonl`, the scrollback and telemetry keep the original [e2e].
- Images are dropped under a replacement.

**Last writer wins:**
- If two hooks both replace a built-in tool's output, the last one wins (`dispatcher.rs:635-653`).
- Our `$GROK_HOME/hooks` file runs **before** the Claude-compat `~/.claude/settings.json` hooks. A Claude adapter that also returned a replacement would override ours (see §5, collision).

**Extra channel [e2e]:** `hookSpecificOutput.additionalContext` (a string, clipped at 10,000 chars) is delivered after the tool result as a user-role message:
```
<system-reminder>
Context from PostToolUse hook 'global/subcortex:post_tool_use[0].hooks[0]':
<text>
</system-reminder>
```
This is the only model-visible injection point available to command hooks. Use it for the B1 and B4 workarounds.

**Not usable:**
- `updatedMCPToolOutput` applies to MCP tools only. On a built-in tool it is downgraded to `Failed` and ignored.
- `decision:"block"` + `reason` does not block anything; it feeds `reason` to the model. **Never emit it** (§5).

**Payload for an MCP tool:** `toolName` is the qualified `server__tool` name, and `updatedToolOutput` passes through unchecked: a string becomes the model text verbatim.

### (c) Pre-compaction: PreCompact. Observe only. Possible.

**When it fires:**
- It fires at the start of `run_compact_inner` and is **awaited** before compaction proceeds, up to `timeout` (`compaction.rs:983-991`).
- It fires for manual `/compact` (`source:"manual"`) and auto-compaction (`source:"auto"`) (`compaction.rs:932-935`).

**stdin [e2e]:**

```json
{"hookEventName":"pre_compact","sessionId":"01a0c5bb-…","cwd":"…","workspaceRoot":"…","timestamp":"…",
 "transcriptPath":"…/sessions/%2F…%2Fwork/01a0c5bb-…/updates.jsonl","permissionMode":"…","source":"manual",
 "hook_event_name":"PreCompact","session_id":"…","transcript_path":"…","permission_mode":"…"}
```

**Response:**
- Stdout and exit code are ignored; it is an observe gate (`dispatcher.rs:868-940`; `command.rs:331-337`).
- A non-zero exit only prints `pre_compact hook (…) failed, ignored: …` in the scrollback.
- Emit nothing. **It cannot veto.**

**Transcript access:**
- The session directory is `dirname(transcriptPath)`. Fallback: `$GROK_HOME/sessions/*/<sessionId>/`. The group directory is the URL-encoded cwd, or a slug plus hash with a `.cwd` file when the name exceeds 255 bytes (`17-sessions.md:25-42`).
- **Prefer `chat_history.jsonl`**, which holds the model's view (including our replacements). At PreCompact time it still holds the pre-compaction history. It is **rewritten** after compaction [e2e].
- Each line is a `ConversationItem`, tagged by `type` (`xai-grok-sampling-types/src/conversation.rs:69-300`):
  - `{"type":"user","content":[{"type":"text","text":"<user_query>\nplease run seq\n</user_query>"}]}`. The `synthetic_reason` key is absent for real human prompts. It is set to `system_reminder`, `compaction_meta`, `project_instructions` or similar for injected items. Strip the `<user_query>` wrapper [e2e].
  - `{"type":"assistant","content":"…","tool_calls":[…]}`
  - `{"type":"tool_result","tool_call_id":"call_1","content":"…"}`
  - `{"type":"system",…}`
  - `{"type":"reasoning",…}`
  - `{"type":"backend_tool_call",…}`
- **`updates.jsonl`** is the append-only ACP stream. Each line is `{"timestamp":<unix>,"method":"session/update"|"_x.ai/session/update","params":{"sessionId":…,"update":{"sessionUpdate":"user_message_chunk"|"agent_message_chunk"|"tool_call"|"tool_call_update"|…,"content":{"type":"text","text":…}}}}`. Agent text arrives as chunks that must be concatenated. It keeps *original* tool output. Use it only as a fallback.

### (d) Post-compaction re-inject: PostCompact. IMPOSSIBLE natively.

- **PostCompact exists** (`compaction.rs:1976-1983`; `event.rs:170-174`). It fires after compaction finishes. It is observe-only, and its stdout is ignored.
- **stdin [e2e]:** the same fields as PreCompact, with `"hookEventName":"post_compact"`, `"hook_event_name":"PostCompact"` and `"source":"manual"|"auto"`.
- **[e2e]:** `additionalContext` returned from PostCompact, PreCompact or SessionStart never appeared in any later model request.
- **SessionStart does not help.** It is observe-only and dispatched asynchronously, not awaited. Its `source` is only `"new"` or `"load"`; it has no `compact` source and does not fire on compaction (`agent_ops.rs:5044`; `run_loop.rs:1901-1950`).
- **Workaround:**
  1. PostCompact sets a "reinject pending" marker for `sessionId`.
  2. The next PostToolUse returns the snapshot as `additionalContext`, capped at 10,000 chars.
  3. For this to fire on the model's first tool call of any kind, register PostToolUse with no `matcher`.

  After compaction Grok itself injects `This session is being continued from a previous conversation…` as a `compaction_meta` user item [e2e].

---

## 5. Exit codes and every blocking response to avoid

Sources: `runner/command.rs:331-358, 685-969`, `runner/mod.rs:226-239, 288-339, 378-543`, and `10-hooks.md:346-354`.

**Our wrapper:**
- `2>/dev/null || true` forces **exit 0 and empty stderr** in every case, so every exit-code path below is unreachable for us. Only **stdout** can do harm.
- **Side effect:** Grok's safety net ("a non-zero exit drops `updatedToolOutput` and `additionalContext`") no longer applies. **[e2e]:** a hook that printed a replacement and then `exit 1` behind `|| true` still had its replacement applied.
- The adapter must therefore write **one complete JSON document at the very end**, or nothing. No partial or streamed prints.

| Event (gate kind) | Blocks or changes control flow when… | Fail-open cases |
|---|---|---|
| `UserPromptSubmit` (Prompt) | Stdout `{"decision":"block"}` **on any exit code**: rejects the prompt. **Exit 2**: blocks even with invalid JSON, stderr becomes the reason. Enforced only for a human prompt in a top-level session. | exit 0; other non-zero; timeout (30 s default); crash; unknown `decision` value (recorded as failed). |
| `PreToolUse` (Tool). **Do not register.** | `decision` or `hookSpecificOutput.permissionDecision` equal to `deny` or `block`, **on any exit code**. Exit 2 (even alongside JSON `allow`). Unknown decision **plus** exit 2. `ask` forces a permission prompt. An `updatedInput` that fails the tool schema blocks the call. | Other exits, timeout (5 s). |
| `Stop` / `SubagentStop` (Stop). **Do not register.** | `{"decision":"block"}` or `hookSpecificOutput.additionalContext` keeps the agent working (up to 8 continuations). `{"continue":false}` force-ends the turn. Exit 2 without JSON blocks. | exit 0 with no or non-JSON stdout. |
| `PostToolUse` (PostTool) | Never blocks. But `decision:"block"` feeds `reason` to the model, and exit 2 feeds stderr to the model. **Emit neither.** An unknown `decision` is recorded as failed. | non-zero exit other than 2 drops `additionalContext` and the replacement; timeout (600 s default) contributes nothing. |
| `PostToolUseFailure` | Context-only: `additionalContext` is honored, and block and replacement are dropped. | as above |
| Observe events: `SessionStart`, `SessionEnd`, `PreCompact`, `PostCompact`, `Notification`, `SubagentStart`, `StopFailure`, `StopCancelled`, `PermissionDenied` | Nothing. | Any non-zero exit only prints a `… failed, ignored: …` line. |

**Never emit** any `decision` key, `permissionDecision`, `continue:false`, `updatedInput`, or `systemMessage`.
- `systemMessage` only produces a UI annotation line, clipped to 256 chars.
- The **only** keys subcortex writes are `hookSpecificOutput.updatedToolOutput` and `hookSpecificOutput.additionalContext`, and only on PostToolUse.
- Empty stdout is always safe.
- Plain non-JSON text on stdout is ignored for PostToolUse, Prompt and observe events.

### Collision with Claude Code hooks (not ours to fix)

- **The double run [e2e]:** Grok runs `~/.claude/settings.json` and `settings.local.json` hooks by default, as `global/settings:<event>…`, **after** ours. A `subcortex-hook claude …` entry therefore fires inside Grok with a Grok payload.
- **Opt-out** (verified with the env var):
  - `[compat.claude] hooks = false` in `$GROK_HOME/config.toml`
  - or `GROK_CLAUDE_HOOKS_ENABLED=0`

  The resolution order is env, then TOML, then remote flag, then the default (on) (`compat.rs`; `05-configuration.md:427`).
- **`/import-claude` copies the hooks.** It writes the user's Claude hooks into `$GROK_HOME/hooks/imported-from-claude.json` (`claude_import.rs:610, 930-941`) and then disables Claude compat through a `config.toml` marker. After an import, the `claude` adapter entry keeps running from that file.
- **How the Claude adapter should sniff Grok:**
  - Most reliable: `GROK_HOOK_EVENT` (or `GROK_HOOK_NAME`) in the environment, which the runner always sets [e2e].
  - Or stdin with camelCase `hookEventName` or `workspaceRoot`.
  - **Do not** rely on snake_case keys. Grok also emits Claude-looking `session_id`, `transcript_path`, `tool_name`, `tool_input`, `tool_response` and a PascalCase `hook_event_name`.
  - Its `transcript_path` points at Grok's `updates.jsonl`, not a Claude transcript.
  - Its `tool_response` for Bash is Grok's tagged `BashOutput`.

  On a Grok payload, the Claude adapter must exit 0 with empty stdout.

---

## 6. MCP registration

- **Config:** `$GROK_HOME/config.toml`, under `[mcp_servers.<name>]` (`07-mcp-servers.md:19-37`).
- **Preferred command:** `grok mcp add subcortex -- /ABS/subcortex mcp`, which defaults to `--scope user`. **[e2e]:** it appended exactly this block, and `grok mcp remove subcortex` removed it cleanly:

  ```toml
  [mcp_servers.subcortex]
  command = "/ABS/subcortex"
  args = ["mcp"]
  enabled = true
  ```

- **Optional keys:** `env = {…}`, `startup_timeout_sec` (default 30, or env `MCP_TIMEOUT` ms / `GROK_MCP_STARTUP_TIMEOUT_SECS`), `tool_timeout_sec` (default 6000), `tool_timeouts`.
- **Project scope:** `.grok/config.toml` via `--scope project`. It requires folder trust.
- **No drop-in directory** exists for config. The only file-owned alternative is a plugin, `~/.grok/plugins/subcortex/.mcp.json`. It is auto-trusted but needs `[plugins].enabled` (§3).
- **Compat duplicate risk:** Grok also imports MCP servers from `~/.claude.json` (`compat.claude.mcps`, default on), `.mcp.json` and Cursor's `mcp.json`. If subcortex is registered for Claude Code in `~/.claude.json`, Grok already sees it. Check with `grok mcp list --json` before adding a second copy.
- **How the model reaches MCP tools:** through the `search_tool` / `use_tool` meta-tools. A hook sees them as `server__tool`.
- **Output cap:** MCP results over 20,000 bytes are cut inline (`GROK_MAX_MCP_OUTPUT_BYTES` / `[mcp] max_output_bytes`).

---

## 7. Unverified claims and corrections

### Corrections to `sweep.md` §2.5 and §0

1. **B2 shape.**
   - The sweep's example `{"type":"Bash","command":…,"exit_code":0,"output_for_prompt":"…"}`, copied from the docs, is **rejected** by 1.0.40 [e2e].
   - A replacement must carry every required `BashOutput` field. Echo `toolResult` back with only `output_for_prompt` changed; `output:[]` is fine.
   - The 64K cap is confirmed as **clipping, not dropping**.
2. **B4.**
   - Besides SessionStart, **PostCompact** exists, but its stdout is also ignored.
   - SessionStart `source` values are `new` or `load`, not `startup` or `resume`.
   - B4 stays ✗ natively. It becomes ◐ through PostToolUse `additionalContext`.
3. **B1.**
   - ✗ is confirmed at the source and in e2e.
   - It becomes ◐ through PostToolUse `additionalContext`. That channel is **verified** to reach the model as a `<system-reminder>`.
4. **Transcript.**
   - The sweep omitted `transcriptPath` / `transcript_path`, which points at `updates.jsonl`. Use `chat_history.jsonl` beside it.
   - `promptId` is absent on PostToolUse and the compaction events.
5. **"A non-zero exit drops the replacement".** This is true, but irrelevant for us: the `|| true` wrapper always exits 0 (§5).
6. **Timeouts.**
   - Observe and PreToolUse: 5 s.
   - **UserPromptSubmit: 30 s.**
   - PostToolUse, Stop and SubagentStop: 600 s.
   - SessionEnd: 1.5 s.
   - Timeouts are integer seconds.
7. **Hook sources.**
   - The sweep omitted `$GROK_HOME/hooks-paths`, the `config.toml` TOML hooks, and project `.claude` and `.cursor` files.
   - It also omitted `/import-claude` → `imported-from-claude.json`.
   - Codex hooks are **not** scanned.

### Still unverified

1. **Minimum version.** When `updatedToolOutput`, `PostCompact`, `transcriptPath` and the camelCase envelope first shipped. The changelog URL returned 403, and there are no GitHub releases. Only 1.0.40 was tested.
2. **The `opencode` agent profile's `bash` tool** was read from source only; it was not run. Nor were the other presets `grok-build-concise`, `grok-computer` and `explore`, or plugin agents. They may register further shell-tool names. If replacement is missing for some profile, check `toolName` in the hook log.
3. **Auto-compaction.** Only manual `/compact` was exercised. `source:"auto"` is from source, as is whether a two-pass or prefired compaction path skips `run_compact_inner`. The only callers found were `compaction.rs:609, 2291`.
4. **Linux network sandbox.** Whether a sandbox profile with `restrict_network_at_known_linux_launches` blocks the hook's loopback or UNIX-socket connection to a subcortex daemon. The seccomp filter is applied to hook children (`child_net.rs:269`). This was not tested; macOS is unaffected.
5. **Headless auth.** Whether `XAI_API_KEY` is needed at all with a custom `[model.*]` that has `api_key`. The runs set `XAI_API_KEY=dummy`.
6. **Community CLI output.** The community `grok-cli`'s `--version` format was not checked. The detection regex assumes it does not start with `grok <semver> (`.
7. **UserPromptSubmit and SessionStart ordering.** On a brand-new session they can race. UserPromptSubmit was *logged* before SessionStart even though SessionStart's timestamp was earlier [e2e]. Do not rely on SessionStart having run first.
