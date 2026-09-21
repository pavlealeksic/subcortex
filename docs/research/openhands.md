# OpenHands CLI: subcortex adapter spec

Verified 2026-09-21 against upstream source: `OpenHands/OpenHands-CLI` at tag `1.16.0` and `main` (954f2ba), and `OpenHands/software-agent-sdk` at `v1.21.0` (the SDK the released CLI pins), `v1.28.1` (the SDK `main` pins, never released as a CLI) and `main`/`v1.49.2`. Code paths below are relative to those repos.

## Summary for adapter authors

- **The CLI is frozen.** `openhands` **1.16.0** (PyPI and GitHub, 2026-05-08) is the latest and last release. The README has said *"no longer actively maintained"* since 2026-08-11 (commit 954f2ba), and it recommends Agent Canvas (the `OpenHands/OpenHands` repo). The released 1.16.0 pins `openhands-sdk==1.21.0`, so **the SDK 1.21.0 hook engine is the contract.** `main` bumped to SDK 1.28.1 with no release. The hook semantics below are identical in 1.21.0, 1.28.1 and 1.49.2 unless noted.
- **Events (the only 6):** `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, `SessionStart`, `SessionEnd`. There is **no PreCompact, PostCompact or compaction hook**, and SessionStart has **no `source`**.
- **What each subcortex behavior can do:**
  - (1) Inject context on prompt submit: **supported** (`additionalContext` on `UserPromptSubmit`).
  - (2) Replace tool output: **impossible.** PostToolUse output is ignored, and PreToolUse cannot rewrite input.
  - (3) Pre-compact snapshot: **no event.** Emulate it by reading the persisted event log.
  - (4) Re-inject after compaction: **SessionStart output is ignored.** Emulate it through `UserPromptSubmit`.
- **Fail-open hazards specific to OpenHands:**
  - A malformed `hooks.json` raises an exception when the conversation is set up, which kills the session.
  - An unknown event key such as `PreCompact` or `pre_compact` is **rejected** and breaks the whole file.
  - `{"continue": <any falsy>}` blocks.
  - A failing MCP server breaks agent init.

---

## 1. Sources and versions

| Item | Value |
|---|---|
| Docs | https://docs.openhands.dev/openhands/usage/customization/hooks (source: `OpenHands/docs` `openhands/usage/customization/hooks.mdx`, last changed 2026-05-12) |
| CLI repo | https://github.com/OpenHands/OpenHands-CLI (`openhands_cli/setup.py`, `locations.py`, `mcp/mcp_utils.py`, `argparsers/mcp_parser.py`) |
| Hook engine | https://github.com/OpenHands/software-agent-sdk `openhands-sdk/openhands/sdk/hooks/{types,config,executor,manager,conversation_hooks}.py`, `conversation/impl/local_conversation.py` |
| PyPI | https://pypi.org/project/openhands/ (latest `1.16.0`, uploaded 2026-05-08; `requires_dist`: `openhands-sdk==1.21.0`, `openhands-tools==1.21.0`) |
| Latest CLI | **1.16.0**. No newer tag or release exists. `main` has only an SDK bump to 1.28.1 plus docs. The project is unmaintained. |
| Latest SDK | v1.49.2 (2026-09-17). It is **not** used by any released CLI. |
| Min CLI with hooks (TUI) | **1.12.0** (2026-02-09; commit 2d7842f *"Load hooks from ~/.openhands/hooks.json in CLI (#428)"*; pins SDK 1.11.1). Hooks first appeared in SDK v1.8.0 (d103b6a, 2025-12-25). |
| Min CLI with `async` hooks | **1.13.0** (SDK 1.11.5 contains 050991d). In 1.12.x the `async` key is silently ignored, so the hook runs synchronously. |
| `name` field on a hook | Only in SDK ≥ the 2026-05-27 commit (c634794): present in 1.28.1, **absent in shipped 1.21.0** (ignored as an extra key there). |
| Version detection | `openhands --version` or `-v` prints `OpenHands CLI 1.16.0` (argparse `action="version"`, `version=f"OpenHands CLI {__version__}"`, `__version__ = importlib.metadata.version("openhands")`). Parse `/OpenHands CLI (\d+\.\d+\.\d+)/`. Alternative: `uv tool list` or `pip show openhands`. The SDK version is not printed. Infer it from the CLI version (1.12→1.11.1, 1.13→1.11.5, 1.14→1.16.1, 1.15→1.17.0, 1.16→1.21.0). |
| Binaries | `openhands` (TUI and subcommands) and `openhands-acp`. Install via `uv tool install openhands --python 3.12` or `curl -fsSL https://install.openhands.dev/install.sh \| sh` (a PyInstaller binary). |

