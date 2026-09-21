# Mistral Vibe (`vibe`) — subcortex hook adapter spec

Target: **mistral-vibe 2.25.5** (PyPI latest, uploaded 2026-09-18T11:10Z; git tag `v2.25.5` = `c069ffa`). Verified against the source at that tag **and** against the pinned Unified-Harness wheel `mistralai-vibe-local-harness==0.5.1` (pure-Python hook glue inside the wheel; macOS arm64 wheel unpacked and read). Nothing was executed.

**Bottom line:** B2 ✅ (genuine, non-blocking replacement via `post_tool` `{"decision":"deny","reason":…}`, both harnesses). B1 ✗, B3 ✗, B4 ✗ as events (only static / append-only workarounds, §4). Vibe has exactly three hook types: `post_agent`, `pre_tool`, `post_tool`.

---

## 1. Sources, versions, detection

- Source (https://github.com/mistralai/mistral-vibe, tag `v2.25.5`):
  - `vibe/core/hooks/models.py` (`HookConfig`, invocation payloads, `HookStructuredResponse`), `config.py` (TOML loading, duplicate names), `executor.py` (subprocess, timeout), `manager.py` (exit-code / JSON dispatch, strict handling), `_handler.py` (response parsing, failure reasons), `_post_tool.py` (**deny → replacement**), `_pre_tool.py`, `_post_agent.py`.
  - `vibe/core/agent_loop_hooks.py` (`_run_post_tool_and_finalize` applies `HookTextReplacement`), `vibe/core/agent_loop/_loop.py` (`_invoke_tool` builds `tool_output_text`; failure path; `_handle_tool_response` writes the tool message).
  - `vibe/core/config/harness_files/_harness_manager.py` (`hook_files`), `vibe/utils/paths.py` (`VIBE_HOME`), `vibe/core/config/models.py` (`MCPStdio`), `vibe/cli/mcp_command.py` (`vibe mcp add`), `vibe/core/config/layers/_base.py` (config.toml writer).
  - Unified harness: `vibe/_experimental_harness.py` (selection), `vibe/app_server/_runtime.py` (`_foreign_hook_definitions`), wheel `mistralai_vibe_local_harness/vibe/_foreign_hooks.py`, `_hook_matcher.py`.
- Docs: README "Hooks" and "MCP Server Configuration"; built-in skill `vibe/plugins/builtins/vibe/skills/vibe/SKILL.md` (§Hooks — same contract, more detail).
- History (CHANGELOG): experimental `before_tool`/`after_tool` (behind `enable_experimental_hooks`) → **2.21.0 (2026-07-17)** hooks graduated, renamed to `post_agent`/`pre_tool`/`post_tool`, flag removed (hooks load whenever declared). 2.25.1 made Unified-Harness hook commands run **without a shell** (harness 0.4.3–0.4.5, Vibe 2.25.1–2.25.4); **2.25.5** (harness 0.5.1) runs them through the shell again and fixes `match` for `edit`/MCP tools and hooks inside subagents. The legacy executor always used a shell.
- **Recommended minimum: 2.25.5.** (Legacy harness works from 2.21.0.)
- Version: `vibe --version` → `vibe 2.25.5` (argparse `%(prog)s {__version__}`).
- Detect: payload `hook_event_name` ∈ `post_agent|pre_tool|post_tool` (lowercase snake). Legacy payload always has `session_id` + `transcript_path`; Unified payload has neither.

## 2. Two harnesses (must support both)

`resolve_harness_selection`: `--legacy-harness` → legacy; `--experimental-harness` → Unified; else the GrowthBook rollout cache (`vibe_cli_unified_harness_rollout`, default `"legacy"`) may put a user on **Unified**; else legacy. Same `hooks.toml`, different glue:

| | Legacy (`vibe/core/hooks`) | Unified (`_foreign_hooks.py`, harness 0.5.1) |
|---|---|---|
| Spawn | `asyncio.create_subprocess_shell(command, cwd=session cwd, start_new_session=True)` | same (shell) in 0.5.1; **argv without shell in 0.4.3–0.4.5** |
| stdin | `invocation.model_dump_json()` (full payload, §4) | reduced payload: `cwd, hook_event_name, tool_name, tool_call_id, tool_input, tool_status, tool_output, tool_output_text, tool_error, duration_ms(=0)` — **no `session_id`, `transcript_path`, `parent_session_id`** |
| `tool_name` | bare (`bash`) | qualified (`file_system.bash`); `match` is tried against qualified, leaf and model name |
| Output cap | 1 MiB stdout/stderr | 1,000,000 bytes |
| post_tool deny | replace text, status unchanged | replace `content`, **also clear `structured_content`**, keep result type |

Because 2.25.1–2.25.4 on Unified ran commands without a shell, `subcortex-hook` must **tolerate trailing junk argv** (`2>/dev/null`, `||`, `true`) and still exit 0 with valid stdout if invoked that way.

## 3. Config, relocation, trust, registration

- Files (`HarnessFilesManager.hook_files`): `<project_root>/.vibe/hooks.toml` for each trusted project root (loaded **first**), then `$VIBE_HOME/hooks.toml` (user). `VIBE_HOME` = `$VIBE_HOME` (expanded, resolved) or `~/.vibe`.
- Trust: user file needs none. Project `.vibe/` is only read in trusted folders (`$VIBE_HOME/trusted_folders.toml`).
- Duplicate `name` across files → config issue, **first (project) wins**; our entry would be shadowed by a project hook with the same name → use distinctive names.
- Load time: at session start (`load_hooks_from_fs`); re-read only on internal reloads (cwd relocation / app-server reload). Assume **restart**.
- Vibe never writes `hooks.toml` (only `config.toml`), so comments/fences in `hooks.toml` survive.

`HookConfig` (pydantic, extra keys ignored by the default loader):

| Field | Type | Notes |
|---|---|---|
| `name` | str, required | unique across loaded files |
| `type` | `post_agent` \| `pre_tool` \| `post_tool` | |
| `command` | str, non-blank | shell line |
| `match` | str, optional | tool hooks only (error on `post_agent`); fnmatch glob, case-insensitive, full-name; `re:<regex>` = case-insensitive fullmatch; default `*` |
| `timeout` | float **seconds**, optional | default **60.0**; whole process group killed on expiry |
| `strict` | bool, default false | tool hooks only (error on `post_agent`) |
| `description` | str, optional | |

An invalid entry is skipped with a TUI warning (the rest of the file still loads); invalid TOML skips the whole file.

```toml
# >>> subcortex (managed) >>>
[[hooks]]
name = "subcortex-post-tool"
type = "post_tool"
match = "bash"
command = "'/abs/subcortex-hook' vibe post_tool 2>/dev/null || true"
timeout = 10.0
description = "subcortex: compact shell output"
# <<< subcortex <<<
```

- `match = "bash"`: the model-facing shell tool is `bash` for both the legacy `Bash` tool and the managed-shell variant (`experimental_bash.py get_name() → "bash"`). On Windows the shell tool may be `windows_shell`/`git_bash` (not verified); `re:(bash|git_bash|windows_shell)` covers them.
- **Never set `strict = true`**: any failure (non-zero exit, timeout, non-JSON stdout) then **clears the tool result to ""** (legacy `on_strict_failure`; Unified `_post_tool_fail` blanks `content` and `structured_content`).
- Tagging: `name` prefix `subcortex-` (plus the command prefix `'/abs/subcortex-hook' vibe `). Uninstall = drop `[[hooks]]` tables with that name prefix; parse with a real TOML parser and rewrite only if fences are absent.
- Execution: hooks of one type run **sequentially** in load order; tool calls in one LLM turn run **concurrently**, so post_tool chains for different calls run in parallel. Subagents inherit the parent's hooks (`parent_session_id` set, legacy).

## 4. Events: stdin and responses for (a)–(d)

Response contract (both harnesses): exit 0 + **empty stdout** = passthrough; exit 0 + **JSON object** = structured response `{decision?: "allow"|"deny", reason?: str, system_message?: str, hook_specific_output?: {tool_input?: obj /*pre_tool*/, additional_context?: str /*post_tool*/}}` (unknown fields ignored). Anything else (non-zero exit, timeout, non-JSON or non-object stdout, schema mismatch) = **failure path**: UI warning, action proceeds unchanged (unless `strict`).

### (a) Prompt submit → context hint: ✗ (no event)
- There is no pre-turn / prompt-submit hook. `post_agent` `deny` injects a **retry user message** and forces another model turn (capped 3/hook/user turn) — that is a blocking-style veto of the answer; **never use it**.
- Workarounds (all partial):
  - Static: `AGENTS.md`, custom system prompt (`$VIBE_HOME/prompts/`), or the MCP server's `prompt` field ("usage hint appended to tool descriptions", §5).
  - Append-only piggyback: on the first `post_tool` of a turn, return `{"hook_specific_output":{"additional_context":"<hint>"}}` (appended with `\n` to the shell output). Only reaches the model when a shell tool runs.

### (b) After shell tool → replace output: ✅
Legacy stdin (`PostToolInvocation.model_dump_json()`):
```json
{"session_id":"4c1f…","transcript_path":"/Users/me/.vibe/logs/session/…/messages.jsonl","cwd":"/Users/me/proj","parent_session_id":null,"hook_event_name":"post_tool","tool_name":"bash","tool_call_id":"call_42","tool_input":{"command":"npm test","timeout":null},"tool_status":"success","tool_output":{"command":"npm test","shell":"/bin/zsh","exit_code":0,"stdout":"…","stderr":"…","returncode":0},"tool_output_text":"command: npm test\nshell: /bin/zsh\nexit_code: 0\nstdout: …\nstderr: …\nreturncode: 0","tool_error":null,"duration_ms":8123.4}
```
- `tool_output_text` = exactly what the LLM will see so far (`"\n".join(f"{k}: {v}")` of the result dict + optional extra; mutated by earlier hooks in the chain).
- **Non-zero exit is a failure, not a success** (legacy `Bash` tool, the default `managed_shell_tools` variant "legacy"; the managed-shell variant is UNVERIFIED): `completed_shell_result` raises `ToolError`, so post_tool gets `tool_status:"failure"`, `tool_output:null`, `tool_error:"Command failed: 'npm test'\nReturn code: 1\nStderr: …\nStdout: …"`, `tool_output_text:"<tool_error>bash failed: Command failed: …</tool_error>"`. Replacement works the same; keep the `<tool_error>…</tool_error>` wrapper in the replacement so the model still sees a failure.
- `tool_status:"cancelled"` fires with the cancel text; pass through.
- post_tool fires **iff the tool body ran** (not after pre_tool deny, user denial, `NEVER`, or cancel-before-start).

**Replacement response** (stdout, exit 0):
```json
{"decision":"deny","reason":"<head…[subcortex: 18k chars elided, full log at /path]…tail>","system_message":"subcortex: compacted bash output (21k→2k chars)"}
```

**Source proof that this replaces without blocking/failing the call:**
- `_post_tool.py` `PostToolHandler._on_deny`: `final_text = reason` (plus `additional_context` if given) → yields `HookTextReplacement(text=final_text)` and `next_invocation = inv.model_copy(update={"tool_output_text": final_text})`, `should_break=False` ("Deny → replace `tool_output_text` with `reason`"; the pipeline continues).
- `agent_loop_hooks.py` `_run_post_tool_and_finalize`: `if isinstance(ev, HookTextReplacement): final_text = ev.text` … then `self._handle_tool_response(tool_call, final_text, finalization.response_status, …)` — the **status passed is the pre-hook `response_status`** (`"success"` for a successful call); the hook cannot change it.
- `_loop.py` `_handle_tool_response` builds the tool message from `text` (`format_handler.create_tool_response_message(tool_call, text)`) and appends it to `self.messages` → the model sees only `reason`.
- The UI `ToolResultEvent` (original output) is yielded **before** hooks run, so the user still sees the raw output; the hook's `HookEndEvent` shows `[subcortex-post-tool] <system_message>` (or "Replaced tool result (N chars)") at WARNING level.
- Unified `_interpret_post_tool_stdout`: `decision == "deny"` → `result.model_copy(update={"content": [text(reason)], "structured_content": None})` — the tool result object keeps its success/failure type; comment: "The tool already ran; a deny replaces the model-visible content with the reason rather than un-running the call."
- README: "`decision: "deny"` + `reason` — replaces `tool_output_text` with `reason`. Pipeline continues; subsequent hooks see the replacement."
- Caveats: `reason` empty/missing → legacy replaces with `""`, Unified with "The tool result was blocked by a post_tool hook." — always send a non-empty `reason`. `hook_specific_output.additional_context` alone = **append** (not replace). The persisted session record (`PersistedToolResult.output`, legacy) still stores the raw `tool_output` dict for resume/UI; whether a resumed session re-sends raw output to the model is UNVERIFIED.
- Passthrough when not compacting: **empty stdout**. (`{}` also parses as allow/no-op, but empty is canonical.)

### (c) Pre-compaction snapshot: ✗ (no event)
- Compaction is internal (`AutoCompactMiddleware`, `auto_compact_threshold`, `/compact`); no hook fires. The session log `messages.jsonl` is **overwritten** by compaction (`os.replace` in `session_logger.py`).
- Workaround (legacy only, ◐): keep a rolling snapshot. `post_agent` fires after every assistant turn that ends without tool calls and carries `transcript_path` (`$VIBE_HOME/logs/session/<prefix>…/messages.jsonl`, empty string if `session_logging.enabled=false`); read the tail there and exit 0 with **empty stdout** (never `deny`). Custom compaction prompt (`compaction_prompt_id`, `$VIBE_HOME/prompts/`) can ask the summariser to preserve specific facts (static).

### (d) Post-compaction re-inject: ✗ (no event)
- Workaround (◐): detect compaction (e.g. `messages.jsonl` shrank / changed head between `post_agent`/`post_tool` calls) and append the snapshot via `post_tool` `additional_context` on the next shell call. Append-only, shell-dependent.

## 5. Exit codes, timeouts, blocking responses to avoid

| Result | post_tool | pre_tool | post_agent |
|---|---|---|---|
| exit 0, empty | passthrough | allow | accept |
| exit 0, valid JSON | per fields | per fields | per fields |
| exit 0, junk / non-object JSON | failure path (UI warning; strict → clear) | failure (strict → **deny**) | failure → accept |
| non-zero exit (incl. 2) / timeout / spawn error | failure path | failure | failure → accept |

- Exit 2 has **no special meaning** (changed in the experimental era; "exit code 2 is treated as a failure"). stderr is only used as the failure reason text in the UI.
- Default timeout 60 s (float seconds); legacy kills the process tree via `kill_async_subprocess`.
- **Never emit:** `pre_tool` `{"decision":"deny"}` (denies the call); `pre_tool` `hook_specific_output.tool_input` (rewrites args; invalid rewrite synthesizes a denial); `post_agent` `{"decision":"deny"}` (forces a retry turn); `strict = true` on any hook.
- Our wrapper forces exit 0, so the only failure modes left are timeout and malformed stdout → UI warning and original output kept (fail-open).

## 6. MCP (`subcortex mcp`)

`$VIBE_HOME/config.toml` (default `~/.vibe/config.toml`), array of tables:
```toml
[[mcp_servers]]
name = "subcortex"
transport = "stdio"
command = "/abs/subcortex"
args = ["mcp"]
# optional: env = {}, cwd = "…", startup_timeout_sec = 10.0, tool_timeout_sec = 60.0,
#           prompt = "<usage hint appended to tool descriptions>", disabled = false, disabled_tools = []
```
- `MCPStdio.command` is `str | list[str]`; a string is **`shlex.split`** before `args` are appended → quote paths with spaces or use `command = ["/abs path/subcortex"]`.
- Non-interactive CLI (writes the user config):
  `vibe mcp add subcortex --transport stdio --command /abs/subcortex --arg mcp [--env K=V] [--startup-timeout-sec N] [--tool-timeout-sec N]`
  Idempotent if an identical entry exists; errors "MCP server name `subcortex` is already configured." if different → `vibe mcp remove subcortex` first. (`/mcp add` inside the TUI is OAuth/HTTP-only.)
- Vibe rewrites `config.toml` with `tomli_w` (comments and ordering are lost whenever Vibe saves config) — tag by `name = "subcortex"`, not by comment fences.
- Tool names: `subcortex_<tool>` (e.g. `subcortex_subcortex_decide`). Default tool permission is `ask` (`BaseToolConfig.permission`); auto-approve with `[tools.subcortex_subcortex_decide] permission = "always"` (and likewise for the other two).

## 7. Unverified / flagged

- Which harness a given user runs (rollout cache); test both with `--legacy-harness` / `--experimental-harness`.
- Unified harness on Windows shell names; Unified `tool_output_text` for bash is built from `content` text or `structured_content` "key: value" lines (exact format differs from legacy).
- Whether resumed sessions replay raw `tool_output` (persisted) rather than the replaced text to the model.
- `post_agent`-based rolling snapshot and `messages.jsonl` line format (not read in detail).
- Hooks in `vibe -p` / ACP / app-server: same loader (`load_hooks_from_fs`) is used, not executed here.
