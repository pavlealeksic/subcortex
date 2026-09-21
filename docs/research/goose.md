# goose: subcortex integration spec (goose v1.51.0, 2026-09-17)

Verified against the goose v1.51.0 source (shallow clone of `aaif-goose/goose` at tag `v1.51.0`) and the official docs. "SRC" means read in source. "DOC" means stated in the docs only. "UNVERIFIED" means inferred and not executed.

## TL;DR for the adapter
- **All four behaviors need hooks that goose cannot provide as a hook channel.** At v1.51.0 every hook other than `PreToolUse` and `Stop` is observation-only. goose ignores stdout from `UserPromptSubmit`, `PostToolUse` and `SessionStart` (the one exception is a user-visible `banner`, described below). No event is fired for compaction. The `PostToolUse` payload does not include the tool output.
  - (1) Prompt hint: **not possible through a hook.** There are two workarounds: the goose-native `GOOSE_MOIM_MESSAGE_FILE` per-turn injection (the hook writes a file), and static MCP `instructions`.
  - (2) Tool-output replacement: **impossible.** The hook cannot see the output and cannot change it. goose already truncates shell output itself (see §4).
  - (3) Pre-compact snapshot and (4) post-compact re-inject: **no events exist.** Only degraded workarounds are available (see §4c).
- The adapter must register **only** observation events. It must exit 0 with empty stdout. It must **never register `PreToolUse` or `Stop`**, and must never set `on_failure`.
- Install as one self-owned plugin directory, `~/.agents/plugins/subcortex/`. Register the MCP server in `config.yaml` under `extensions.subcortex`.

## 1. Sources, versions, detection
Sources:
- Docs, hooks: https://goose-docs.ai/docs/guides/context-engineering/hooks/ (fetched as agent-13_12.md; matches the repo copy `documentation/docs/guides/context-engineering/hooks.md` at v1.51.0)
- Blog, 2026-05-14: https://goose-docs.ai/blog/2026/05/14/goose-hooks/. Its payload example `"tool_name":"developer__shell"` is **outdated**: the tool is now bare `shell`.
- Docs, plugins: https://goose-docs.ai/docs/guides/context-engineering/plugins/
- Docs, persistent instructions (MOIM): https://goose-docs.ai/docs/guides/context-engineering/using-persistent-instructions/
- Docs, environment variables: https://goose-docs.ai/docs/guides/environment-variables/
- Docs, config files: https://goose-docs.ai/docs/guides/config-files/
- Source, https://github.com/aaif-goose/goose/tree/v1.51.0 (`block/goose` redirects here):
  - `crates/goose/src/hooks/mod.rs`: engine, payload struct, exit/stdout classification
  - `crates/goose/src/plugins/{discovery.rs,mcp_servers.rs,mod.rs,formats/open_plugins.rs}`
  - `crates/goose/src/agents/agent.rs`: default loop, and where events fire
  - `crates/goose/src/agents/state_machine/ops_*.rs`: opt-in loop, `GOOSE_STATE_MACHINE=1`
  - `crates/goose/src/config/{paths.rs,base.rs,extensions.rs}`, `crates/goose/src/agents/extension.rs`
  - `crates/goose/src/agents/{moim.rs,platform_extensions/tom.rs,large_response_handler.rs,platform_extensions/developer/shell.rs}`
  - `crates/goose/src/context_mgmt/mod.rs`
  - `crates/goose-cli/src/{cli.rs,session/mod.rs,session/builder.rs,session/output.rs}`
- PRs: #9093 (hooks), #9088 (plugins in `~/.agents/plugins`), #9304 (PreToolUse deny), #9468 (Stop blocking), #9471 (MCP servers in plugins), #9615 (PATH repair), #9960 (SubagentStart/SubagentStop removed), #9968 (Stop `last_assistant_message`), #10596 (no hooks for subagents), #11120 (`PreToolUseResult` + `tool_call_id`), #11449 (`on_failure`).

Versions:
- Latest: **v1.51.0** (2026-09-17). There are no hook changes after v1.48.0 at source level. Workspace `Cargo.toml` has `version = "1.51.0"`.
- **Minimum with hooks: v1.34.0** (2026-05-13, SRC: `hooks/mod.rs` is absent at v1.33.1 and present at v1.34.0). Timeline, from source checks at each tag:

| Tag | Change (SRC) |
|---|---|
| v1.34.0 | 11 events: PreToolUse, PostToolUse, PostToolUseFailure, SessionStart, SessionEnd, UserPromptSubmit, BeforeReadFile, AfterFileEdit, BeforeShellExecution, AfterShellExecution, Stop. All observation-only. Plugins live in `~/.agents/plugins`. |
| v1.35.0 | PreToolUse deny added (exit 2 / `{"decision":"block"}`). SubagentStart/SubagentStop present from v1.35 through v1.39. |
| v1.37.0 | Stop can block. |
| v1.39.0 | Plugin `.mcp.json` MCP servers (`plugins/mcp_servers.rs`, absent at v1.38.0). |
| v1.40.0 | SubagentStart/SubagentStop removed. Stop gains `last_assistant_message`. |
| v1.46.0 | SessionStart `{"banner":...}` in the interactive CLI. Hooks skipped for subagents. |
| v1.48.0 | `PreToolUseResult` event, `tool_call_id`, `on_failure`. |

  Note: the release-note bodies repeat items across versions and are not reliable, so the dates above come from the source.
- Recommended floor for subcortex: **v1.46.0**, which has a stable payload and skips hooks in subagents. Hook files are forward-compatible: unknown events and unknown action types are ignored.
- Version detection: run `goose --version`. clap renders `"{display_name} {version}\n"`, and `cli.rs` sets `display_name = ""`, so the output is expected to be **` 1.51.0`** with a leading space and no "goose" prefix (derived from clap_builder 4.6.7 `_render_version`, not executed; UNVERIFIED). Parse with `(\d+)\.(\d+)\.(\d+)` and do not match on a "goose " prefix.

## 2. Config paths, environment relocation, trust
Path resolution (SRC `config/paths.rs` + etcetera 0.11 `choose_app_strategy` = **Xdg on macOS and Linux**, Windows strategy on Windows):

| What | Default (macOS/Linux) | With `GOOSE_PATH_ROOT=/abs` |
|---|---|---|
| config.yaml (extensions, `plugins` map, `GOOSE_*` keys) | `$XDG_CONFIG_HOME` or `~/.config/goose/config.yaml` | `/abs/config/config.yaml` |
| secrets (when `GOOSE_DISABLE_KEYRING` is set) | `~/.config/goose/secrets.yaml` | `/abs/config/secrets.yaml` |
| data (`sessions/sessions.db`) | `~/.local/share/goose/` | `/abs/data/` |
| state (logs) | `~/.local/state/goose/` | `/abs/state/` |
| **user plugins** (also where `goose plugin install` writes) | `$HOME/.agents/plugins/<name>/` | `/abs/.agents/plugins/<name>/` |
| user plugin settings | `~/.config/goose/settings.json` | `/abs/.config/goose/settings.json` |
| project plugins | `<cwd>/.agents/plugins/<name>/` | same |
| project plugin settings | `<cwd>/.config/goose/settings.json`, `<cwd>/.config/goose/settings.local.json` | same |
| system config (always merged first, read-only) | `/etc/goose/config.yaml` | same (not relocatable) |

- The docs page on environment variables says the macOS default is `~/Library/Application Support/Block/goose/`. **This conflicts with the source**, where `choose_app_strategy` returns Xdg on macOS. Treat the source as authoritative, but UNVERIFIED on a real macOS install.
- `GOOSE_PATH_ROOT` must be absolute; a relative value is ignored. `GOOSE_ADDITIONAL_CONFIG_FILES` holds path-list extra config files merged between the system and user config.
- Config precedence: environment variable (exact uppercase key) overrides the config.yaml key.
- **Isolated e2e recipe:**
  ```
  GOOSE_PATH_ROOT=$T HOME=$T/home GOOSE_DISABLE_KEYRING=1 GOOSE_PROVIDER=... GOOSE_MODEL=... <KEY>=... goose run -t "..."
  ```
  Put the plugin at `$T/.agents/plugins/subcortex/`, and run from a temp cwd so no project plugins load. `/etc/goose/config.yaml` still applies if it exists.
