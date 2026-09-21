# Claude Code: subcortex hook adapter spec (verified September 2026)

Status legend: **[doc]** quoted or paraphrased from official docs · **[src]** read in source · **[obs]** observed in real transcripts on this machine (Claude Code 2.1.229 to 2.1.273, used only to confirm schemas) · **[unverified]** inferred and needs an e2e check.

---

## 1. Sources, versions, detection

| What | Value |
|---|---|
| Latest release | **2.1.278** (GitHub `anthropics/claude-code` tag `v2.1.278`, 2026-09-19; npm `@anthropic-ai/claude-code@2.1.278`) |
| **Minimum version for all 4 behaviors** | **2.1.121** (2026-04-28). Changelog: "PostToolUse hooks can now replace tool output for all tools via `hookSpecificOutput.updatedToolOutput` (previously MCP-only)". Before that, only `updatedMCPToolOutput` existed, so built-in Bash output could NOT be replaced. |
| Other relevant version gates | UserPromptSubmit `additionalContext` 1.0.59 · PostCompact 2.1.76 · PreCompact can block (exit 2 or `decision:block`) 2.1.105 · hook `args` (exec form) 2.1.139 · exit 2 + invalid JSON now blocks 2.1.214 · unparseable `{…}` stdout is an error, no longer plain text 2.1.248 |
| Version detection | `claude --version` prints `2.1.278 (Claude Code)`. Parse the leading semver. |

Sources:
- https://code.claude.com/docs/en/hooks (`.md` suffix returns raw markdown): the full reference; quoted below
- https://code.claude.com/docs/en/hooks-guide
- https://code.claude.com/docs/en/settings · https://code.claude.com/docs/en/cli-reference · https://code.claude.com/docs/en/headless
- https://code.claude.com/docs/en/env-vars · https://code.claude.com/docs/en/authentication · https://code.claude.com/docs/en/sessions
- https://code.claude.com/docs/en/tools-reference (Bash output limits) · https://code.claude.com/docs/en/mcp
- https://code.claude.com/docs/en/changelog · https://github.com/anthropics/claude-code/releases/tag/v2.1.278

---

## 2. Config paths, env relocation, trust

| Scope | Path |
|---|---|
| User | `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`) |
| Project, shared | `<repo>/.claude/settings.json` |
| Project, local | `<repo>/.claude/settings.local.json` |
| Per run | `claude --settings <file-or-inline-json>` |
| Plugin | `<plugin>/hooks/hooks.json`, loaded with `--plugin-dir <path>` for one run |

- **Hooks merge across levels** [doc]: "Hook entries merge across settings levels rather than replacing each other". "If you define the same handler in more than one settings file, it runs once."
- **`CLAUDE_CONFIG_DIR`** [doc]: "Override the configuration directory (default: `~/.claude`). All settings, session history, and plugins are stored under this path." Credentials are relocated too: `.credentials.json` moves under that dir, and "keys the macOS Keychain entry to that directory too". **An isolated `CLAUDE_CONFIG_DIR` therefore needs its own login or `ANTHROPIC_API_KEY`.** `~/.claude.json` (MCP user/local scope) also lives under it [unverified path: `$CLAUDE_CONFIG_DIR/.claude.json`].
- **`--settings <file|json>`** [doc]: "Values you set here override the same keys in your settings.json files for this session." It sits above user, project and local settings and below managed settings. Because hooks merge, **a `hooks` block in `--settings` adds hooks for that single run**, on top of any user/project hooks.
- **Fully isolated e2e** [doc]: `--bare` skips "auto-discovery of hooks, skills, … plugins, MCP servers, … CLAUDE.md". The headless docs list `--settings <file-or-json>` as the way to load settings in bare mode. Bare mode never reads OAuth or the Keychain, so `ANTHROPIC_API_KEY` is required. [unverified: that `--bare` honours `hooks` inside `--settings`. The docs imply it; run the smoke test below.]
- **Workspace trust** [doc]: "**Interactive session**: Claude Code holds back hooks from every settings file, including your own `~/.claude/settings.json`, until you accept the workspace trust dialog for the folder". "**`-p` or SDK session**: … treats the folder as trusted". There is no per-hook trust or hash, unlike Codex.
- Kill switches: `"disableAllHooks": true` (one run: `--settings '{"disableAllHooks":true}'`). Managed `allowManagedHooksOnly` blocks user/project hooks.
- Settings validation [doc]: invalid JSON in a user file produces a "Settings Error" dialog (interactive). "only individual entries fail, such as … an unknown hook event name" produces a warning and the rest stays in effect. **Do not add custom keys to hook handler objects** for tagging.