## 2. Config paths, format, relocation, trust

**Hooks file: exactly ONE file is loaded, and it is not a merge.** `HookConfig.load(working_dir=get_work_dir())` in SDK 1.21.0 `config.py`:

```python
search_paths = [base_dir/".openhands"/"hooks.json", Path.home()/".openhands"/"hooks.json"]
for p in search_paths:
    if p.exists(): path = p; break      # FIRST FOUND WINS
```

- Project: `<workdir>/.openhands/hooks.json`, where `<workdir>` is `$OPENHANDS_WORK_DIR` or the cwd.
- User: `~/.openhands/hooks.json` (uses `Path.home()`, i.e. `$HOME`).
- **If a project `.openhands/hooks.json` exists, the user-level file is IGNORED entirely.** A user-level subcortex install silently does nothing in repos that ship their own hooks.json. Both the installer and `doctor` must detect this and warn, or offer a per-project install.
- The file is loaded **once per conversation**, when the conversation is set up (in the TUI that is the first message or a resume). There is no hot reload. Edits apply to the next conversation.
- SDK `main` (not shipped) replaces the home path with `get_user_persistence_dir()/hooks.json`, which honors `OH_PERSISTENCE_DIR`. This does not apply to 1.16.0.

**Format** (JSON; `HookConfig` has `model_config = {"extra": "forbid"}`):

- Canonical: snake_case top-level keys `pre_tool_use`, `post_tool_use`, `user_prompt_submit`, `session_start`, `session_end`, `stop`.
- Also accepted: PascalCase keys (`PreToolUse`, …) and the Claude-style wrapper `{"hooks": {...}}`. If the wrapper has extra sibling keys, the SDK logs a warning and ignores them.
- **Fatal file errors.** Each of the following raises `ValueError` or `ValidationError`, and so does invalid JSON (`json.JSONDecodeError`):
  - an unknown PascalCase key (`"Unknown event type 'PreCompact'…"`);
  - an unknown snake_case key (extra is forbidden);
  - both `PreToolUse` and `pre_tool_use` present (`"Duplicate hook event"`).
- **How fatal errors surface.** `setup_conversation()` (`openhands_cli/setup.py`) calls `HookConfig.load` **without try/except**. The call chain (`ConversationRunner.__init__` ← `RunnerFactory.create` ← `RunnerRegistry.get_or_create`) catches nothing, so **a bad hooks.json breaks conversation creation.** The installer MUST:
  - write valid JSON only;
  - use only the 6 keys;
  - keep the existing key style (reuse whichever form already exists, and never add the other case form);
  - write atomically (temp file plus rename).
- Matcher group: `{"matcher": str = "*", "hooks": [HookDefinition]}`.
- `HookDefinition` (1.21.0) fields:
  - `type` (`"command"`; `"prompt"` falls open);
  - `command` (required string);
  - `timeout` (int, **seconds**, default **60**);
  - `async` (bool, default false).
  - In 1.28.1+ also `name`, `prompt`, `system_prompt`, `tools`, `max_iterations`, and `type: "agent"`.
  - Extra keys on a definition or matcher are **ignored**: pydantic's default is `extra="ignore"`, and only `HookConfig` forbids extras.

**Matcher semantics** (`HookMatcher.matches`, same in all versions):