- Trust and enable:
  - **There is no trust prompt.** Every subdirectory of the plugin dirs is discovered at agent construction and is enabled by default.
  - On first discovery goose **writes** `plugins: { "<abs plugin root>": { enabled: true } }` into the user config.yaml. Setting `enabled: false` there drops the plugin.
  - `disabledPlugins` / `enabledPlugins` (lists of plugin names) in settings.json are checked in the order Local, then Project, then User; the first listing wins.
  - Plugins with the same name are de-duplicated, and **project scope wins**, so a project plugin named `subcortex` would shadow ours.
  - Hooks load **once per Agent**: with `project_root = process cwd`, and never for subagents. A running session does not reload them. Edits apply to new sessions.

## 3. Registration
Plugin layout, fully owned by subcortex:
```
~/.agents/plugins/subcortex/          # or $GOOSE_PATH_ROOT/.agents/plugins/subcortex/
├── plugin.json                       # manifest lookup order: .goose-plugin/plugin.json, .plugin/plugin.json, plugin.json
├── .subcortex-managed                # our own marker file; goose ignores it
└── hooks/hooks.json
```
`plugin.json`: goose only reads `name`, `version`, `skills` and `mcpServers`. The name falls back to the directory name.
```json
{ "name": "subcortex", "version": "0.1.0", "description": "subcortex context hooks (managed)" }
```
`hooks/hooks.json`:
```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "exec /usr/local/bin/subcortex hook goose SessionStart", "timeout": 5 } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "exec /usr/local/bin/subcortex hook goose UserPromptSubmit", "timeout": 5 } ] }
    ],
    "PostToolUse": [
      { "matcher": "^shell$", "hooks": [ { "type": "command", "command": "exec /usr/local/bin/subcortex hook goose PostToolUse", "timeout": 3 } ] }
    ],
    "SessionEnd": [
      { "hooks": [ { "type": "command", "command": "exec /usr/local/bin/subcortex hook goose SessionEnd", "timeout": 5 } ] }
    ]
  }
}
```
Schema (SRC):
- Top level is `{"hooks": {<Event>: [rule...]}}`. A rule is `{matcher?, hooks: [action...]}`. An action is `{type?: "command", command: string, timeout?: u64 seconds, on_failure?: "allow"|"block"}`. Unknown keys are ignored (no `deny_unknown_fields`).
- `type` omitted or `"command"` means a command action. Any other string makes goose ignore that action. A non-string `type` or `command` makes goose **skip the whole hooks.json file**.
- `on_failure` is read only for `PreToolUse`. **Never set it.**
- `matcher` is an unanchored Rust `regex` tested against `matcher_context`. It is `""` for SessionStart, SessionEnd and Stop. An invalid regex (for example a bare `*`) skips that rule with a warning. Omit it, or use `.*`, to match everything.
- Timeout: default 30 s, no documented max. On timeout the future is dropped and `kill_on_drop` kills the **direct child** (`sh`). Use `exec` so subcortex is that child and is not orphaned. Note that `timeout: 0` would time out immediately.
- The command runs through `sh -c "<command>"`:
  - `${PLUGIN_ROOT}` is string-expanded, and `PLUGIN_ROOT` is also exported.
  - The environment is inherited from goose. The CLI does **not** repair PATH; goose Desktop merges the login-shell PATH. **Use an absolute binary path.**
  - cwd is **not set**, so the hook inherits the goose process cwd. Payloads carry `working_dir` on tool events only.
- Execution is **sequential**: plugins sort project before user, then by name, and hooks run in config order. Each hook is **awaited**. `PostToolUse` runs inside the tool-result future before the result reaches the model, so hook latency adds directly to every tool call and every prompt.

Uninstall, removing exactly what is ours:
1. `rm -rf <plugins dir>/subcortex`, only if `.subcortex-managed` exists.
2. In config.yaml, delete `plugins["<abs path of that dir>"]`, the entry goose auto-added.
3. Delete `extensions.subcortex`, only if its `cmd` is our binary (see §6).
4. Do not touch settings.json.

goose rewrites config.yaml through serde_yaml, so YAML comments are not preserved; identify our entries by key plus `cmd`, not by comments. Do **not** create `.goose-plugin-install.json` in our dir, because that is the metadata for `goose plugin install`/`goose plugin update`.