E2E recipes:
```bash
# A) Your normal login, hooks for this run only (user hooks also run)
claude -p "echo hi via Bash" --settings /tmp/sc-e2e/settings.json \
  --output-format stream-json --verbose --include-hook-events --debug-file /tmp/sc-e2e/cc.log
# B) Hermetic (API key)
ANTHROPIC_API_KEY=... claude --bare -p "..." --settings /tmp/sc-e2e/settings.json --allowedTools Bash
# C) Hermetic via relocated home (needs login or API key inside it)
CLAUDE_CONFIG_DIR=/tmp/sc-e2e/cchome claude -p "..." --settings /tmp/sc-e2e/settings.json
# Force early auto-compaction for PreCompact/SessionStart(compact) tests:
CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=1  # "Use lower values … to compact earlier"
```
`--include-hook-events` does not produce `hook_started` for PreCompact/PostCompact [doc]. Confirm those through `--debug-file` or the snapshot file on disk.

---

## 3. Registration (settings.json)

Three levels: event → matcher group → handler. `timeout` is in **seconds** [doc]. Defaults: 600 s for `command`, 30 s on UserPromptSubmit, and the handler is cancelled at timeout with its output discarded. Matcher [doc]: `"*"`, `""` or omitted matches everything. A value containing only `[A-Za-z0-9_\- ,|]` is an exact string or list (`|` or `,`). Anything else is an unanchored JS regex. UserPromptSubmit has **no matcher support** ("silently ignored"). SessionStart matches `source`, PreCompact matches `trigger` (`manual`/`auto`).

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "timeout": 10,
        "command": "'/opt/homebrew/bin/subcortex' hook claude-code UserPromptSubmit || true" } ] }
    ],
    "PostToolUse": [
      { "matcher": "Bash", "hooks": [ { "type": "command", "timeout": 10,
        "command": "'/opt/homebrew/bin/subcortex' hook claude-code PostToolUse || true" } ] }
    ],
    "PreCompact": [
      { "matcher": "manual|auto", "hooks": [ { "type": "command", "timeout": 10,
        "command": "'/opt/homebrew/bin/subcortex' hook claude-code PreCompact || true" } ] }
    ],
    "SessionStart": [
      { "matcher": "compact", "hooks": [ { "type": "command", "timeout": 10,
        "command": "'/opt/homebrew/bin/subcortex' hook claude-code SessionStart || true" } ] }
    ]
  }
}
```
- **Shell form plus `|| true`** is deliberate. Without `args`, the command runs under `sh -c` [doc], so `|| true` forces exit 0 even if Python, argparse or the import chain fails. That neutralises the exit-2 failure mode. Stdout still passes through. Exec form (`"command": "/abs/subcortex", "args": ["hook","claude-code","PostToolUse"]`) avoids quoting problems but has no shell, so exit codes pass straight through. Use it only if the CLI guarantees exit 0 (section 7).
- Quote the executable path with `shlex.quote`, because paths with spaces break shell form.
- **Tagging for exact uninstall**: there is no id field, and custom keys risk validation warnings. Identify our handlers by the command string only, using this regex: `(?:^|[\s/'"])subcortex['"]?\s+hook\s+claude(?:-code)?\s+(UserPromptSubmit|PostToolUse|PreCompact|SessionStart)\b` (also allow a `-m subcortex hook …` fallback). A stronger option: record the exact command strings written at install time in `~/.local/share/subcortex/installed.json` and remove only exact matches. `statusMessage: "subcortex"` is a documented optional field. It could serve as a secondary tag, but it shows as a spinner label on every Bash call.
- Alternative packaging: a Claude Code plugin (`hooks/hooks.json` plus `.mcp.json`) can be enabled or disabled as a unit, and `--plugin-dir` tests it for one run.

---

## 4. Events: input, output, and exactly what each response does

Common stdin fields [doc]: `session_id`, `prompt_id` (≥2.1.196), `transcript_path`, `cwd`, `scratchpad_dir` (≥2.1.257), `permission_mode` (not on all events), `effort` (tool-context events), `hook_event_name`. Inside subagents: `agent_id`, `agent_type`.

Stdout parsing [doc]: output that "Starts with `{` and ends with `}`" is parsed as JSON. Anything else is plain text. **On UserPromptSubmit, UserPromptExpansion, SessionStart and PostModelSwitch, plain-text stdout is ADDED TO CLAUDE'S CONTEXT.** A stray `print()` or library warning on stdout therefore becomes model context. JSON that fails schema validation is a non-blocking "hook error" notice, and the context is not added. The strings `additionalContext`, `systemMessage` and plain stdout are each capped at 10,000 chars (anything over is spilled to a file and replaced by a 2,000-char preview).

`additionalContext` [doc]: "Claude Code wraps the string in a system reminder and inserts it into the conversation at the point where the hook fired." Guidance: "Write the text as factual statements rather than imperative system instructions … Text framed as out-of-band system commands can trigger Claude's prompt-injection defenses."

### 4.1 UserPromptSubmit (behavior 1: context hint)
stdin:
```json
{"session_id":"8f1c2a3e-5b7d-4c9e-a1f0-2d3b4c5e6f70","prompt_id":"550e8400-e29b-41d4-a716-446655440000",
 "transcript_path":"/Users/me/.claude/projects/-Users-me-proj/8f1c2a3e-5b7d-4c9e-a1f0-2d3b4c5e6f70.jsonl",
 "cwd":"/Users/me/proj","permission_mode":"default","hook_event_name":"UserPromptSubmit",
 "prompt":"rename foo to bar in utils.py"}
```
(a) context injection, the only thing we emit:
```json
{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit",
  "additionalContext":"[subcortex] A local classifier rated this request as simple (confidence 0.91); the most direct, minimal change is likely sufficient."}}
```
Never emit: `decision:"block"` ("prevents the prompt from being processed and erases it from context"), `continue:false`, or exit 2 ("Blocks prompt processing and erases the prompt"). The prompt itself cannot be rewritten [doc].

### 4.2 PostToolUse (behavior 2: output replacement), **REPLACE is possible**
Fires only after a *successful* tool call. Failures go to `PostToolUseFailure` [doc]. For Bash, "success" includes exit 1 from `grep/rg/find/diff/test/[`/`git diff`/`git grep` [doc tools-reference].

stdin (Bash). The `tool_response` keys are **observed** [obs]: `stdout, stderr, interrupted, isImage, noOutputExpected` plus optional `returnCodeInterpretation`, `persistedOutputPath`, `persistedOutputSize`, `backgroundTaskId`, `backgroundCwdHint`, `bashEditDiff`, `gitOperation`, `timedOutAfterMs`. **There is no exit-code field.**
```json
{"session_id":"8f1c2a3e-…","transcript_path":"/Users/me/.claude/projects/-Users-me-proj/8f1c2a3e-….jsonl",
 "cwd":"/Users/me/proj","permission_mode":"default","hook_event_name":"PostToolUse",
 "tool_name":"Bash","tool_input":{"command":"npm ls --all","description":"List deps"},
 "tool_response":{"stdout":"proj@1.0.0 /Users/me/proj\n├── …(18,000 chars)…","stderr":"","interrupted":false,"isImage":false,"noOutputExpected":false},
 "tool_use_id":"toolu_01ABC","duration_ms":812}
```
Answer to the key doubt, quoted from the PostToolUse decision-control table [doc]:
- `decision`: "`"block"` adds the `reason` next to the tool result. **Claude still sees the original output; to replace it, use `updatedToolOutput`**"
- `updatedToolOutput`: "Replaces the tool's output with the provided value before it is sent to Claude. The value must match the tool's output shape"
- `updatedMCPToolOutput`: "Replaces the output for MCP tools only. Prefer `updatedToolOutput`, which works for all tools"
- Warning box: "Built-in tools return structured objects … `Bash` returns an object with `stdout`, `stderr`, `interrupted`, and `isImage` fields. For built-in tools, a value that doesn't match the tool's output schema is **ignored and the original output is used**."

(b) replacement. Copy the whole `tool_response` dict and change only `stdout`:
```json
{"hookSpecificOutput":{"hookEventName":"PostToolUse",
  "updatedToolOutput":{"stdout":"<first 1000 chars>\n\n[subcortex: trimmed 16500 chars of low-value output; re-run the command if you need the rest]\n\n<last 500 chars>",
    "stderr":"","interrupted":false,"isImage":false,"noOutputExpected":false}}}
```
Skip replacement when: `persistedOutputPath` is present (Claude Code already sent a file path plus a 2,000-char preview; `stdout` is capped at about 30,000 chars [obs]), `backgroundTaskId` is present, `isImage` is true, or `interrupted` is true. Built-in inline cap: valid Bash results over about 30,000 chars are already spilled to a file [doc tools-reference]. **The useful window is roughly 6,000 to 30,000 chars.** On versions below 2.1.121 the field is silently ignored and the tool is unaffected (fail-safe).
Never emit on PostToolUse: `decision:"block"` (does not replace; it adds feedback), `continue:false` ("Claude stops processing entirely", and "the stop applies even when the tool call … completes while Claude is still streaming"), or exit 2 ("Shows stderr to Claude").

### 4.3 PreCompact (behavior 3: snapshot, never veto)
stdin:
```json
{"session_id":"8f1c2a3e-…","transcript_path":"/Users/me/.claude/projects/-Users-me-proj/8f1c2a3e-….jsonl",
 "cwd":"/Users/me/proj","hook_event_name":"PreCompact","trigger":"auto","custom_instructions":null}
```
(c) Output: **nothing** (exit 0, empty stdout). The event has no context or `hookSpecificOutput` fields, and "Claude Code discards a PreCompact hook's `systemMessage` and `continue` fields." Blockers to avoid: exit 2 ("Blocks compaction") and `{"decision":"block"}`. Blocking an auto-compaction that recovers from a context-limit error makes "the current request fail[s]".

Transcript format: JSONL at `~/.claude/projects/<cwd with non-alphanumerics replaced by '-'>/<session_id>.jsonl` [doc sessions]. It is "written asynchronously and may lag" [doc]. The line schema is undocumented, so these facts are [obs]:
- `type` values include `user`, `assistant`, `system`, `attachment`, `mode`, `ai-title`, `file-history-snapshot`, `last-prompt`, `queue-operation`, and others. Keep only `user` and `assistant`.
- Assistant lines carry ONE content block each (`message.content[0].type` ∈ `text|thinking|tool_use`).
- User lines: `message.content` is a string (typed prompt) or a list (`tool_result`, `text`, `image`).
- Skip lines with `isMeta:true` (caveats, reminders), `isCompactSummary:true` (the summary), or `isSidechain:true` (subagents), and strings starting with `<command-name>`, `<local-command-stdout>` or `<local-command-caveat>`.
- A compaction writes a `{"type":"system","subtype":"compact_boundary","compactMetadata":{"trigger":"auto","preTokens":…,"preservedMessages":{…}}}` line to the **same file**. `sessionId` is unchanged across compaction [obs], so keying the snapshot by `session_id` is correct.
- Note: current Claude Code already keeps a verbatim "preservedSegment" of recent messages after compaction [obs], so behavior 4 overlaps with built-in behavior.

### 4.4 SessionStart with source `compact` (behavior 4: re-inject)
stdin:
```json
{"session_id":"8f1c2a3e-…","transcript_path":"/Users/me/.claude/projects/-Users-me-proj/8f1c2a3e-….jsonl",
 "cwd":"/Users/me/proj","hook_event_name":"SessionStart","source":"compact","model":"claude-opus-5"}
```
`source` ∈ `startup|resume|clear|compact|fork` (`fork` ≥2.1.214). `model` may be absent.
(a) output:
```json
{"hookSpecificOutput":{"hookEventName":"SessionStart",
  "additionalContext":"[subcortex] Last messages before compaction (oldest first):\nuser: …\nassistant: …"}}
```
Keep it under 10,000 chars. `sessionTitle` is ignored on `compact`. SessionStart exit 2 only "Shows stderr to user only" (non-blocking). Still never emit `continue:false`.

---

## 5. Exit codes and output semantics

| Exit | Meaning [doc] |
|---|---|
| 0 | Success. JSON is parsed if stdout is `{…}`. Plain text becomes context on UserPromptSubmit/SessionStart and goes to the debug log elsewhere. Stderr goes to the debug log only. |
| 2 | "Blocking error". Blocks regardless of JSON. **UserPromptSubmit: blocks and erases the prompt. PreCompact: blocks compaction.** PostToolUse: stderr is shown to Claude. SessionStart: stderr shown to the user. |
| any other (1, 127, …) | Non-blocking. The action proceeds, and the transcript shows a "`<hook> hook error`" notice with the first stderr line ("Failed with non-blocking status code: …"). With valid JSON, the JSON decides and no error is shown. |
| timeout | The handler is cancelled and its output discarded. The prompt proceeds without context, with a notice. |

**Full list the adapter must NEVER produce:** exit 2 (any event); exit ≠0 (noisy error notice); `"decision":"block"` (UserPromptSubmit rejects the prompt; PostToolUse injects feedback and does NOT replace; PreCompact vetoes); `"continue":false` or `"stopReason"` (stops Claude); `"suppressOriginalPrompt"`; `hookSpecificOutput.permissionDecision` (PreToolUse only); `systemMessage` (a user-visible warning; avoid as noise); JSON with a wrong `hookEventName` or a malformed `updatedToolOutput` (validation error notice, or the change is ignored); **any non-JSON text on stdout for UserPromptSubmit/SessionStart** (injected verbatim as context); multi-line JSON where one line sets a field (a parse error). Write exactly one JSON object or nothing, and exit 0.

---

## 6. MCP fallback registration
```bash
claude mcp add --scope user --transport stdio subcortex -- /opt/homebrew/bin/subcortex mcp
claude mcp remove subcortex --scope user
```
This stores `{"mcpServers":{"subcortex":{"type":"stdio","command":"/opt/homebrew/bin/subcortex","args":["mcp"],"env":{}}}}` at the top level of `~/.claude.json` (user scope). Local scope is the default; it is stored under that project's entry in `~/.claude.json`. Project `.mcp.json` uses the same `mcpServers` shape and requires approval. One-run test: `claude -p … --mcp-config '{"mcpServers":{"subcortex":{"command":"/abs/subcortex","args":["mcp"]}}}' --strict-mcp-config`. MCP tools appear as `mcp__subcortex__<tool>`.

---

## 7. Mismatches in the existing code (file:line → fix)

1. **CRITICAL: the exit-2 bug is still present.** `src/subcortex/installers/claude_code.py:47` writes `… hook claude <Event>`, but `src/subcortex/cli.py:300` accepts only `choices=["claude-code","codex"]`. argparse then exits 2 on every hook, which blocks every prompt and every compaction. Fix both sides: (a) emit `hook claude-code`, and (b) accept the aliases `claude`, `claude-code` and `claude_code`.
2. `src/subcortex/cli.py:320-322`: `build_parser().parse_args(argv)` can raise `SystemExit(2)` for *any* hook-path mistake (unknown event, extra arg, future flag). Fix: in `main()`, if `argv[0]=="hook"`, bypass argparse. Take tui and event positionally (default event = `payload.hook_event_name`), wrap everything in `try/except BaseException: return 0`, and `os._exit(0)`-style guarantee exit 0. Also add `|| true` in the installed command (section 3).
3. `src/subcortex/adapters/claude_code.py:335-341` (and all imports): stray stdout from imported libraries or `load_config()` corrupts the output, and on UserPromptSubmit/SessionStart plain text becomes model context. Fix: before the work, `real = os.dup(1); os.dup2(2, 1)`. Write the final JSON only to `real`.
4. `src/subcortex/installers/claude_code.py:30-31`: ignores `CLAUDE_CONFIG_DIR`. Use `Path(os.environ.get("CLAUDE_CONFIG_DIR") or ~/.claude) / "settings.json"`.
5. `src/subcortex/installers/claude_code.py:50-51`: `_is_subcortex_hook` matches the substring `"subcortex"` anywhere in the command. Uninstall would delete unrelated user hooks (e.g. a script under `…/Gits/moji/subcortex/…`). Use the regex or exact-command set from section 3.
6. `src/subcortex/installers/claude_code.py:38-47`: `_executable_command()` returns an unquoted path (line 42) that is then f-string concatenated. Use `shlex.quote(exe)` plus ` hook claude-code {event} || true`.
7. `src/subcortex/installers/claude_code.py:54-59` plus `:106`: invalid or unparseable settings.json becomes `{}`, and the file is then OVERWRITTEN with only our hooks (data loss). Refuse to write on a parse error.
8. `src/subcortex/installers/claude_code.py:62-66` plus `:106`: writes and re-backs up the file even when nothing changed. A second `install` overwrites `settings.json.subcortex.bak` with the already-modified file, so the original is lost. Write only if changed, and never overwrite an existing `.bak`.
9. `src/subcortex/adapters/claude_code.py:120-134`: `_looks_like_failure` checks `exit_code/exitCode/returncode`, which Bash `tool_response` never has, and failed commands never reach PostToolUse anyway. The check is harmless dead code. Add skips for `persistedOutputPath`, `backgroundTaskId`, `isImage` and `interrupted` (section 4.2).
10. `src/subcortex/adapters/claude_code.py:181`: decides on `verdict["needed"]` only, and ignores `p_needed` vs `thresholds.output_needed_threshold`. The Codex and OpenCode adapters use `p_needed`. Unify.
11. `src/subcortex/adapters/claude_code.py:186`: `updatedToolOutput` is **correct**. Document the minimum 2.1.121 (older versions ignore it, so it stays fail-open).
12. `src/subcortex/adapters/claude_code.py:224-232`: the transcript reader includes `isMeta`, `isCompactSummary` and `isSidechain` lines and `<local-command…>` strings. Filter them per section 4.3. Because assistant lines hold one block each, "last 5 messages" is really the last 5 blocks. Coalesce consecutive assistant lines that share the same `message.id`.
13. `src/subcortex/adapters/claude_code.py:263-266`: SessionStart does not check `source == "compact"` and relies on the matcher alone. Add the check, as `codex.py:230-232` already does.
14. `src/subcortex/adapters/claude_code.py:39-40`: `_compact_dir` ignores `SUBCORTEX_DATA_DIR` (`codex.py:156-159` honors it). Unify, which is needed for hermetic e2e.
15. `src/subcortex/adapters/claude_code.py:89-91`: imperative phrasing ("Prefer the most direct, minimal path."). The docs warn that imperative out-of-band text can trip prompt-injection defenses. Rephrase factually (section 4.1).
16. `docs/claude-code.md` "What it does" table: says "no nonzero exit" detection. PostToolUse never sees failures, and there is no exit field. Also document the 2.1.121 minimum and the 6k to 30k effective window.

### 7b. Uncommitted working-tree refactor (seen while writing; line refs above are against HEAD `3c96217`)
The working tree has replaced these files with `src/subcortex/hook.py`, `adapters/{base,claude_family}.py` and `installers/{base,claude_family}.py`. The new `hook.py` already fixes items 1-3 (it routes around argparse, always exits 0, captures stdout, and a `guard` drops blocking fields). `installers/base.py` fixes items 4-6 (`CLAUDE_CONFIG_DIR`, `shlex.join` plus ` 2>/dev/null || true`). Two new problems block Claude Code specifically:
- **CRITICAL** `adapters/claude_family.py:63-64` `is_claude_code_payload`: `if "prompt_id" in payload: return False  # Devin CLI`. **Claude Code itself sends `prompt_id` in the common input fields since 2.1.196** ("Absent until the first user input") [doc]. Every real Claude Code event after the first prompt is therefore ignored, including PostToolUse, PreCompact and SessionStart(compact). Use another Devin discriminator, or drop the check.
- **HIGH** `adapters/claude_family.py:29, 38-41, 47-57`: `ClaudeCodeAdapter` inherits `replaces_output = False`, so behavior 2 is disabled even though ≥2.1.121 supports it. The base renders `updatedToolOutput` as a **string**, which Claude Code ignores for built-in tools ("a value that doesn't match the tool's output schema is ignored"). For Claude Code, set `replaces_output = True` and render `{**tool_response, "stdout": replacement}` (skip when `persistedOutputPath`, `backgroundTaskId`, `isImage` or `interrupted` is set). The installer self-test at `installers/claude_family.py:102-104` will then expect output for PostToolUse.
- `adapters/base.py:83-96` `failed_of` checks `exit_code`-style keys. Claude's Bash result has none (see item 9), so detection relies on `interrupted` plus the stderr traceback.

---

## 8. Unverified or flagged
- [unverified] `--bare` plus `--settings` actually runs `hooks` from the settings JSON. The docs imply it; smoke-test it.
- [unverified] Whether `updatedToolOutput` on a result that also carries `persistedOutputPath` is re-spilled or replaces the preview. We skip that case.
- [unverified] Behavior when two PostToolUse hooks both return `updatedToolOutput`. The docs only say a note is dropped "if … another hook's rewrite replaces it", which suggests last-writer-wins.
- [obs, not doc] The transcript line schema and the Bash `tool_response` extra keys. The docs say they are not a stable interface; parse defensively.
- [unverified] `/compact` in `-p` mode as an e2e trigger. Use `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` or an interactive run instead.
- [unverified] Exact location of `.claude.json` under a relocated `CLAUDE_CONFIG_DIR`.