- `"*"` or `""` matches everything.
- `/re/` is `re.fullmatch(re, tool_name)`.
- A string containing any of `|.*+?[]()^$\` is auto-treated as a regex with **fullmatch**; if the regex is invalid, it falls back to exact match.
- Anything else is an exact string match.
- **For non-tool events** (`UserPromptSubmit`, `SessionStart`, `SessionEnd`, `Stop`), `tool_name` is None, so only `"*"` or `""` (or an omitted matcher) matches. Any other matcher means the hook **never runs**.
- Tool names in the CLI's default toolset (`openhands_cli/utils.py:get_default_cli_tools`) are `terminal`, `file_editor` and `task_tracker`, plus the sub-agent task tool (legacy name `delegate`). MCP tools use the raw MCP tool name, with no server prefix.

**Other CLI data** (`openhands_cli/locations.py`):

| What | Default | Env override |
|---|---|---|
| Persistence dir (`agent_settings.json`, `cli_config.json`, `mcp.json`, `projects/<sha256(workdir)>/prompt_history.json`) | `~/.openhands` | `OPENHANDS_PERSISTENCE_DIR` |
| Conversations (`<id.hex>/base_state.json`, `<id.hex>/events/event-00000-<eventid>.json`) | `$PERSISTENCE/conversations` | `OPENHANDS_CONVERSATIONS_DIR` |
| Work dir (hook cwd, project hooks lookup) | cwd | `OPENHANDS_WORK_DIR` |
| User hooks.json | `$HOME/.openhands/hooks.json` | **only `HOME`** (1.16.0) |

**Isolated e2e run:**

```sh
T=$(mktemp -d)
HOME=$T OPENHANDS_PERSISTENCE_DIR=$T/.openhands \
LLM_API_KEY=... LLM_MODEL=... \
openhands --override-with-envs --headless --json -t "..."
```

- `--headless` requires `-t` or `-f` and auto-approves actions.
- `--json` streams events as JSONL. That stream should include `HookExecutionEvent` records (fields `hook_event_type`, `hook_command`, `success`, `blocked`, `exit_code`, `stdout`, `stderr`, `additional_context`, `error`), which are useful for assertions. Whether the JSON callback emits them is **unverified**.
- The LLM env vars are `LLM_API_KEY`, `LLM_MODEL` and `LLM_BASE_URL`.

**Trust / enable:** there is no trust prompt and no enable flag. The CLI has no "trust" code path. Any `hooks.json` found runs automatically. On load the TUI prints `✓ Hooks loaded`, and `/skills` lists the loaded hooks. The CLI has no plugin mechanism: the SDK's plugin hooks (`plugins=`) are not wired by the CLI.

## 3. Registration snippet (user level `~/.openhands/hooks.json`)

```json
{
  "user_prompt_submit": [
    { "matcher": "*", "hooks": [
      { "type": "command", "name": "subcortex",
        "command": "/usr/local/bin/subcortex hook openhands user-prompt-submit", "timeout": 10 } ] }
  ],
  "post_tool_use": [
    { "matcher": "*", "hooks": [
      { "type": "command", "name": "subcortex",
        "command": "/usr/local/bin/subcortex hook openhands post-tool-use", "timeout": 10, "async": true } ] }
  ],
  "session_start": [
    { "matcher": "*", "hooks": [
      { "type": "command", "name": "subcortex",
        "command": "/usr/local/bin/subcortex hook openhands session-start", "timeout": 10 } ] }
  ]
}
```

- **Timeout units:** integer seconds. Default 60. Use integers, because `5.5` fails validation and that is fatal to the whole file.
- **Always use an absolute path** to `subcortex`. The command runs through `subprocess.run(command, shell=True)`, i.e. `/bin/sh -c`. If the binary is missing, sh exits 127, which is a non-blocking error.
- **`post_tool_use` with `"async": true`:** the process is fire-and-forget, with stdout and stderr sent to DEVNULL. It adds no latency, and it is killed when its timeout expires (process group SIGTERM, then SIGKILL) or at SessionEnd.
  - Without `async`, PostToolUse runs **synchronously** inside the event callback, while the conversation state lock is held, so it stalls the agent until the hook exits or the timeout hits.
  - On CLI 1.12.x the `async` key is ignored.
- **Do not register `stop` or `pre_tool_use`.** They are blocking events, subcortex gains nothing from them, and they add risk.
- **Tagging and uninstall.** The shipped SDK has no stable id field: `name` is ignored in 1.21.0 but is a real field in 1.28+. Identify our entries by the **command prefix** `<abs>/subcortex hook openhands ` (and `name == "subcortex"` when present). Uninstall:
  1. Remove every hook definition whose `command` matches.
  2. Drop matcher groups whose `hooks` list became empty.
  3. Drop event keys whose list became empty.
  4. If the result is `{}` and our install state says we created the file, delete it. Otherwise leave `{}`, which is valid.
  5. Preserve the user's key style and wrapper.
- **Project level:** the same JSON goes in `<repo>/.openhands/hooks.json`. Merge only when the user explicitly consents, because this file is usually committed.

## 4. Events: names, payloads, responses

The hook runs through `/bin/sh -c`, with cwd = the work dir. **stdin** is `HookEvent.model_dump_json()`: all 8 keys are always present, null when unused, with no trailing newline. Environment (a copy of the CLI environment, `sanitized_env()`, which strips `SESSION_API_KEY` and similar and restores `LD_LIBRARY_PATH` for PyInstaller builds):

- `OPENHANDS_PROJECT_DIR`
- `OPENHANDS_SESSION_ID`
- `OPENHANDS_EVENT_TYPE`
- `OPENHANDS_TOOL_NAME` (tool events only)

`session_id` is `str(UUID)` **with dashes**. The on-disk conversation dir uses `uuid.hex` (**no dashes**).

| subcortex behavior | OpenHands event | Supported? |
|---|---|---|
| prompt-submit | `UserPromptSubmit` / `user_prompt_submit` | **yes**: `additionalContext` |
| after-tool | `PostToolUse` / `post_tool_use` | observe only; **replacement impossible** |
| pre-compact | none | **no event** (emulate; see 4c) |
| session-start (post-compact) | `SessionStart` / `session_start` | runs, but **output ignored**; no `source` |

### 4a. UserPromptSubmit (context injection: works)

It fires for every `MessageEvent` with `source == "user"`, including queued messages. It does not fire for Stop-hook feedback (`source == "environment"`). It runs synchronously and **before** the LLM call.

```json
{"event_type":"UserPromptSubmit","tool_name":null,"tool_input":null,"tool_response":null,"message":"fix the typo in README.md","session_id":"4f1c2a9e-8b7d-4e21-9c3a-0d5e6f7a8b9c","working_dir":"/Users/me/proj","metadata":{}}
```

**Response to inject:** exit 0 with stdout exactly one JSON object:

```json
{"additionalContext":"[subcortex] Simple request: prefer the most direct, minimal path."}
```

- The value is converted with `str()`. When several hooks inject, their values are joined with `\n`.
- The SDK appends it as a `TextContent` to the user `MessageEvent.extended_content`. `MessageEvent.to_llm_message` appends `extended_content` to the user message content, so the model sees it as part of that user turn. The modified event is the one that gets persisted.
- Plain-text stdout is **not** injected. A `JSONDecodeError` is swallowed, so non-JSON stdout is simply ignored.
- **To inject nothing:** exit 0 with empty stdout, or with `{}`.

### 4b. PostToolUse (tool-output replacement: IMPOSSIBLE)

It fires after the `ObservationEvent` has been produced, and only if the matching `ActionEvent` is found in state. `tool_input` is `action.model_dump()` and `tool_response` is `observation.model_dump()`, both including the computed `kind`.

```json
{"event_type":"PostToolUse","tool_name":"terminal",
 "tool_input":{"command":"npm test 2>&1","is_input":false,"timeout":null,"reset":false,"kind":"TerminalAction"},
 "tool_response":{"content":[{"cache_prompt":false,"type":"text","text":"> proj@1.0.0 test\n> jest\n...PASS src/a.test.ts\n"}],
   "is_error":false,"command":"npm test 2>&1","exit_code":0,"timeout":false,
   "metadata":{"exit_code":0,"pid":-1,"username":"me","hostname":"mbp","working_dir":"/Users/me/proj","py_interpreter_path":null,"prefix":"","suffix":""},
   "full_output_save_dir":"/Users/me/.openhands/conversations/4f1c2a9e8b7d4e219c3a0d5e6f7a8b9c/observations","kind":"TerminalObservation"},
 "message":null,"session_id":"4f1c2a9e-8b7d-4e21-9c3a-0d5e6f7a8b9c","working_dir":"/Users/me/proj","metadata":{}}