## 4. Events

| subcortex behavior | goose event | Can respond? |
|---|---|---|
| prompt-submit | `UserPromptSubmit` | No. Output ignored (SRC `emit()`). |
| after-tool | `PostToolUse` (success), `PostToolUseFailure`, `AfterShellExecution` (success shell only; matcher = the command) | No. Output ignored, and the payload has no output. |
| pre-compact | **none** | n/a |
| session-start after compaction | **none**. `SessionStart` fires once per CLI session and on `--resume`, never after compaction, and has no `source` field. | Interactive CLI only: `{"banner": "..."}` is printed to the user's terminal, **not to the model**. |

All events: `SessionStart`, `SessionEnd`, `Stop`, `UserPromptSubmit`, `PreToolUse`, `PreToolUseResult`, `PostToolUse`, `PostToolUseFailure`, `BeforeReadFile`, `AfterFileEdit`, `BeforeShellExecution`, `AfterShellExecution`. There is **no** PreCompact, PostCompact, SubagentStart, SubagentStop or Notification event.

Two agent loops exist:
- **Default** (`agent.rs`).
- Opt-in `GOOSE_STATE_MACHINE=1` (`state_machine/`). This loop adds `working_dir` to SessionStart and UserPromptSubmit and fires SessionStart from its entry operation.

Samples below are for the default loop. Payload field order follows `HookContext`. `matcher_context` is always present, and is `null` when unset. `tool_output` exists in the struct but is **never populated** in v1.51.0.

Stdin samples:
```json
{"event":"SessionStart","session_id":"20260921_3","matcher_context":null}
```
```json
{"event":"UserPromptSubmit","session_id":"20260921_3","matcher_context":"rename foo to bar in src/lib.rs","message":"rename foo to bar in src/lib.rs"}
```
```json
{"event":"PostToolUse","session_id":"20260921_3","matcher_context":"shell","tool_call_id":"toolu_01HZ8k2QmV7aYw3nB9cXp4Lr","tool_name":"shell","tool_input":{"command":"cargo test 2>&1","timeout_secs":600},"working_dir":"/Users/you/proj"}
```
```json
{"event":"AfterShellExecution","session_id":"20260921_3","matcher_context":"cargo test 2>&1","tool_name":"shell","tool_input":{"command":"cargo test 2>&1","timeout_secs":600},"working_dir":"/Users/you/proj"}
```
```json
{"event":"Stop","session_id":"20260921_3","matcher_context":null,"last_assistant_message":"Done. Renamed foo to bar and tests pass.","working_dir":"/Users/you/proj"}
```
```json
{"event":"SessionEnd","session_id":"20260921_3","matcher_context":null}
```

Notes on the samples:
- `session_id` has the format `YYYYMMDD_N` (SRC session_manager).
- Tool names: developer, analyze, summon and code_execution tools are **unprefixed** (`shell`, `write`, `edit`, `tree`, `read_image`). Other extensions use `<ext>__<tool>`.
- Shell `tool_input` keys: `command`, `timeout_secs?`.

**(a) Context injection**
- **No hook response field exists.** `additionalContext`, `hookSpecificOutput`, `context` and plain stdout are all ignored.
- Workaround A, **MOIM file** (SRC `platform_extensions/tom.rs`, `moim.rs`, `agent.rs reply()`):
  - Setup: the user's shell must export `GOOSE_MOIM_MESSAGE_FILE=~/.subcortex/goose/moim.md`. This is read with `std::env::var` **only**; a config.yaml key does NOT work.
  - Per-turn flow in the default loop: the `UserPromptSubmit` hook (awaited) runs, then auto-compaction, then `reply_internal` builds the turn-context message. That message re-reads the file, capped at 64 KB, and appends it as an agent-only `<turn-context>` user message.
  - The hook should therefore write the hint to the file for simple prompts and **truncate it to empty otherwise**, because the content repeats every turn while the file is non-empty.
  - Limits:
    - the built-in `tom` extension must be enabled (default on);
    - skipped when the model context limit is under 32,000;
    - one file per process environment, so concurrent sessions race (UNVERIFIED in practice);
    - injected hints stay in history.
