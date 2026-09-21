# OpenAI Codex CLI: subcortex hook adapter spec (verified September 2026)

Status legend: **[doc]** official docs · **[src]** Rust source read at tag `rust-v0.155.1` (paths relative to `codex-rs/`) · **[rel]** GitHub release notes · **[unverified]**.

---

## 1. Sources, versions, detection

| What | Value |
|---|---|
| Latest release | **0.155.1** (GitHub `openai/codex` tag `rust-v0.155.1`, 2026-09-18; npm `@openai/codex@0.155.1`) |
| Hook history [rel] | 0.114.0 (2026-03-11) experimental engine (SessionStart, Stop), behind the `codex_hooks` flag · 0.116 UserPromptSubmit · 0.117 shell-only Pre/PostToolUse · **0.124.0 (2026-04-23) "Hooks are now stable, can be configured inline in `config.toml`"**, feature `default_enabled: true` [src at tag] · **0.129.0 PreCompact/PostCompact, `/hooks` browser, hook trust metadata and enforcement (#20321)**, `hooks` alias for `codex_hooks` · 0.131.0 `--dangerously-bypass-hook-trust` · **0.133.0 SessionStart `source:"compact"`** · 0.141.0 blocking PostToolUse rejects code-mode calls · 0.145.0 compact SessionStart runs before turn continuation, `additionalContextLimit` · 0.148.0 `async` and `mcp_tool` handlers · 0.155.0 SessionStart `source:"fork"` |
| **Minimum for all 4 behaviors** | **0.133.0** (inline `[hooks]` needs ≥0.124; PreCompact ≥0.129; SessionStart compact ≥0.133). **Recommend ≥0.145.0** so compact-SessionStart context reaches the immediate continuation after mid-turn auto-compaction. |
| Stable? | Yes. `features.hooks` is `Stage::Stable, default_enabled: true` since 0.124 [src `features/src/lib.rs`]. Disable with `[features] hooks = false` (the `codex_hooks` alias is deprecated). |
| Version detection | `codex --version` prints `codex-cli 0.155.1`. |

Sources:
- https://developers.openai.com/codex/hooks (`.md` suffix returns markdown; also served at learn.chatgpt.com/docs/hooks)
- https://developers.openai.com/codex/config-reference · https://developers.openai.com/codex/cli/reference · https://developers.openai.com/codex/mcp
- https://github.com/openai/codex/releases/tag/rust-v0.155.1 · generated wire schemas: https://github.com/openai/codex/tree/main/codex-rs/hooks/schema/generated
- Source (tag rust-v0.155.1): `hooks/src/events/{post_tool_use,user_prompt_submit,compact,session_start}.rs`, `hooks/src/engine/{output_parser,discovery,command_runner}.rs`, `hooks/src/schema.rs`, `hooks/src/lib.rs`, `config/src/{hook_config,config_toml}.rs`, `core/src/tools/{registry,parallel,context}.rs`, `core/src/tools/handlers/unified_exec.rs`, `core/src/hook_runtime.rs`, `features/src/lib.rs`, `tui/src/hooks_rpc.rs`, `history/src/rollout_payload.rs`, `rollout/src/policy.rs`

---

## 2. Config paths, env relocation, trust

- Hook sources [doc]: `~/.codex/hooks.json`, `~/.codex/config.toml` (inline `[hooks]`), `<repo>/.codex/hooks.json`, `<repo>/.codex/config.toml`, and plugin `hooks/hooks.json`. "Codex loads all matching hooks. Higher-precedence config layers don't replace lower-precedence hooks." If one layer has both `hooks.json` and inline `[hooks]`, "Codex merges them and warns at startup".
- Project hooks load only when the project is trusted (`[projects."<path>"] trust_level = "trusted"`).
- **`CODEX_HOME`** relocates everything: `$CODEX_HOME/config.toml`, `hooks.json`, `auth.json` (credential store `file` mode), `sessions/`, `log/`. For isolated e2e, log in inside it (`CODEX_HOME=/tmp/cx codex login --with-api-key`) or copy `auth.json`.
- **Hook trust (hash-based)** [doc+src]: "Before a non-managed hook can run, Codex requires you to review and trust the exact hook definition. Codex records trust against the hook's current hash, so new or changed hooks are marked for review and skipped until trusted." Trust in the TUI happens via `/hooks`. At startup Codex warns if hooks need review.
  - Persisted in the **user `config.toml`** [src `tui/src/hooks_rpc.rs`] as `[hooks.state."<source-path>:<event_key>:<group_idx>:<handler_idx>"]` with `trusted_hash = "sha256:<hex>"` (and `enabled = false` to disable). The key comes from `hooks/src/lib.rs::hook_key`, e.g. `"/Users/me/.codex/config.toml:post_tool_use:1:0"`. The hash is sha256 over the canonical JSON of `{event_name, matcher, hooks:[normalized handler incl. timeout]}` [src `discovery.rs::hook_hash`, `config/src/fingerprint.rs`].
  - Consequences: **keys are positional**. Inserting another matcher group before ours for the same event changes our key, and our hooks become Untrusted again. Any edit to our command, timeout or matcher requires re-trust.
  - One-run bypass: `--dangerously-bypass-hook-trust` (TUI and `codex exec`; "Run enabled hooks without requiring persisted hook trust for this invocation"). Managed hooks (`requirements.toml`, MDM, system) are trusted by policy.
- **Older Codex and unknown `[hooks]` keys**: `ConfigToml` derives serde `Deserialize` with only `#[schemars(deny_unknown_fields)]` (a JSON-schema attribute, not a serde one). Unknown top-level keys are therefore silently ignored on every version checked (0.100.0, 0.120.0, 0.155.1) [src]. Before 0.124 an inline `[hooks]` table is a harmless no-op. **However, on ≥0.124 a type-invalid `[hooks]` value (e.g. `timeout = "10"`) fails the whole `config.toml` deserialization** [src: `hooks: Option<HooksToml>` is a typed field] and Codex refuses to start. Validate the schema, not just TOML syntax.
- **`-c` overrides for a single run** [doc+src]: `-c key=value` ("Values parse as TOML if possible"). Overrides form the `SessionFlags` layer. `discovery.rs::config_toml_source_path` maps it to the synthetic path `<session-flags>/config.toml` and loads its hooks like any other layer (non-managed, so trust or bypass is needed). [unverified end-to-end, but supported by source]:
```bash
CODEX_HOME=/tmp/sc-e2e/codex codex exec --skip-git-repo-check --dangerously-bypass-hook-trust \
  -c 'hooks.UserPromptSubmit=[{hooks=[{type="command",command="/abs/subcortex hook codex UserPromptSubmit || true",timeout=10}]}]' \
  -c 'hooks.PostToolUse=[{matcher="^Bash$",hooks=[{type="command",command="/abs/subcortex hook codex PostToolUse || true",timeout=10}]}]' \
  -c 'hooks.PreCompact=[{hooks=[{type="command",command="/abs/subcortex hook codex PreCompact || true",timeout=10}]}]' \
  -c 'hooks.SessionStart=[{matcher="^compact$",hooks=[{type="command",command="/abs/subcortex hook codex SessionStart || true",timeout=10}]}]' \
  -c model_auto_compact_token_limit=4000 --json "run: seq 1 5000"
```
`--ignore-user-config` skips `$CODEX_HOME/config.toml` ("Authentication still uses CODEX_HOME"). [unverified: whether `$CODEX_HOME/hooks.json` is also skipped with it.] `--json` streams events, including hook runs.

---

## 3. Registration

Hook shape: event → matcher group → handler. Handler fields [src `config/src/hook_config.rs`]: `type` (`command` | `mcp_tool`; `prompt` and `agent` are parsed but skipped), `command`, `commandWindows`/`command_windows`, `timeout` (**seconds**, default 600; SessionEnd/Interrupt 1 s, max 3 s), `async` (bool), `statusMessage`, `additionalContextLimit` (tokens, default 2500; 0 means unlimited). The matcher is a **regex** (`"*"`, `""` or omitted matches all). UserPromptSubmit ignores the matcher. PreCompact matches `trigger` (`manual|auto`). SessionStart matches `source` (`startup|resume|clear|compact|fork`). PostToolUse matches `tool_name` plus aliases: shell and `exec_command` both appear as `Bash`.

Commands run as **`$SHELL -lc "<command>"`** (login shell; `/bin/sh -lc` fallback) with cwd = session cwd [src `command_runner.rs::default_shell_command`]. **Login-shell profile output that reaches stdout pollutes the hook's stdout** (see section 5). `|| true` works in sh, bash, zsh and fish ≥3.

Inline TOML (`~/.codex/config.toml`), marker-delimited:
```toml
# >>> subcortex (managed block; remove with: subcortex uninstall --tui codex)
[[hooks.UserPromptSubmit]]
[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = "'/opt/homebrew/bin/subcortex' hook codex UserPromptSubmit || true"
timeout = 10

[[hooks.PostToolUse]]
matcher = "^Bash$"
[[hooks.PostToolUse.hooks]]
type = "command"
command = "'/opt/homebrew/bin/subcortex' hook codex PostToolUse || true"
timeout = 10

[[hooks.PreCompact]]
[[hooks.PreCompact.hooks]]
type = "command"
command = "'/opt/homebrew/bin/subcortex' hook codex PreCompact || true"
timeout = 10

[[hooks.SessionStart]]
matcher = "^compact$"
[[hooks.SessionStart.hooks]]
type = "command"
command = "'/opt/homebrew/bin/subcortex' hook codex SessionStart || true"
timeout = 10
# <<< subcortex
```
Equivalent `~/.codex/hooks.json` (`{"hooks":{"PostToolUse":[{"matcher":"^Bash$","hooks":[{"type":"command","command":"…","timeout":10}]}], …}}`). hooks.json is the **safer install target for exact uninstall**: it is a JSON merge with command-regex tagging (as for Claude Code), and Codex's own trust writes go to `config.toml`, never to hooks.json. Codex warns if the same layer also has inline `[hooks]`.

Tagging: the marker comments in TOML, or the command regex `(?:^|[\s/'"])subcortex['"]?\s+hook\s+codex\s+(UserPromptSubmit|PostToolUse|PreCompact|SessionStart)\b` in hooks.json. `statusMessage = "subcortex"` is a valid secondary tag, shown while the hook runs. On uninstall, optionally also drop `[hooks.state."…:<event>:<g>:<h>"]` entries whose keys point at our positions.

**Trust step (user-visible):** after install, open `codex`, run `/hooks`, and trust the 4 hooks. Until then they are skipped silently apart from the startup warning (fail-safe). CI and e2e use `--dangerously-bypass-hook-trust`.

---

## 4. Events: input, output, semantics

Common stdin [doc+src schema]: `session_id` (subagent hooks get the parent's id), `transcript_path` (string|null), `cwd`, `hook_event_name`, `model`, plus `turn_id` on turn-scoped events, `permission_mode` on SessionStart, PreToolUse, PermissionRequest, PostToolUse, UserPromptSubmit and others, and `agent_id`/`agent_type` inside subagents. **All output wire structs are `#[serde(deny_unknown_fields)]`** [src `hooks/src/schema.rs`]. Any extra key (e.g. Claude's `updatedToolOutput`, `sessionTitle`) makes the JSON invalid, the run is marked **Failed**, and the output is ignored.

`additionalContext` becomes "extra developer context". Anything over about `additionalContextLimit` (2,500 tokens) is spilled to `<tmp>/hook_outputs/<session_id>/<uuid>.txt` and the model gets a head/tail preview [doc].

### 4.1 UserPromptSubmit (behavior 1)
stdin:
```json
{"session_id":"019651a2-7c3e-7d41-9b1e-3f0c2d4e5a6b","turn_id":"0","transcript_path":"/Users/me/.codex/sessions/2026/09/21/rollout-2026-09-21T10-15-30-019651a2-7c3e-7d41-9b1e-3f0c2d4e5a6b.jsonl",
 "cwd":"/Users/me/proj","hook_event_name":"UserPromptSubmit","model":"gpt-5.5-codex","permission_mode":"default",
 "prompt":"rename foo to bar in utils.py"}
```
(a) emit:
```json
{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit",
  "additionalContext":"[subcortex] A local classifier rated this request as simple (confidence 0.91); the most direct, minimal change is likely sufficient."}}
```
Plain-text stdout is **also** added as developer context [src `user_prompt_submit.rs`: non-JSON stdout goes to `append_additional_context`]. Blockers: `decision:"block"` + `reason`, `continue:false` (stops the turn), exit 2 **with non-empty stderr**.

### 4.2 PostToolUse (behavior 2): replacement is possible; use `continue:false` + `reason`
stdin (Bash via unified exec). **`tool_response` is a JSON *string*: the model-facing output text, already truncated by Codex's output policy. It has no exit code and no header** [src `core/src/tools/context.rs` `ExecCommandToolOutput::post_tool_use_response` → `truncated_output_with_policy`]. If Codex truncated it, the text starts with `Warning: truncated output (original token count: N)`. The hook fires only after the process exits (a later `write_stdin` poll delivers it for long-running commands). It also fires for non-zero exits [doc].
```json
{"session_id":"019651a2-…","turn_id":"3","transcript_path":"/Users/me/.codex/sessions/2026/09/21/rollout-…jsonl",
 "cwd":"/Users/me/proj","hook_event_name":"PostToolUse","model":"gpt-5.5-codex","permission_mode":"default",
 "tool_name":"Bash","tool_use_id":"call_Zx81","tool_input":{"command":"npm ls --all"},
 "tool_response":"proj@1.0.0 /Users/me/proj\n├── …(18,000 chars)…"}
```
What each output does [src `hooks/src/events/post_tool_use.rs` + `core/src/tools/registry.rs` + `core/src/tools/parallel.rs`]:

| Output | Effect on what the model sees |
|---|---|
| `{"decision":"block","reason":R}` or exit 2 + stderr R | `should_block` → `FunctionCallError::RespondToModel(R)` → `failure_response` → `FunctionCallOutput{ body: R, success: Some(false) }`. **The model sees R as a FAILED tool call.** The TUI shows the hook as "Blocked" with feedback. In code mode "the promise rejects with the hook reason". |
| `{"continue":false,"stopReason":S,"reason":R}` | Status "Stopped". `feedback_message = R` (or S if R is empty). `result.result = PostToolUseFeedbackOutput{ model_visible: FunctionToolOutput::from_text(R, success: None) }`. **A clean replacement**: the turn continues, and the code-mode promise is not rejected. The TUI shows a "stop" row with S. Doc: "Codex will replace the tool result with your feedback or stop text and continue from there." |
| `hookSpecificOutput.additionalContext` | Appended as developer context next to the result (the original is still seen). |
| `hookSpecificOutput.updatedMCPToolOutput` / `suppressOutput:true` | "parsed but not supported yet": the run is marked Failed and the original is kept. |
| `reason` without `decision` while `continue` is true | Failed ("PostToolUse hook returned reason without decision"). |
| plain text stdout | ignored. |

(b) **Recommended replacement** (a non-error replacement; the head/tail text goes in `reason`, a short user-facing line in `stopReason`):
```json
{"continue":false,
 "stopReason":"subcortex: trimmed 16500 chars of low-value Bash output",
 "reason":"<first 1000 chars>\n\n[subcortex: trimmed 16500 chars of low-value output; re-run the command if you need the rest]\n\n<last 500 chars>"}
```
Note: the model loses Codex's header (`Wall time`, `Process exited with code N`) because R replaces the whole result. Include a one-line prefix if needed. `reason` counts as "tool feedback" and keeps the default ~2,500-token spill limit [doc], which is fine for about 1.5k chars. Because `tool_response` has no exit code, **failure detection is text-only**: skip when an error regex matches (`^\s*(error|fatal|panic|exception|traceback|FAIL(ED)?)\b`, `npm ERR!`, `error[E\d+]`, and similar) or when the text starts with `Warning: truncated output` (Codex already truncated it).

### 4.3 PreCompact (behavior 3)
stdin:
```json
{"session_id":"019651a2-…","turn_id":"7","transcript_path":"/Users/me/.codex/sessions/2026/09/21/rollout-…jsonl",
 "cwd":"/Users/me/proj","hook_event_name":"PreCompact","model":"gpt-5.5-codex","trigger":"auto"}
```
(c) Output: **nothing**. Plain stdout is ignored. `{"continue":false}` "Codex stops before compacting" (a veto; never emit it). Exit 2 is **not** a veto here: any non-zero exit only sets status Failed with stderr shown [src `compact.rs`]. There is no `custom_instructions` field (unlike Claude).

Transcript (rollout) format [src `history/src/rollout_payload.rs`, `rollout/src/policy.rs`]: JSONL at `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`. Each line is `{"timestamp":"…","type":"<snake_case item>","payload":{…}}`. Messages: `{"type":"response_item","payload":{"type":"message","role":"user|assistant|developer","content":[{"type":"input_text"|"output_text","text":"…"}]}}`. Skip the `developer` role and user items starting with `<environment_context>`, `<user_instructions>`, `# AGENTS.md instructions` or `<turn_aborted>`. `event_msg` `user_message`/`agent_message` (`payload.message`) are persisted only in Legacy history mode, while Paginated mode stores `item_completed` TurnItems. The docs are explicit: "the transcript format isn't a stable interface for hooks". **A more robust design:** keep a per-session ring buffer from our own hooks (UserPromptSubmit `prompt`, plus a `Stop` hook's `last_assistant_message`) and snapshot that at PreCompact. Parse the rollout only as a fallback.

### 4.4 SessionStart source `compact` (behavior 4)
stdin:
```json
{"session_id":"019651a2-…","transcript_path":"/Users/me/.codex/sessions/2026/09/21/rollout-…jsonl",
 "cwd":"/Users/me/proj","hook_event_name":"SessionStart","model":"gpt-5.5-codex","permission_mode":"default","source":"compact"}
```
(a) emit:
```json
{"hookSpecificOutput":{"hookEventName":"SessionStart",
  "additionalContext":"[subcortex] Last messages before compaction (oldest first):\nuser: …\nassistant: …"}}
```
"After Codex compacts a root session, `SessionStart` hooks that match `source: "compact"` run before the next model request … [including] automatic compaction … in the middle of a turn." `continue:false` here "ends the turn without sending another model request" (never emit it). Plain stdout is also added as context. The same `session_id` is kept across compaction [doc: same thread].

---

## 5. Exit codes and output semantics [src, per event `parse_completed`]

| Exit | UserPromptSubmit | PostToolUse | PreCompact | SessionStart |
|---|---|---|---|---|
| 0, empty | ok | ok | ok | ok |
| 0, valid JSON | fields applied | fields applied | fields applied | fields applied |
| 0, `{`/`[`-looking but invalid (incl. unknown fields) | Failed (error row), nothing applied | Failed | Failed | Failed |
| 0, plain text | **added as developer context** | ignored | ignored | **added as developer context** |
| **2 + non-empty stderr** | **BLOCKS the prompt** | **replaces the result with stderr as a failed tool output** | Failed (no veto) | Failed |
| 2 + empty stderr | Failed | Failed | Failed | Failed |
| other non-zero | Failed ("hook exited with code N"), continues | same | same (stderr shown) | same |
| timeout / spawn error | Failed, continues | same | same | same |

"Failed" means a visible error row in the TUI or event stream. It is non-blocking, but noisy.

**Never emit:** exit 2 with stderr (argparse usage errors produce exactly this: exit 2 plus usage on stderr); `decision:"block"` on UserPromptSubmit/PostToolUse; `continue:false` on UserPromptSubmit, PreCompact, SessionStart or PostCompact (PostToolUse `continue:false` is the one deliberate exception, used for replacement); `decision:"block"` without `reason`; `reason` without `decision`/`continue:false`; any key outside the schema (deny_unknown_fields); `updatedMCPToolOutput`; `suppressOutput:true`; **any stray stdout text** on UserPromptSubmit/SessionStart, including login-shell banners from `$SHELL -lc`.

---

## 6. MCP fallback registration
```bash
codex mcp add subcortex -- /opt/homebrew/bin/subcortex mcp
codex mcp remove subcortex
```
The result in `~/.codex/config.toml`:
```toml
[mcp_servers.subcortex]
command = "/opt/homebrew/bin/subcortex"
args = ["mcp"]
startup_timeout_sec = 10   # default 10
tool_timeout_sec = 60      # default 60
```
One run: `-c 'mcp_servers.subcortex={command="/abs/subcortex",args=["mcp"]}'`.

---

## 7. Mismatches in the existing code (file:line → fix)

1. `src/subcortex/adapters/codex.py:146-150`: returns `{"decision":"block","reason":…}`. Per the source, this is delivered as a **failed** function output (`success:false`) and shown as "Blocked". The model may conclude the command failed and re-run it, and code-mode promises reject. Switch to `{"continue":false,"stopReason":"subcortex: trimmed N chars…","reason":<head+marker+tail>}`. The comment at `:148-149` is inaccurate.
2. `src/subcortex/adapters/codex.py:117-128` `_looks_successful`: Bash `tool_response` is a plain string with no exit code, and PostToolUse also fires for non-zero exits. The only failure check is the substring `"Traceback"`, so compiler, test and npm failures get truncated. Add the error-line regex (Claude adapter `claude_code.py:36` has one) and skip text starting with `Warning: truncated output`.
3. `src/subcortex/adapters/codex.py:167-208` `_extract_text`/`_read_transcript_texts`: rollout lines are `{"timestamp","type","payload"}`, while the code looks for top-level `message`/`text`/`content`. **Every line yields "" and the snapshot is always empty, so behavior 4 never fires.** It would also include developer and environment-context items once fixed naively. Parse `response_item`/`message` per section 4.3, and prefer our own ring buffer. `transcript_path` can be `null` (handled, via `str(None)` → `"None"` → OSError; make that explicit).
4. `src/subcortex/installers/codex.py:49-53` `_command`: no `|| true` guard. In Codex, exit 2 plus stderr **blocks the prompt** (UserPromptSubmit) or **replaces tool output with the usage text** (PostToolUse). Append ` || true`, and make `cli.py` never exit 2 (see claude-code.md section 7 items 1-2; `cli.py:300` already accepts `codex`).
5. `src/subcortex/installers/codex.py:72-73, 85-86`: matchers `"Bash"`/`"compact"` are unanchored regexes. They work, but use `^Bash$`/`^compact$` as in the official examples (the tool names `Bash`, `apply_patch` and `mcp__…` do not collide today).
6. **CRITICAL** `src/subcortex/installers/codex.py:67, 80`: `[[hooks.UserPromptSubmit.hooks]]` and `[[hooks.PreCompact.hooks]]` are written with no parent `[[hooks.UserPromptSubmit]]`/`[[hooks.PreCompact]]` header. Verified with tomllib on the installer's own `_block()` output: `hooks.UserPromptSubmit` and `hooks.PreCompact` become **TOML tables (dict)**, not arrays (`PostToolUse`/`SessionStart` are correct lists). Codex types these fields as `Vec<MatcherGroup>` [src `config/src/hook_config.rs`], and `ConfigToml.hooks: Option<HooksToml>` is a typed field, so `deserialize_config_toml_with_base` returns `InvalidData` ("invalid type: map, expected a sequence"). **This most likely breaks loading `config.toml`, so Codex would not start** [high confidence from source; confirm e2e]. The lenient hook-discovery path (`discovery.rs::load_toml_hooks_from_layer`) would also drop *all* inline hooks in that file with a "failed to parse TOML hooks" warning. When the user already has a `[[hooks.UserPromptSubmit]]` group, the same lines instead append our handler into the **user's last group**. Fix: emit an explicit `[[hooks.UserPromptSubmit]]` and `[[hooks.PreCompact]]` header before each `.hooks` sub-table (section 3), and validate the merged document by checking that every `hooks.<Event>` is a list of tables whose `hooks` is a list.
7. `src/subcortex/installers/codex.py:141-174`: no version gate. Codex older than 0.124 silently ignores `[hooks]`, older than 0.129 has no PreCompact, and older than 0.133 has no compact source. Check `codex --version` and warn. Also, only TOML *syntax* is validated. Add schema validation (types of `timeout`, `type`, `command`), because a type error on ≥0.124 makes Codex refuse to start.
8. `src/subcortex/installers/codex.py:121-138` `_strip_block` [unverified risk]: after the user trusts hooks via `/hooks`, Codex writes `[hooks.state."…"]` tables into the same `config.toml` using toml_edit. If they land between our markers (likely when our block is at the end of the file), uninstall strips trust state for **other** hooks too. Either (a) install into `~/.codex/hooks.json` (recommended), or (b) on uninstall, preserve `hooks.state` tables found inside the block. Verify with an e2e test.
9. `src/subcortex/installers/codex.py:39-42`: honors `CODEX_HOME` (good). `status()` does not detect hooks installed via `hooks.json` or trust state. Consider reporting trust status by parsing `hooks.state` keys.
10. `docs/codex.md` "Manual config" snippet has the same missing-`[[hooks.UserPromptSubmit]]`/`[[hooks.PreCompact]]` bug as item 6. Replace it with the section 3 block. The "Trust step" section is accurate. It is missing `--dangerously-bypass-hook-trust`, the positional-key caveat, and the minimum versions. Its PostToolUse description ("block replaces") is technically true but omits that the result is marked as a failure.

### 7b. Uncommitted working-tree refactor (seen while writing; refs above are against HEAD `3c96217`)
- **CRITICAL** `src/subcortex/adapters/__init__.py:21` registers `"codex": "codex:CodexAdapter"`, but `adapters/codex.py` is unchanged from HEAD and defines no `CodexAdapter` class. `get_adapter("codex")` raises AttributeError, `hook.main` swallows it, and every Codex hook becomes a silent no-op (fail-open, but non-functional).
- **HIGH** `src/subcortex/adapters/base.py:32-45` `BLOCKING_VALUES` includes `("continue", False)` and `("decision","block")`, so `guard` will drop BOTH Codex replacement shapes. The new `CodexAdapter` must set `allowed_blocking = (("tool_output", "continue", False),)` and emit `{"continue":false,"stopReason":…,"reason":…}` for `TOOL_OUTPUT` only (section 4.2).
- `installers/base.py:49` `SHELL_GUARD = " 2>/dev/null || true"` matches section 3. Keep it on the Codex commands too, because exit 2 plus stderr blocks prompts in Codex.

---

## 8. Unverified or flagged
- [unverified, source-supported] `-c hooks.<Event>=[…]` from `SessionFlags` loads and runs with `--dangerously-bypass-hook-trust`.
- [unverified] Placement of `[hooks.state]` tables written by `/hooks` relative to our marker block (item 8).
- [unverified] Whether `--ignore-user-config` also skips `$CODEX_HOME/hooks.json`.
- [unverified] How the TUI renders a PostToolUse "Stopped" row (it is expected to show the `stopReason` text; keep it short).
- [src, subject to change] The rollout JSONL schema. The docs call it unstable.
- [src] The `$SHELL -lc` login shell: profile scripts that print to stdout will corrupt hook output. Tell users, or use a wrapper that execs with `sh -c` (e.g. `command = "/bin/sh -c '…'"` still runs under `$SHELL -lc` first; the only real fix is a quiet profile).