```

The field order and the `full_output_save_dir` value are illustrative. The field names come from `TerminalAction`, `TerminalObservation` and `CmdOutputMetadata` in openhands-tools 1.21.0.

- **Replace, block or append: none is possible.** `run_post_tool_use` runs with `stop_on_block=False`, and its results are only logged and emitted as `HookExecutionEvent`. `additionalContext`, `decision`, `continue` and exit 2 on PostToolUse change nothing the model sees. The observation is already final and immutable.
- **PreToolUse cannot help either.** It supports only allow/deny/continue; it has no `updated_input`, so we cannot rewrite `cmd` into `cmd | subcortex-trim`.
- **Native mitigation:** the terminal tool already truncates observations above **30,000 characters** (`MAX_CMD_OUTPUT_SIZE`). It keeps the head and tail, inserts a notice, and saves the full output to `<conversation>/observations/terminal_output_<hash>.txt` (`maybe_truncate`). This is effectively the built-in version of subcortex behavior 2. The adapter should therefore **not** attempt behavior 2 on OpenHands, and should report it as "native".
- Use PostToolUse (async) only for telemetry or the rolling snapshot.

### 4c. Pre-compact (no event)

- **What compaction is here:** the condenser (`LLMSummarizingCondenser(max_size=80, keep_first=4)` by default; also the TUI `/condense` command) runs inside the agent step. It emits a persisted `Condensation` event (`kind: "Condensation"`, with `forgotten_event_ids`, `summary`, `summary_offset`, `llm_response_id`). No hook fires.
- **Emulation: no pre-hook is needed.** The event log is append-only. Condensation only *hides* events from the LLM view; the event files stay on disk at `$OPENHANDS_CONVERSATIONS_DIR` (default `$OPENHANDS_PERSISTENCE_DIR/conversations`, i.e. `~/.openhands/conversations`), under `<session_id without dashes>/events/event-NNNNN-*.json`. The hook process inherits these env vars. The snapshot can therefore be rebuilt **after the fact**, from the events that precede the newest `Condensation`.
- If an explicit pre-snapshot is still wanted, maintain a rolling "last N messages" file from the async PostToolUse hook and from UserPromptSubmit. That is best-effort and never vetoes anything.

### 4d. SessionStart (re-injection: output IGNORED)

It runs **once per `LocalConversation` object**, lazily in `_ensure_plugins_loaded()`, on the first `send_message()` / `run()`. In the TUI that means just before the first user message is processed, and again on `--resume` or a conversation switch. The results are only logged and emitted. **`additionalContext` is not injected**, and exit 2 or deny is ignored. The payload has no `source` or compaction flag. `matcher` must be `"*"`.

```json
{"event_type":"SessionStart","tool_name":null,"tool_input":null,"tool_response":null,"message":null,"session_id":"4f1c2a9e-8b7d-4e21-9c3a-0d5e6f7a8b9c","working_dir":"/Users/me/proj","metadata":{}}
```

**Recommended re-injection path:** in the `UserPromptSubmit` handler, list `<conv>/events/`, find the newest `kind == "Condensation"` event, and compare its id or index with a marker stored in subcortex state (keyed by session_id). If it is new, emit `{"additionalContext":"<snapshot>"}` once and advance the marker.

- **Limitation:** a condensation that happens mid-turn, between tool calls, is only compensated at the *next* user prompt.
- SessionStart may still be registered as a no-op (exit 0, empty stdout) to record session starts, or it can be omitted.

Other payloads, for reference only (never register them):

- Stop: `metadata: {"reason":"agent_finished"}`.
- PreToolUse: same shape as PostToolUse, with `tool_response: null`.
- SessionEnd: runs on `conversation.close()`.

## 5. Exit codes, output parsing, and every blocking response

Parsing is in `HookExecutor.execute` (SDK 1.21.0, identical in 1.28.1 and main):

```python
result = subprocess.run(command, shell=True, cwd=wd, env=env, input=event_json,
                        capture_output=True, text=True, timeout=hook.timeout)