- Workaround B, **MCP `instructions`**: the `InitializeResult.instructions` of `subcortex mcp` go into the system prompt under that extension's "### Instructions", with `{{WORKING_DIR}}` substituted. They are read when the extension loads (session start or resume), so they are static per session. Use this for a constant "prefer the most direct, minimal path" policy.

**(b) Tool-output replacement**
- **Impossible.** There is no `updated_output`, `updatedMCPToolOutput` or `updated_input`. `PreToolUse` only accepts `decision` and `reason`, and the post events carry no output.
- goose already does this natively (SRC):
  - The developer `shell` tool: if output exceeds 2,000 lines or 50,000 bytes, goose keeps the last 50 lines (at most 10,000 bytes), adds the notice `[Output exceeded … Full output saved to <path>. Read it with …]`, and saves the full output to a file.
  - Any tool text over `GOOSE_MAX_TOOL_RESPONSE_SIZE` (default 200,000 chars) is written to a temp file and replaced with a pointer.
  - Old tool pairs are summarized by `GOOSE_TOOL_PAIR_SUMMARIZATION` (default true) past `GOOSE_TOOL_CALL_CUTOFF`.

**(c) Compaction**
- There are no hooks.
- Triggers:
  - (i) automatic, in `reply()` right after the UserPromptSubmit hook and before the turn, when tokens exceed `GOOSE_AUTO_COMPACT_THRESHOLD` (default `0.8`) times the context limit;
  - (ii) recovery compaction mid-turn on context-length errors;
  - (iii) manual compaction.
- Never for providers that manage their own context.
- goose keeps the original messages in `sessions.db`, hidden from the agent, and injects its own summary plus a continuation note.
- Degraded options (UNVERIFIED, optional):
  - Snapshot inside `UserPromptSubmit`, via `goose session export --session-id <id> --format json`, or read-only SQLite on `<data>/sessions/sessions.db` (tables `sessions(id,…,total_tokens)` and `messages(session_id, role, content_json, metadata_json)`; internal schema).
  - Re-inject through the MOIM file. The hook cannot tell whether compaction will happen this turn, so pairing is heuristic.
  - Recommendation: **treat 3 and 4 as unsupported on goose.**

## 5. Exit codes and output semantics (SRC `hooks/mod.rs`)

**Observation events**: every event except `PreToolUse` and `Stop`. Blocking by these hooks is impossible.
- exit 0 → OK.
- Any non-zero exit, **including 2** → WARN log `Plugin hook failed` with the lossy UTF-8 stderr, and goose continues.
- Timeout, spawn failure, or a stdin write failure such as EPIPE when the hook exits without reading stdin → logged, and goose continues.
- stdout is **ignored entirely**: empty, invalid JSON and non-UTF-8 are all fine.
- stderr is used only in logs.

**SessionStart in the interactive CLI (`emit_collecting_banners`)**:
- On exit 0, stdout is trimmed. If it starts with `{`, parses as JSON and has a non-empty string `banner`, goose prints it to the terminal.
- Anything else is silently ignored.
- `goose run` (headless) uses plain `emit`, so banners are ignored there.

**Blocking events** (`classify_output`):
1. Exit 2 → deny, with the reason taken from stderr (default `denied by plugin hook`).
2. Otherwise, stdout must be valid UTF-8, or the result is a hook failure. Trimmed stdout that starts with `{`, parses, and has `"decision":"block"` → deny, **regardless of the exit code**.
3. Exit 0 with empty stdout, or with `"decision":"allow"` → allow.
4. Anything else, or a timeout, signal or spawn error → hook failure. A hook failure fails open unless `on_failure: "block"` is set on a PreToolUse action.

Default timeout is 30 s.

**Every response that blocks, denies or stops. The adapter must never produce these.** The simplest guarantee is to never register `PreToolUse` or `Stop` and never set `on_failure`.
- `PreToolUse`: exit code 2.
- `PreToolUse`: stdout JSON `{"decision":"block", ...}` with **any** exit code.
- `PreToolUse` with `"on_failure":"block"`: **any** hook failure. That includes a non-zero exit without a decision, a timeout, a spawn failure, invalid UTF-8 stdout, stray stdout text, `{}`, an unknown decision value, a stdin delivery failure, or a payload serialization failure.
- `Stop`: exit 2, or stdout `{"decision":"block"}` with any exit code. This forces the turn to continue, up to `GOOSE_STOP_HOOK_BLOCK_CAP` consecutive times (default 8). `on_failure` does not apply to Stop.
- A config error in hooks.json does not block anything; goose skips the file. For example, a PreToolUse `on_failure` value other than `allow` or `block` skips that plugin's whole hooks.json.