hr = HookResult(success=rc==0, blocked=rc==2, exit_code=rc, stdout=..., stderr=...)
if stdout.strip():
  try: d=json.loads(stdout)            # WHOLE stdout must be one JSON value
       if dict: "decision"→.lower()=="allow"|"deny"(sets blocked) ; "reason"; "additionalContext"; 
                "continue": if not d["continue"]: blocked=True
  except JSONDecodeError: pass
TimeoutExpired / FileNotFoundError / any Exception → HookResult(success=False, exit_code=-1, error=...)  # not blocked
should_continue = not blocked and decision != DENY
```

| Outcome | Effect |
|---|---|
| exit 0, empty stdout | success, no-op (**the adapter's default no-op**) |
| exit 0, `{"additionalContext": "..."}` | success. Injected on UserPromptSubmit (and on a blocked Stop as feedback). Ignored elsewhere. |
| exit 0, non-JSON or partial JSON stdout | JSON error swallowed. Treated as success with no fields. |
| exit 0, JSON that is not an object (array, string, number) | ignored |
| exit 0, `"decision"` with a non-string value (e.g. null) | `.lower()` raises AttributeError. Caught as `success=False`, not blocked (falls open, but avoid it). |
| exit 1 or any other non-zero except 2 | `success=False`. Logged as a non-blocking error. The operation proceeds. stdout JSON **is still parsed**, so a `deny` printed with exit 1 still blocks. |
| **exit 2** | **blocked = True** regardless of stdout. `{"decision":"allow"}` does **not** un-block it. |
| timeout | the child is killed (`subprocess.run` kills the shell). `success=False`, not blocked. Default timeout 60 s. |
| command not found, spawn error | `success=False`, not blocked |
| stderr | never parsed. Used only as the block reason when blocked and no `reason` is given, and shown in logs and the `HookExecutionEvent` (truncated to 50k). |

Multiple hooks for one event run **sequentially** in config order (`execute_all`), not in parallel. For PreToolUse, UserPromptSubmit and Stop the chain stops at the first blocking hook. Every run is persisted as a `HookExecutionEvent` (stdout and stderr truncated at 50,000 chars), so keep stdout small.

**Every response that blocks, denies or stops. The adapter MUST never emit any of these, on any event:**

1. **Exit code `2`.** It blocks a PreToolUse tool call. On UserPromptSubmit the message is blocked and skipped: the model is never called. On Stop, the agent is **forced to keep running**, which can loop.
2. **`"decision": "deny"`** (case-insensitive) in stdout JSON, **with any exit code**.
3. **A `"continue"` key with any falsy value:** `false`, `null`, `0`, `""`, `[]` or `{}`. The rule is `if not d["continue"]: blocked=True`, with any exit code. **Never emit a `continue` key at all.**
4. Async hooks can never block (their stdout goes to DEVNULL). Blocking results on PostToolUse, SessionStart and SessionEnd are recorded but not acted on. The adapter still emits none of the above on those events.

Also avoid:

- `"decision": "allow"` on PreToolUse. It is not a block, but it pre-empts nothing because confirmation is separate.
- `"reason"` without need. It is shown in the UI when blocked.

**Adapter rules:**

- Always exit 0.
- Write stdout only as a single compact JSON object `{"additionalContext": "..."}` (UserPromptSubmit only), or nothing.
- Put diagnostics on stderr.
- Wrap everything in catch-all handlers.
- Hard internal deadline below the configured timeout (for example 3 s against `timeout: 10`).
- Never print partial JSON.

## 6. MCP server registration (fallback `subcortex mcp`)

**File:** `$OPENHANDS_PERSISTENCE_DIR/mcp.json` (default `~/.openhands/mcp.json`). It is read by `openhands_cli/mcp/mcp_utils.py` via `fastmcp.mcp_config.MCPConfig`. The CLI loads only servers with `enabled != false` (the default is true) into `agent.mcp_config` when the agent is built, so a change applies to the **next** conversation.

```json
{
  "mcpServers": {
    "subcortex": {
      "command": "/usr/local/bin/subcortex",
      "args": ["mcp"],
      "env": {},
      "transport": "stdio",
      "enabled": true
    }
  }
}
```

CLI equivalent (argparse; `args` is `REMAINDER` after the positional target):

```sh
openhands mcp add subcortex --transport stdio /usr/local/bin/subcortex mcp
openhands mcp list | get subcortex | disable subcortex | enable subcortex | remove subcortex
```

- `mcp add` errors if the name already exists (`"MCP server 'subcortex' already exists"`, exit 1), so remove the entry first to update it.
- `--env KEY=value` is repeatable.
- The docs' example puts `--` before args (`python -- -m ...`). Avoid it for `mcp`: whether argparse strips `--` from REMAINDER differs across Python 3.12 patch versions (unverified).
- For uninstall, the key `subcortex` in `mcpServers` is the tag.
- Tools appear under their raw MCP names (no `mcp__server__` prefix).

**Hazard: MCP is NOT fail-open.**

- SDK `Agent` init calls `create_mcp_tools(self.mcp_config, 30)` and then `future.result()`. A server that fails to spawn, crashes, or takes longer than 30 s to `initialize` plus `tools/list` raises `MCPTimeoutError` or the underlying exception, which aborts agent initialization for that conversation.
- `subcortex mcp` must therefore start fast, never exit early, and answer `tools/list` even when the daemon is down.
- `doctor` should verify that the binary path exists before writing `mcp.json`.

## 7. Unverified or flagged claims

- **Unmaintained product.** No CLI release since 1.16.0 (2026-05-08). Anything newer in the SDK (1.28–1.49) will not reach CLI users unless a new release is cut. If a user runs a source build of `main`, they get SDK 1.28.1: same semantics, plus a `name` field.
- **Crash on bad hooks.json.** The exception from `HookConfig.load` inside `setup_conversation` is not caught in the CLI code paths I traced. I did not reproduce the exact UX (Textual error screen, crash, or notification).
- **HookExecutionEvent visibility.** The CLI TUI has no dedicated renderer for it: `git grep HookExecutionEvent` on the CLI finds nothing. Whether it is shown through a generic fallback, and whether `--json` headless mode streams it, is unverified.
- **Sample shapes.** Exact `model_dump()` key order, the extra keys in `tool_input` and `tool_response` for non-terminal tools, and `full_output_save_dir` paths are derived from the models, not from captured output. Capture real payloads in e2e (for example `cat > $T/payload.json` as the hook command).
- **Terminal truncation file location.** `<conv>/observations/` is now verified: `ConversationState.env_observation_persistence_dir` returns `persistence_dir/"observations"`.
- **Condensation emulation.** The persisted event file naming (`event-%05d-<id>.json`) and the `kind` discriminator are verified from source. The claim that forgotten events remain on disk after condensation rests on EventLog being append-only; I did not observe it at runtime.
- **SessionStart timing.** The claim that SessionStart runs at the first message rather than at TUI launch comes from SDK code (lazy `_ensure_agent_ready` in `send_message`). It was not observed in the TUI.
- **`--` handling.** `openhands mcp add ... -- args` behavior with argparse `REMAINDER` is unverified.
- **Docs gaps.** The docs page does not mention the user-level `~/.openhands/hooks.json` fallback, "first file wins", fatal unknown keys, `continue`, or that SessionStart and PostToolUse outputs are ignored. All of these come from source.
- **Agent Canvas** (the recommended successor, `OpenHands/OpenHands`) was not evaluated. It likely uses the same SDK hook engine and `.openhands/hooks.json`, but that is unverified.