**Adapter contract for goose:** always exit 0, print nothing to stdout (optionally `{"banner":"…"}` on SessionStart only), send diagnostics to stderr, read stdin fully, and finish well under the configured timeout.

## 6. MCP / extension registration (`subcortex mcp`)

**Preferred: config.yaml**, at `~/.config/goose/config.yaml` or `$GOOSE_PATH_ROOT/config/config.yaml`. This loads on new and resumed sessions.
```yaml
extensions:
  subcortex:
    enabled: true          # required (ExtensionEntry.enabled has no default)
    type: stdio            # serde tag
    name: subcortex        # injected from the key if missing
    description: "subcortex context tools"
    cmd: /usr/local/bin/subcortex
    args: [mcp]            # REQUIRED for stdio (no serde default)
    envs: {}               # alias `env`; PATH, LD_*, DYLD_*, PYTHONPATH, NODE_OPTIONS etc. are rejected
    env_keys: []
    timeout: 300           # seconds; DEFAULT_EXTENSION_TIMEOUT = 300
    bundled: false
    # cwd: /some/dir       # optional
    # available_tools: []  # optional allowlist
```
- A malformed entry is skipped with an info log, so this fails open.
- Tools are exposed as `subcortex__<tool>`.
- There is **no non-interactive `goose extension add` command** in v1.51.0. The top-level commands are: configure, info, doctor, mcp, acp, roam, serve, session, run, recipe, skills, plugin, schedule, gateway, update, term, local-models, completion, review, validate-extensions, mcp-probe. `goose configure` is interactive.

**Per run**, from `cli.rs --with-extension` (format `[name:]ENV=val cmd args`):
```
goose session --with-extension "subcortex:/usr/local/bin/subcortex mcp"
```

**Alternative: plugin-bundled server (≥ v1.39.0)**
- Declare it in `~/.agents/plugins/subcortex/.mcp.json`:
  ```json
  {"mcpServers":{"subcortex":{"command":"/usr/local/bin/subcortex","args":["mcp"],"env":{}}}}
  ```
- goose creates a stdio extension named `subcortex:subcortex` with timeout 300 and `PLUGIN_ROOT` in its environment.
- **It is not loaded** with `--resume`, `--no-profile`, or when a recipe supplies extensions (SRC `goose-cli/src/session/builder.rs`), so config.yaml is more reliable.
- The exact tool prefix after name normalization (`subcortex_subcortex__…`?) is UNVERIFIED.

**Context channels an MCP server can use:**
- `instructions`: static system prompt text; see §4a.
- Tool results, but only when the model chooses to call them.
- Per-turn MOIM injection is **only for in-process platform extensions**; stdio MCP clients return `None`.

## 7. Unverified / caveats
- `goose --version` leading-space output: derived from source, not run.
- The macOS config/data paths are derived from source and conflict with the docs; confirm on a real install. Run e2e tests with `GOOSE_PATH_ROOT` to avoid the question.
- The MOIM-file workaround (concurrent-session race, `tom` enabled by default, exact placement in the request) is from source reading only. The env var must be in goose's environment; a config key does not work.
- Hook cwd in goose Desktop, where the `goosed` process cwd determines project-plugin discovery: not verified. Project plugins may not load in Desktop.
- `exec` in the `sh -c` command, so the timeout kill reaches subcortex, is our recommendation and not a goose-documented requirement.
- The `sessions.db` schema and `goose session export` are usable for snapshots but are not a hook contract. Migrations may add or rename columns.
- `tool_output` is present in the payload struct but unused in v1.51.0. A future version may start populating it; stdout would still be ignored unless the engine changes.
- The `state_machine` loop is opt-in, not the default, and its payloads differ slightly (it adds `working_dir`). Behavior is otherwise the same: observation-only.
