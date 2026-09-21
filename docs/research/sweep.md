# Sweep: terminal coding agents with extension seams for subcortex (as of 2026-09-21)

**Method.** Three parallel research passes used official docs, source (`gh api`), and npm/PyPI registries and shipped bundles, with nothing installed. I then spot-checked the highest-value claims myself against primary docs: Qoder hooks, CodeBuddy hooks, Factory Droid hooks reference, the Open Interpreter hooks doc and releases, and the Kiro hook-actions page.

**Scope.** Agents already covered elsewhere are only listed at the end, not re-specified: Claude Code, Codex CLI, OpenCode, Gemini CLI, Qwen Code, Cursor CLI, Copilot CLI, Kimi Code, Crush, Goose, OpenHands. Amp and Aider have their own specs (`amp.md`, `aider.md`).

**Legend.**
- B1: prompt-submit context injection
- B2: *replace* post-tool output. "append" means additionalContext only, which does not count.
- B3: pre-compaction event (observe)
- B4: session-start after compaction, or an equivalent re-injection point
- ✅ native · ◐ workaround or partial · ✗ impossible
- "CC-compat" means the same event names and JSON shapes as Claude Code hooks.

---

## 0. Cross-cutting findings (read first)

1. **Some agents run Claude Code hook files, so a `subcortex hook claude …` entry will fire inside another TUI with a foreign payload.**
   - Grok Build (xAI) scans `~/.claude/settings.json(.local)` and `<proj>/.claude/settings.json`. Opt out with `[compat.claude] hooks = false` in `~/.grok/config.toml`.
   - Devin CLI reads `~/.claude.json`, `~/.claude/settings(.local).json` and `.claude/settings(.local).json`. Opt out with `"read_config_from": {"claude": false}`.
   - Snowflake Cortex Code reads `.claude/settings(.local).json` and `~/.claude/settings.json`.
   - Continue `cn` merges `~/.claude/settings.json` and `.claude/settings{,.local}.json`, but its hook engine is not wired yet (see its entry). It would send `transcript_path:""`.
   - Factory Droid offers an interactive "Claude Code Hooks Auto-Migration" that rewrites tool names.
   - **Action:** the Claude adapter must sniff the host and no-op (exit 0, empty stdout) on foreign payloads. Signs of a foreign payload:
     - camelCase `hookEventName`/`workspaceRoot`/`sessionId` (Grok)
     - `prompt_id` with no `transcript_path` (Devin)
     - lowercase `bash` tool (Cortex)
     - empty `transcript_path` (cn)
2. **True B2 output replacement via command hooks is rare.** It exists only in:
   - Qoder and CodeBuddy: `hookSpecificOutput.updatedToolOutput`
   - Grok Build: `updatedToolOutput`, in the tool's own tagged shape
   - Open Interpreter: PostToolUse `{"continue":false,"reason":…}`, a Codex-engine quirk
   - Mistral Vibe: `post_tool` `decision:"deny"`+`reason`
   - Docker Agent: `tool_response_transform`

   Plugin seams that replace: Kilo `tool.execute.after`, Cline `afterTool`, Pi `tool_result`, Neovate `toolResult`, Amp `tool.result`.

   Kiro, Q, Droid, Auggie, Letta, Junie, Devin, Kode and Every Code **cannot** replace output.
3. **The same field means different things across TUIs.**
   - `continue:false` on PostToolUse *replaces output without aborting* in Open Interpreter (Codex engine), but *stops* in Claude-shaped engines (Qoder, CodeBuddy, Droid, Auggie).
   - `decision:"deny"` on Vibe's `post_tool` replaces output, but in Qoder it blocks, the same as exit 2.
   - Never share a PostToolUse response builder across adapters.
4. **Universal fail-open rules.**
   - Exit 0.
   - Emit empty stdout, or `{}`, when there is nothing to do.
   - Never emit `decision:"block"|"deny"`, `continue:false` (except OI's deliberate B2 path), `permissionDecision:"deny"`, or `cancel:true`.
   - Never exit 2.
   - Several agents treat *any* non-zero exit, a timeout, or a crash as blocking on specific events. See each entry. Examples: Kiro prompt submit per its docs, cecli pre_tool, Docker Agent pre_tool_use.

---

## 1. Summary table

| Agent (binary) | Latest (date) | Seam | Config path | B1 | B2 | B3 | B4 | CC-compat | Notes / priority | Source |
|---|---|---|---|---|---|---|---|---|---|---|
| **Qoder CLI** (`qodercli`) | 1.1.60 (2026-09-21, npm `@qoder-ai/qodercli`) | command hooks (+http/prompt/agent) | `~/.qoder/settings.json`, `.qoder/settings.json`, `.qoder/settings.local.json` → `hooks` | ✅ | ✅ `updatedToolOutput` | ✅ PreCompact | ✅ SessionStart `compact` | **near-clone** (tool `Bash`) | **P1: all 4** | docs.qoder.com/cli/hooks.md |
| **CodeBuddy Code** (`codebuddy`) | 2.156.0 (2026-09-20, npm `@tencent-ai/codebuddy-code`) | command hooks | `~/.codebuddy/settings.json`, `.codebuddy/settings(.local).json` → `hooks` | ✅ | ✅ `updatedToolOutput` | ✅ | ✅ | **near-clone** | **P1: all 4** | codebuddy.ai/docs/cli/hooks |
| **Open Interpreter** (`interpreter`) | rust-v0.0.45 (2026-09-20) | command hooks (Codex engine) | `~/.openinterpreter/hooks.json` or `config.toml` | ✅ | ✅ via `continue:false`+`reason` | ✅ PreCompact | ✅ SessionStart `compact` | Codex-compat; Claude names | **P1: all 4.** Trust gate | github.com/openinterpreter/openinterpreter docs/hooks.md |
| **Factory Droid** (`droid`) | 0.223.0 (2026-09-19) | command hooks | `~/.factory/hooks.json`, `.factory/hooks.json` (no `hooks` wrapper) | ✅ | ✗ (append) | ✅ | ✅ | names yes, tools differ (`Execute`) | **P2: 3/4** | docs.factory.ai/reference/hooks-reference.md |
| **Grok Build** (`grok`, xAI) | 1.0.40 stable (x.ai/cli/stable; source synced 2026-09-19) | command+http hooks | `~/.grok/hooks/*.json`, `<proj>/.grok/hooks/*.json`, `~/.grok/config.toml` `[[hooks.<Event>]]` (+ Claude/Cursor compat files) | ✗ (stdout discarded) | ✅ (tagged shape) | ◐ passive | ✗ | reads CC files; camelCase stdin | P2. **Reads `~/.claude/settings.json`** | xai-org/grok-build docs/user-guide/10-hooks.md |
| **Docker Agent** (formerly cagent) | v1.142.0 (2026-09-21) | YAML command hooks | `~/.config/cagent/hooks.d/*.yaml` (drop-in), `~/.config/cagent/config.yaml` `settings.hooks`, agent YAML | ✅ | ✅ `tool_response_transform` | ✅ `pre_compact`/`before_compaction` | ◐ via `turn_start` | no (snake_case) | P2. pre_tool_use failure **blocks by default** | docker/cagent docs/configuration/hooks/index.md |
| **Kilo Code CLI** (`kilo`) | 7.7.6 (2026-09-21, `@kilocode/cli`) | TS plugin (Bun; OpenCode fork) | `~/.config/kilo/plugin/*.ts`, `.kilo/plugin/`, or `kilo.json[c]` `"plugin":[]` | ✅ `chat.message` | ✅ `tool.execute.after` | ✅ `experimental.session.compacting` | ✅ `session.compacted` event | no (OpenCode API) | **P1: reuse the OpenCode plugin**; does not read `.opencode/` | Kilo-Org/kilocode packages/plugin/src/index.ts |
| **Cline CLI** (`cline`) | 3.0.62 (2026-09-15; `@cline/core` 0.0.83) | file hooks **and** TS plugins | hooks: `~/.cline/hooks/<Event>`; plugins: `~/.cline/plugins/*.ts` | file ✗ (async) / plugin ◐ `beforeModel` | file ✗ / plugin ✅ `afterTool {result}` | ✗ (`PreCompact` unwired) | plugin ◐ | no | P2 via TS plugin | docs.cline.bot/sdk/plugins.md, config.md |
| **Pi** (`pi`, earendil-works/pi, was badlogic/pi-mono) | v0.87.0 (2026-09-21) | TS extension | `~/.pi/agent/extensions/*.ts`, `.pi/extensions/` | ✅ `before_agent_start` | ✅ `tool_result` | ✅ `session_before_compact` | ◐ flag on `session_compact`, inject next turn | no; **no MCP** | P2 (Amp's plugin API is modeled on it) | packages/coding-agent/docs/extensions.md |
| **Kiro CLI** (`kiro-cli`) | 2.22.0 (2026-09-16) | command hooks (plain stdout) | V2: `~/.kiro/agents/<name>.json` `hooks`; V3: `.kiro/hooks/*.json`, `~/.kiro/hooks/` | ✅ plain stdout | ✗ | ✗ | ✗ (spawn only) | no | P3: B1 only | kiro.dev/docs/hooks/actions, /docs/custom-agents/configuration-reference, /docs/cli/v3/hooks-migration |
| **Amazon Q Dev CLI** (`q chat`) | v1.19.7 (2025-11-17), **superseded by Kiro CLI** | command hooks (same as Kiro V2) | `~/.aws/amazonq/cli-agents/*.json`, `.amazonq/cli-agents/` | ✅ | ✗ | ✗ | ✗ | no | skip (maintenance-only) | github.com/aws/amazon-q-developer-cli |
| **Auggie** (`auggie`) | 0.36.0 (2026-08-21) | command hooks (Claude-like JSON) | `~/.augment/settings.json`, `<ws>/.augment/settings(.local).json`, `/etc/augment/settings.json` | ✗ (PromptSubmit fire-and-forget) | ✗ (append) | ✗ | ◐ SessionStart startup/resume only | partial (shape yes; names/paths no) | P3 | docs.augmentcode.com/cli/hooks.md |
| **Letta Code** (`letta`) | v0.32.15 (2026-09-21) | command hooks (own stdin shape) | `~/.letta/settings.json`, `.letta/settings(.local).json` → `hooks` | ✅ raw stdout | ✗ | ◐ PreCompact observe | ◐ (no compact source) | **no** (`event_type`, `working_directory`) | P3 | letta-ai/letta-code src/hooks/*.ts |
| **Junie CLI** (JetBrains) | 26.9.14 (2026-09-14) | command hooks (**EAP only**) | `~/.junie/config.json` `hooks` (project `.junie/config.json` ignored by default) | ✅ | ✗ (no PostToolUse) | ✗ | ✅ SessionStart `compact` | mostly (names/shape) | P3 | junie.jetbrains.com/docs/junie-cli-hooks.html |
| **Devin CLI** (Cognition) | v3000.10.31 (2026-09-16) | command hooks | `.devin/hooks.v1.json`, `.devin/config.json`, `~/.config/devin/config.json` (+ Claude files) | ✅ | ✗ (append) | ✗ (PostCompaction only) | ◐ `PostCompaction`/SessionStart `source` | reads CC files; tool `exec` | P3. **Reads `~/.claude/settings.json`** | docs.devin.ai/cli/extensibility/hooks |
| **Mistral Vibe** (`vibe`) | v2.25.5 (2026-09-18) | TOML command hooks | `~/.vibe/hooks.toml`, `<proj>/.vibe/hooks.toml` (trusted) | ✗ | ✅ `post_tool` deny+reason | ✗ | ✗ | no | P3: B2 only | mistralai/mistral-vibe README "Hooks", vibe/core/hooks/*.py |
| **Snowflake Cortex Code** (`cortex`) | UNVERIFIED | command hooks | `.cortex/settings(.local).json`, `~/.snowflake/cortex/hooks.json` (+ Claude files) | ✅ | UNVERIFIED | ✅ PreCompact | ✅? SessionStart | reads CC files; tool `bash` | P3 | docs.snowflake.com/en/user-guide/cortex-code/extensibility |
| **Every Code** (`coder`, just-every/code) | v0.6.191 (2026-09-16) | project hooks (env payload) | `~/.code/config.toml` `[[projects."<abs cwd>".hooks]]` | ✅ plain stdout | ✗ | ✗ | ✗ | no | P4 (per-repo only, sandboxed) | just-every/code docs/config.md#project-hooks |
| **Kode** (`@shareai-lab/kode`) | 2.2.1 | command hooks (Claude-style) | UNVERIFIED | ✅ | ✗ | ✅ PreCompact | UNVERIFIED | mostly | P4 | shareai-lab/kode src types |
| **Neovate Code** | 0.28.5 (Feb 2026, low activity) | JS plugin | UNVERIFIED | ✅ `userPrompt` | ✅ `toolResult` | ✗ | ✗ | no | P4 | neovateai/neovate-code plugin.ts |
| **cecli** (Aider fork, ex-aider-ce) | v1.6.0 (2026-09-19) | command (placeholder args) + Python hooks | `.cecli.conf.yml` `hooks:` | ◐ Python `on_message` | ✗ | ✗ | ◐ | no | see aider.md §8 | cecli-dev/cecli docs/config/hooks.md |
| superagent-ai/grok-cli (`grok-dev`) | 1.1.7 (2026-05-15) | command hooks | `~/.grok/user-settings.json` (**clashes with official `~/.grok`**) | UNVERIFIED | ✗ | ✗ | ✗ | partial | skip | superagent-ai/grok-cli |
| Continue CLI (`cn`) | 1.5.47 (2026-06-18) | hooks code present but **never fired**, so **MCP only** in practice | MCP: `~/.continue/config.yaml` | ✗ | ✗ | ✗ | ✗ | would be CC-compat | MCP. Watch issue #11678 | continuedev/continue extensions/cli/src/hooks |
| Warp agent / `warp` CLI / Oz | app v0.2026.09.16 | MCP + rules only | `~/.warp/.mcp.json`, `{repo}/.warp/.mcp.json`, `~/.warp_cli/.mcp.json` | ✗ | ✗ | ✗ | ✗ | – | MCP only | github.com/warpdotdev/docs |
| Zed agent | v1.20.2 (2026-09-17) | MCP only; no headless or terminal agent mode | `~/.config/zed/settings.json` `context_servers` | ✗ | ✗ | ✗ | ✗ | – | MCP; external ACP agents use their own hooks | zed docs/src/ai/mcp.md |
| Roo Code CLI (`roo`) | cli-v0.1.17 (2026-03-04); **repo archived 2026-05-15** | MCP only | `.roo/mcp.json` | ✗ | ✗ | ✗ | ✗ | – | skip | RooCodeInc/Roo-Code |
| Rovo Dev CLI (`acli rovodev`) | UNVERIFIED | notification-only `eventHooks` (no I/O contract) | `~/.rovodev/config.yml`; MCP `~/.rovodev/mcp_config.json` | ✗ | ✗ | ✗ | ✗ | – | MCP + AGENTS.md | atlassian.com/blog/development/streamline-rovo-dev-cli-with-event-hooks |
| Trae Agent (bytedance/trae-agent) | 0.1.0 (last push 2026-02-05) | MCP only (hooks issue #397 open) | `trae_config.yaml` `mcp_servers:` | ✗ | ✗ | ✗ | ✗ | – | skip | github.com/bytedance/trae-agent |
| iFlow CLI | 0.5.19 (2026-04-25); **service shut down 2026-04-17** | had hooks (9 events) | – | – | ✗ | ✗ | ◐ | – | skip (dead) | iflow-ai/iflow-cli README |
| Qodo Command (`@qodo/command`) | 0.36.0 (2026-01-11), **npm-deprecated** | MCP only (`mcp.json`) | – | ✗ | ✗ | ✗ | ✗ | – | skip (dead) | registry.npmjs.org/@qodo/command |
| Plandex | cli/v2.2.1 (2025-07-16), dormant (Cloud wound down) | none (internal server hooks only) | – | ✗ | ✗ | ✗ | ✗ | – | skip | github.com/plandex-ai/plandex |
| Charm Mods | v1.8.1 (2025-07-10); **archived 2026-03-09** (successor: Crush) | MCP (`mods.yml`) | – | ✗ | ✗ | ✗ | ✗ | – | skip | github.com/charmbracelet/mods |
| Jules CLI (`@google/jules`) | 0.1.42 (2025-12-16) | none locally (cloud VM; curated MCP via web) | – | ✗ | ✗ | ✗ | ✗ | – | skip | jules.google/docs/cli/reference |
| Codebuff (repo now CodebuffAI/freebuff) | npm 1.0.688 | none found (UNVERIFIED) | – | ✗ | ✗ | ✗ | ✗ | – | skip | – |
| Other Codex forks: stellarlinkco/codex (archived; v1.3.0, 2026-03-15), ymichael/open-codex (dormant since 2025-05) | – | (Codex hooks) | – | – | – | – | – | – | skip | GitHub |

**Suggested adapter order:**
1. Qoder, CodeBuddy and Open Interpreter: command hooks with all 4 behaviors; Qoder and CodeBuddy mostly reuse the Claude adapter.
2. Kilo: reuse the OpenCode plugin.
3. Droid, then Grok Build, then Docker Agent.
4. Cline and Pi via plugins.
5. B1-only or partial: Kiro, Junie, Devin, Letta, Auggie, Vibe.
6. Warp and Zed via MCP.

---

## 2. Detailed entries: agents with real command hooks

### 2.1 Qoder CLI (`qodercli`): P1, all 4 behaviors (spot-verified)
- **Version:** 1.1.60 on npm `@qoder-ai/qodercli` (2026-09-21). The release-notes page tops out at 1.1.58 (2026-09-19).
- **Config:** the `hooks` key in `~/.qoder/settings.json` (user), `${project}/.qoder/settings.json`, and `${project}/.qoder/settings.local.json`. All three are merged.
  ```json
  {"hooks":{
    "UserPromptSubmit":[{"hooks":[{"type":"command","command":"subcortex hook qoder UserPromptSubmit","timeout":10}]}],
    "PostToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"subcortex hook qoder PostToolUse","timeout":30}]}],
    "PreCompact":[{"hooks":[{"type":"command","command":"subcortex hook qoder PreCompact","timeout":10}]}],
    "SessionStart":[{"matcher":"compact","hooks":[{"type":"command","command":"subcortex hook qoder SessionStart","timeout":10}]}]}}
  ```
  - Hook types: `command`, `http`, `prompt`, `agent`.
  - Command fields: `timeout` (default 600 s), `shell`, `env`, `if` (e.g. `"Bash(git *)"`), `async`, `asyncRewake`, `once`, `statusMessage`, `args` (exec form).
- **Events (27)** include SessionStart (`source` startup|resume|clear|compact|new), SessionEnd, UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure, PermissionRequest, PermissionDenied, Stop, StopFailure, SubagentStart, SubagentStop, PreCompact (`trigger`, `custom_instructions`), PostCompact (`trigger`, `compact_summary`), Notification, and InstructionsLoaded.
- **stdin:**
  - Common: `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `permission_mode`, `agent_id`, `agent_type`.
  - PostToolUse adds `tool_name`, `tool_input`, `tool_use_id`, `tool_response` (an object shaped per tool), plus `mcp_context` for MCP tools.
- **stdout (exit 0):** JSON `{continue, stopReason, suppressOutput, systemMessage, decision, reason, hookSpecificOutput}`. Plain-text stdout is injected as context **only for SessionStart and UserPromptSubmit**.
  - B1: `{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"…"}}`
  - B2: `{"hookSpecificOutput":{"hookEventName":"PostToolUse","updatedToolOutput":"<head…marker…tail>"}}`. The docs say it "Replaces the tool response (works for any tool)". `updatedMCPToolOutput` is lower priority.
  - B3: PreCompact → exit 0 with empty stdout.
  - B4: SessionStart (matcher `compact`) → `additionalContext`, or plain stdout.
  - **`hookSpecificOutput` MUST include `hookEventName`**, "otherwise the entire JSON output is rejected".
- **Exit codes:** 0 means stdout is parsed. **2 blocks** on UserPromptSubmit, PreToolUse, Stop, SubagentStop and **PreCompact** ("exit 2 prevents this compaction"). Other codes are non-blocking and their stdout is ignored.
- **Avoid:** exit 2; `decision:"deny"` (equivalent to exit 2); `continue:false`.
- **CC-compat:** near-clone. Same event names, fields and tool names (`Bash`, `Write`, `Edit`, …). `--with-claude-config` imports Claude skills, commands and subagents, but not hooks.
- **MCP:** `mcpServers` in `~/.qoder/settings.json`. A project `.mcp.json` requires approval.
- **Sources:** https://docs.qoder.com/cli/hooks.md , https://docs.qoder.com/cli/hooks-reference.md , https://docs.qoder.com/cli/mcp-reference.md

### 2.2 Tencent CodeBuddy Code (`codebuddy`): P1, all 4 behaviors (spot-verified)
- **Version:** 2.156.0 on npm `@tencent-ai/codebuddy-code` (2026-09-20).
- **Config:** the `hooks` key in `~/.codebuddy/settings.json`, `<proj>/.codebuddy/settings.json` and `<proj>/.codebuddy/settings.local.json`. The structure is identical to Claude's: `{"hooks":{"EventName":[{"matcher":"…","hooks":[{"type":"command","command":"…"}]}]}}`.
  - Hook types: `command`, `prompt`, `agent`, `http`.
  - Default timeout 60 s. Env: `CODEBUDDY_PROJECT_DIR`, `CODEBUDDY_PLUGIN_ROOT`.
- **Events:** 27+, including UserPromptSubmit, PreToolUse, PostToolUse, PreCompact (`trigger` manual|auto, `custom_instructions`), PostCompact, and SessionStart (`source` startup|resume|clear|compact).
- **stdin:** `session_id`, `transcript_path`, `cwd`, `permission_mode`, `generation_id`, `hook_event_name`, plus tool fields.
- **stdout:**
  - B1: `hookSpecificOutput.additionalContext`. Exit-0 stdout is also "appended to conversation context".
  - B2: `{"hookSpecificOutput":{"hookEventName":"PostToolUse","updatedToolOutput":"compressed"}}`. The docs say it "entirely replaces the original tool output before sending to the Agent".
  - B3: PreCompact → exit 0, no output.
  - B4: SessionStart `source:"compact"` → `additionalContext`.
- **Exit codes:** 0 means success. **2 is blocking**, including **blocking compaction on PreCompact** and blocking the prompt on UserPromptSubmit. Other codes are non-blocking.
- **Avoid:** exit 2; `continue:false`; `decision` (deprecated).
- **CC-compat:** same shape. Does not read `.claude` files. UNVERIFIED: the PreToolUse input rewrite may be `modifiedInput` instead of `updatedInput`, which is irrelevant for subcortex.
- **MCP:** `~/.codebuddy/.mcp.json` (user) or `<proj>/.mcp.json`, using `mcpServers`. CLI: `codebuddy mcp add --scope user subcortex -- subcortex mcp`.
- **Sources:** https://www.codebuddy.ai/docs/cli/hooks , https://www.codebuddy.ai/docs/cli/mcp

### 2.3 Open Interpreter (`interpreter`): P1, all 4 behaviors, Codex hook engine (spot-verified)
- **Status:** the project is now "a coding agent for open models". It is a Rust fork of OpenAI Codex (`codex-rs/`, `codex-rs/hooks/`). Latest release rust-v0.0.45 (2026-09-20). Install with `curl -fsSL https://www.openinterpreter.com/install | sh`. The legacy Python `open-interpreter` is dead.
- **Config:** all matching sources run: `~/.openinterpreter/hooks.json`, `~/.openinterpreter/config.toml` (`[[hooks.<Event>]]`), `.openinterpreter/hooks.json` and `.openinterpreter/config.toml` (trusted projects), and plugin-bundled hooks. Hooks are on by default (`[features] hooks = true`).
  ```json
  {"hooks":{"PostToolUse":[{"matcher":"^Bash$","hooks":[{"type":"command","command":"subcortex hook openinterpreter PostToolUse","timeout":30}]}]}}
  ```
- **Trust gate:** "Non-managed command hooks must be reviewed and trusted before they run… changed hooks need review again". The user approves them via `/hooks`, and `--dangerously-bypass-hook-trust` skips it. Trust is stored as `[hooks.state."<key>"] trusted_hash="sha256:…"`, hashed over the event, matcher and handler, so **any edit to our command string resets trust**. Default timeout 600 s.
- **Events:** SessionStart (`source` startup|resume|clear|compact), UserPromptSubmit, PreToolUse, PermissionRequest, PostToolUse, PreCompact and PostCompact (`trigger` manual|auto), SubagentStart, SubagentStop, Stop.
- **stdin:**
  - Common: `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `model`.
  - UserPromptSubmit adds `turn_id`, `permission_mode`, `prompt`.
  - PostToolUse adds `turn_id`, `permission_mode`, `tool_name`, `tool_input`, `tool_response`, `tool_use_id`.
  - PreCompact adds `turn_id`, `trigger`.
  - SessionStart adds `permission_mode`, `source`.
- **stdout:**
  - B1: `{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"…"}}`. Plain stdout also becomes context.
  - **B2: `{"continue":false,"reason":"<head…marker…tail>"}`.** The model-visible output becomes `reason`, the original is kept for logs, and the turn is not aborted (`PostToolUseFeedbackOutput{model_visible}`, codex-rs/core/src/tools/registry.rs).
    - `updatedMCPToolOutput` is rejected, and `suppressOutput` is rejected.
    - PostToolUse fires only on tool success. Non-zero shell exits probably skip it (UNVERIFIED).
  - B3: PreCompact → empty stdout, exit 0. **`continue:false` on PreCompact aborts the turn** (`TurnAborted`).
  - B4: SessionStart `source:"compact"` → `additionalContext`, or plain stdout.
- **Avoid:** exit 2; `decision:"block"`; `continue:false` anywhere except the deliberate PostToolUse B2 path; `permissionDecision:"deny"`. Other exit codes are logged and ignored.
- **Compat:** Codex-hook-compatible (the same engine as upstream Codex, so the Codex adapter can likely be reused with the path changed). Claude-like names and fields.
- **MCP:** `[mcp_servers.subcortex] command="subcortex" args=["mcp"]` in `~/.openinterpreter/config.toml`.
- **Sources:** https://github.com/openinterpreter/openinterpreter (docs/hooks.md, docs/mcp.md, codex-rs/hooks/schema/generated/, codex-rs/core/src/tools/registry.rs, codex-rs/hooks/src/engine/output_parser.rs)

### 2.4 Factory Droid (`droid`): P2, behaviors 1, 3 and 4; no replacement (spot-verified)
- **Version:** 0.223.0 (2026-09-19). npm `droid` / `@factory/cli`.
- **Config:** `~/.factory/hooks.json` (user) and `.factory/hooks.json` (project). The legacy `.factory/hooks/hooks.json` still loads. If `hooks.json` is absent, Droid reads the `hooks` key of `settings.json`.
  - **Standalone files are keyed by event with no `hooks` wrapper.**
  - "Always use absolute paths in hook commands."
  ```json
  {"UserPromptSubmit":[{"hooks":[{"type":"command","command":"/usr/local/bin/subcortex hook droid UserPromptSubmit","timeout":10}]}],
   "PreCompact":[{"hooks":[{"type":"command","command":"/usr/local/bin/subcortex hook droid PreCompact"}]}],
   "SessionStart":[{"matcher":"compact","hooks":[{"type":"command","command":"/usr/local/bin/subcortex hook droid SessionStart"}]}]}
  ```
- **Events:** PreToolUse, PostToolUse, UserPromptSubmit (`prompt`, `has_images`), Notification, Stop, SubagentStop, PreCompact (`trigger`, `custom_instructions`, `message_count`, `estimated_tokens`), SessionStart (`source` startup|resume|clear|compact), SessionEnd.
  - Tool matchers: `Execute` (the shell tool), `Read`, `Edit`, `Create`, `ApplyPatch`, …. `commandRegex` filters Execute commands.
- **stdin:** `session_id`, `transcript_path`, `cwd`, `permission_mode` (off|spec|auto-low|auto-medium|auto-high), `hook_event_name`, plus tool fields.
- **stdout:**
  - B1: `additionalContext` "is appended when not blocked".
  - B2: ✗. PostToolUse supports only `decision:"block"`+`reason` and `hookSpecificOutput.additionalContext`.
  - B4: SessionStart `hookSpecificOutput.additionalContext` "is appended to the new session context".
- **Avoid:** exit 2; `decision:"block"`; `continue:false`.
- **Other:** default timeout 60 s. Hooks are snapshotted at startup. `FACTORY_PROJECT_DIR`.
- **MCP:** `~/.factory/mcp.json` `{"mcpServers":{"subcortex":{"command":"subcortex","args":["mcp"],"disabled":false}}}`, or `droid mcp add subcortex subcortex mcp --type stdio`.
- **Sources:** https://docs.factory.ai/reference/hooks-reference.md , https://docs.factory.ai/harness/mcp.md

### 2.5 Grok Build (xAI official `grok`): P2, behaviors 2 and 3
- **Version:** 1.0.40 stable (https://x.ai/cli/stable). Source `xai-org/grok-build`, synced 2026-09-19. No GitHub releases.
- **Config:** `~/.grok/hooks/*.json`, `<proj>/.grok/hooks/*.json` (trusted projects), `[[hooks.<Event>]]` in `~/.grok/config.toml`, plus Claude and Cursor compat files. The Claude file format is accepted: `{"hooks":{"PostToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"…","timeout":30}]}]}}`. The `Bash` matcher is aliased to `run_terminal_command`.
- **stdin is camelCase:** `hookEventName` ("post_tool_use"), `hook_event_name` (PascalCase alias), `sessionId`, `cwd`, `workspaceRoot`, `permissionMode`, `promptId`, `toolName`, `toolInput`, `toolResult` (alias `tool_response`), `toolResultTruncated`.
- **Mapping:**
  - B1: ✗. "an allowing hook's stdout / additionalContext is discarded".
  - B2: ✅ `hookSpecificOutput.updatedToolOutput`. Built-in tools require grok's tagged shape, e.g. `{"type":"Bash","command":…,"exit_code":0,"output_for_prompt":"…"}`: edit the received `toolResult`, and check `toolResultTruncated` first. MCP tools accept a string. Capped at 64K.
  - B3: passive PreCompact.
  - B4: ✗. SessionStart stdout is ignored.
- **Avoid:** `decision:"deny"`, which is honored on any exit code; UserPromptSubmit `decision:"block"`. A non-zero exit drops the replacement, which is fail-open.
- **Other:** default timeout 5 s (600 s for PostToolUse).
- **MCP:** `[mcp_servers.subcortex]` in `~/.grok/config.toml`, or `grok mcp add subcortex -- subcortex mcp`.
- **Source:** xai-org/grok-build `crates/codegen/xai-grok-pager/docs/user-guide/{10-hooks,07-mcp-servers,05-configuration}.md`

### 2.6 Docker Agent (formerly cagent): P2, behaviors 1–3, and 4 via a workaround
- **Version:** v1.142.0 (2026-09-21).
- **Config:** YAML hooks can go in the agent file, in `~/.config/cagent/config.yaml` under `settings.hooks`, or as **drop-ins at `~/.config/cagent/hooks.d/*.yaml`**, which is the cleanest install point.
- **Events and output are snake_case.**
  - B1: `user_prompt_submit` → `hook_specific_output.additional_context`.
  - B2: `tool_response_transform` returns `updated_tool_response`.
  - B3: `pre_compact` and `before_compaction`. The latter can replace the summary via `hook_specific_output.summary`. `after_compaction` also exists.
  - B4: `session_start` source is `startup` only. Re-inject via `turn_start` context after `after_compaction`.
- **Avoid:** exit 2. **A `pre_tool_use` failure blocks by default**, so do not register pre_tool_use.
- **Source:** docker/cagent `docs/configuration/hooks/index.md`. Exact field names beyond those quoted are UNVERIFIED.

### 2.7 Kiro CLI (`kiro-cli`, AWS): P3, B1 only
- **Version:** 2.22.0 (2026-09-16). It has two harnesses: V2 (classic) and V3 ("CLI 3.0", opt in with `--v3`, or the `chat.agentEngine` setting since 2.21.4). Which one is the default is UNVERIFIED.
- **V2 config:** hooks inside the agent JSON at `~/.kiro/agents/<name>.json` or `.kiro/agents/`.
  ```json
  {"hooks":{"agentSpawn":[{"command":"subcortex hook kiro agentSpawn"}],
            "userPromptSubmit":[{"command":"subcortex hook kiro userPromptSubmit","timeout_ms":10000}],
            "postToolUse":[{"matcher":"execute_bash","command":"subcortex hook kiro postToolUse"}]}}
  ```
  - Hook fields: `command`, `matcher`, `timeout_ms`, `cache_ttl_seconds`, `max_output_size`.
- **V3 config:** `.kiro/hooks/*.json` or `~/.kiro/hooks/`, shaped `{"version":"v1","hooks":[{"name":"…","trigger":"UserPromptSubmit","action":{"type":"command","command":"…"},"timeout":10}]}`.
  - Triggers: `SessionStart`, `Stop`, `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `PreTaskExec`, `PostTaskExec`, `PostFile{Create,Save,Delete}`, `Manual`.
  - The docs conflict on whether `SessionStart` works in the CLI.
- **V2 events:** `agentSpawn`, `userPromptSubmit`, `preToolUse`, `postToolUse`, `stop`.
- **stdin:** `{"hook_event_name":"userPromptSubmit","cwd":…,"session_id":…,"prompt":…}`. postToolUse adds `tool_name`, `tool_input`, `tool_response{success,result}`. Env `USER_PROMPT`.
- **stdout:** **plain text, not JSON.** Exit-0 stdout is "added to the agent's context" for agentSpawn and userPromptSubmit and ignored for the others. **B2, B3 and B4 are impossible.** There are no compaction events, and agentSpawn fires once.
- **Exit codes conflict between sources.**
  - The CLI docs tab, as I fetched it, says non-zero exit sends stderr to the agent and **blocks** preToolUse and prompt submit.
  - Group A's reading says only exit 2 blocks.
  - Either way: **always exit 0, and set `timeout_ms` explicitly.** The documented default is 60 s on the actions page and 30 s in another reading.
- **MCP:** `~/.kiro/settings/mcp.json` or `.kiro/settings/mcp.json`, `{"mcpServers":{"subcortex":{"command":"subcortex","args":["mcp"]}}}`, or `kiro-cli mcp add`.
- **Sources:** https://kiro.dev/docs/hooks/actions/ , https://kiro.dev/docs/hooks/types.md , https://kiro.dev/docs/custom-agents/configuration-reference.md , https://kiro.dev/docs/cli/v3/hooks-migration.md
- **Amazon Q Developer CLI** uses the same V2 schema at `~/.aws/amazonq/cli-agents/*.json`, with `max_output_size` defaulting to 10 KB. It is maintenance-only ("now available as Kiro CLI"), so skip it.

### 2.8 Auggie (`auggie`, Augment): P3, only a partial B4
- **Version:** 0.36.0 (2026-08-21).
- **Config:** `~/.augment/settings.json`, `<ws>/.augment/settings(.local).json`, `/etc/augment/settings.json`.
  ```json
  {"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"/abs/sc-auggie-SessionStart.sh"}]}]}}
  ```
  - The docs require the command to be a `.sh`/`.ps1`/`.bat`/`.cmd` script, so ship a `.sh` shim that execs `subcortex hook auggie <event>`.
  - Timeouts are in ms.
- **Events:** `PreToolUse`, `PostToolUse`, `Stop`, `SessionStart`, `SessionEnd`, `Notification`, `PromptSubmit`. It does **not** use `UserPromptSubmit` or `PreCompact`, and the schema is strict, so Claude names only produce warnings.
- **stdin:**
  - Base: `hook_event_name`, `conversation_id`, `workspace_roots[]`.
  - Tool events add `tool_name`, `tool_input`, `tool_output`, `tool_error`, `file_changes`, `is_mcp_tool`.
  - PromptSubmit adds `user_prompt`.
- **stdout:** JSON `{continue, stopReason, suppressOutput, systemMessage, additionalContext, hookSpecificOutput}`.
  - B1: ✗. PromptSubmit is fired without being awaited, and its result is discarded (0.36.0 bundle).
  - B2: ✗. PostToolUse context is *appended* as `[Hook Context]`.
  - B4: SessionStart `additionalContext` at startup or resume only, with no compact source.
- **Avoid:** exit 2; `permissionDecision:"deny"`; `continue:false`; Stop `decision:"block"`.
- **MCP:** `"mcpServers":{"subcortex":{"command":"subcortex","args":["mcp"]}}` in `~/.augment/settings.json`.
- **Source:** https://docs.augmentcode.com/cli/hooks.md

### 2.9 Letta Code (`letta`): P3; not Claude-shaped
- **Version:** v0.32.15 (2026-09-21).
- **Config:** the `hooks` key in `~/.letta/settings.json` and `.letta/settings(.local).json`, shaped `{matcher, hooks:[{type:"command",command,timeout /*ms, default 60000*/}]}`. Non-tool events omit `matcher`.
- **Events:** PreToolUse, PostToolUse, PostToolUseFailure, PermissionRequest, UserPromptSubmit, Notification, Stop, SubagentStop, PreCompact, SessionStart, SessionEnd.
- **stdin (NOT Claude-shaped):**
  - Common: `event_type`, `working_directory`, `session_id`.
  - UserPromptSubmit: `prompt`, `is_command`, `agent_id`, `conversation_id`.
  - PostToolUse: `tool_result{status,output}`.
  - PreCompact: `context_length`, `max_context_length`.
  - SessionStart: `is_new_session`.
- **stdout:**
  - B1: raw exit-0 stdout is injected. JSON is not parsed.
  - B2: ✗.
  - B3: PreCompact, observe only.
  - B4: no compact source. SessionStart stdout is collected regardless of exit code. Otherwise re-inject on the next UserPromptSubmit.
- **Avoid:** exit 2 (blocks UserPromptSubmit, PreToolUse, Stop).
- **MCP:** `/mcp add --transport stdio subcortex subcortex mcp`.
- **Source:** letta-ai/letta-code `src/hooks/{types,executor,index,loader}.ts`

### 2.10 JetBrains Junie CLI: P3; hooks are Early Access only
- **Version:** 26.9.14 (build 3196.4, 2026-09-14).
- **Config:** the `hooks` key in `~/.junie/config.json`. The project `.junie/config.json` is ignored by default.
  ```json
  {"hooks":{"UserPromptSubmit":[{"hooks":[{"type":"command","command":"subcortex hook junie UserPromptSubmit","timeout":10}]}],
            "SessionStart":[{"matcher":"compact","hooks":[{"type":"command","command":"subcortex hook junie SessionStart","timeout":10}]}]}}
  ```
- **Events:** SessionStart (`source` incl. `compact`), UserPromptSubmit, PreToolUse, Stop, StopFailure, PermissionRequest, SessionEnd. **There is no PostToolUse and no PreCompact.**
- **stdin:** `hook_event_name`, `session_id`, `cwd`, `project_path`.
- **stdout:**
  - B1: `additionalContext` (top level or in `hookSpecificOutput`) is prepended to the prompt.
  - B4: SessionStart `compact`.
  - B2 and B3: ✗.
- **Avoid:** exit 2. Otherwise fail-open. Default timeout 10 s.
- **MCP:** `~/.junie/mcp/mcp.json` using `mcpServers`.
- **Sources:** https://junie.jetbrains.com/docs/junie-cli-hooks.html , https://junie.jetbrains.com/docs/junie-cli-mcp-configuration.html

### 2.11 Devin CLI (Cognition): P3; reads Claude hooks
- **Version:** v3000.10.31 (2026-09-16).
- **Config:** `.devin/hooks.v1.json` (unwrapped), the `hooks` key in `.devin/config.json` or `~/.config/devin/config.json`, **plus Claude files** (`~/.claude/settings.json` etc.). Opt out with `"read_config_from":{"claude":false}`.
- **Events:** PreToolUse, PostToolUse, PermissionRequest, UserPromptSubmit, Stop, **PostCompaction** (`summary`), SessionStart (`source`), SessionEnd. The shell tool is `exec`.
- **stdin:** includes `session_id` and `prompt_id`.
- **stdout:**
  - `hookSpecificOutput.additionalContext` works on UserPromptSubmit (B1), SessionStart and PostToolUse.
  - B2: ✗.
  - B3: ✗. There is no PreCompact; use PostCompaction for observation.
  - B4: ◐ via PostCompaction or SessionStart.
- **MCP:** `~/.config/devin/mcp_config.json`, or `devin mcp add -s user subcortex -- subcortex mcp`.
- **Source:** docs.devin.ai/cli/extensibility/hooks/{overview,lifecycle-hooks}.md

### 2.12 Mistral Vibe (`vibe`): P3, B2 only
- **Version:** v2.25.5 (2026-09-18).
- **Config:** `~/.vibe/hooks.toml` (under `VIBE_HOME`) or `<proj>/.vibe/hooks.toml` (trusted projects).
  ```toml
  [[hooks]]
  name = "subcortex-post"
  type = "post_tool"
  match = "bash"
  command = "subcortex hook vibe post_tool"
  timeout = 60.0
  ```
- **Events:** only `post_agent`, `pre_tool`, `post_tool`.
- **stdin:**
  - Common: `session_id`, `parent_session_id`, `transcript_path`, `cwd`, `hook_event_name`.
  - `post_tool` adds `tool_name`, `tool_call_id`, `tool_input`, `tool_status`, `tool_output`, `tool_output_text`, `tool_error`, `duration_ms`.
- **stdout:** empty (passthrough) or snake_case JSON `{decision, reason, system_message, hook_specific_output}`.
  - **B2: `{"decision":"deny","reason":"<head…marker…tail>"}` on `post_tool` replaces `tool_output_text` with `reason`. It does not block.**
  - `hook_specific_output.additional_context` only appends.
  - B1, B3 and B4: ✗.
- **Avoid:** **never set `strict=true`**. With it, a failing post_tool hook clears the output to "".
- **MCP:** `[[mcp_servers]] name="subcortex" transport="stdio" command="subcortex" args=["mcp"]` in `config.toml`.
- **Source:** github.com/mistralai/mistral-vibe README "Hooks", `vibe/core/hooks/{models,_post_tool,manager}.py`

### 2.13 Snowflake Cortex Code (`cortex`): P3, UNVERIFIED version
- **Config:** `.cortex/settings(.local).json`, `~/.snowflake/cortex/hooks.json`, **plus Claude files**.
- **Events:** PreToolUse, PostToolUse, PermissionRequest, UserPromptSubmit, SessionStart, SessionEnd, PreCompact, Stop, SubagentStop, Notification, Setup. Tool names are lowercase and case-sensitive (`bash`).
- **Documented output:** `decision`, `systemMessage`, `hookSpecificOutput.{updatedInput, additionalContext, permissionDecision}`. B2 replacement is UNVERIFIED.
- **MCP:** `~/.snowflake/cortex/mcp.json`.
- **Source:** https://docs.snowflake.com/en/user-guide/cortex-code/extensibility

### 2.14 Every Code (`coder`, just-every/code): P4
- **Version:** v0.6.191 (2026-09-16). The shipped binary is `code-rs`. Its `codex-rs/` is only a read-only mirror, so **Codex hooks.json does not apply**.
- **Config:** `~/.code/config.toml` (`CODE_HOME`), **per exact resolved cwd only**. There is no global table.
  ```toml
  [[projects."/abs/repo".hooks]]
  event = "user.prompt_submit"
  run = ["subcortex","hook","everycode","user.prompt_submit"]
  timeout_ms = 5000
  ```
- **Events:** `session.start`, `session.end`, `user.prompt_submit`, `tool.before`, `tool.after`, `file.before_write`, `file.after_write`, `stop`.
- **Payload arrives in an env var, not on stdin:** `CODE_HOOK_PAYLOAD` (JSON). Related vars: `CODE_HOOK_EVENT`, `CODE_SESSION_CWD`, and others. The `tool.after` payload truncates stdout and stderr to 2048 bytes.
- **Output:**
  - B1: plain `user.prompt_submit` stdout is injected as a developer message.
  - `tool.after` and `session.start` stdout is ignored.
  - B2, B3 and B4: ✗.
- **Avoid:** exit 2 with non-empty stderr (blocks).
- **Caveat:** hooks run inside the session sandbox, where the network is off under `workspace-write`, so HTTP to 127.0.0.1:7707 may fail. Use a file or Unix socket instead (UNVERIFIED).
- **MCP:** `[mcp_servers.subcortex] command="subcortex" args=["mcp"]`.
- **Source:** github.com/just-every/code `docs/config.md#project-hooks`

### 2.15 Cline CLI (`cline` 3.0.62): file hooks exist, but use the TS plugin
- **File hooks:** the filename is the event (`~/.cline/hooks/PostToolUse` with a shebang), also in `.cline/hooks/` or `$CLINE_HOOKS_DIR`.
  - Events: TaskStart, TaskResume, TaskCancel, TaskComplete, TaskError, PreToolUse, PostToolUse, UserPromptSubmit, PreCompact (**mapped to undefined, never fires**), SessionShutdown.
  - stdout JSON: `{cancel, contextModification, errorMessage, review, overrideInput}`. Exit codes never block; only `cancel:true` does.
  - UserPromptSubmit runs detached. PostToolUse only appends `<hook_context>`. Not useful here.
- **TS plugin:** `~/.cline/plugins/subcortex.ts` (or `cline plugin install <path>`).
  - `afterTool` returns `{result}`, which **replaces the tool result** (B2).
  - `beforeModel` returns `messages`, which rewrites the history sent to the model (B1 and B4). Compaction is customized via `registerMessageBuilder()` (B3 via a custom builder, UNVERIFIED).
  - Set `failureMode:"fail_open"`. Never return `stop`, `skip`, or `cancel`.
- **MCP:** `~/.cline/data/settings/cline_mcp_settings.json`, `{"mcpServers":{"subcortex":{"command":"subcortex","args":["mcp"],"disabled":false,"autoApprove":[]}}}`.
- **Sources:** https://docs.cline.bot/sdk/plugins.md , https://docs.cline.bot/getting-started/config.md

### 2.16 Plugin-only seams worth an adapter: Kilo and Pi
- **Kilo Code CLI 7.7.6** is an OpenCode fork (`packages/opencode`). **Reuse the OpenCode plugin**, but install it at `~/.config/kilo/plugin/subcortex.ts`; Kilo does not read `.opencode/`. The module shape is `export default { id:"subcortex", server: async (ctx) => ({…hooks}) }`.
  - B1: `chat.message` (mutate or push `output.parts`).
  - B2: `tool.execute.after` (rewrite `output.output`).
  - B3: `experimental.session.compacting` (`output.context[]`).
  - B4: the `event` hook fires `session.compacted`, then use `experimental.chat.system.transform` or `experimental.chat.messages.transform`.
  - Never throw in `tool.execute.before`.
  - MCP: `{"mcp":{"subcortex":{"type":"local","command":["subcortex","mcp"],"enabled":true}}}` in `kilo.jsonc`.
- **Pi v0.87.0** (`@earendil-works/pi-coding-agent`) loads extensions from `~/.pi/agent/extensions/subcortex.ts`.
  - B1: `before_agent_start` → `{message:{customType,content,display}}`.
  - B2: `tool_result` → partial `{content,details,isError}`.
  - B3: `session_before_compact` (`reason` manual|threshold|overflow).
  - B4: flag it on `session_compact`, then inject on the next `before_agent_start`.
  - Avoid `{block:true}`, `{cancel:true}`, `{action:"handled"}`.
  - **No MCP.**

---

## 3. Already covered elsewhere (not repeated here)
All of the following have real command hooks or plugins, per the earlier research files `agent-12_*` and `agent-13_*`:
- Claude Code, Codex CLI and OpenCode: existing adapters.
- Gemini CLI: BeforeAgent, AfterTool, PreCompress, SessionStart.
- Qwen Code: Claude-style, incl. PreCompact and PostCompact.
- Cursor CLI: hooks.json with beforeSubmitPrompt, afterShellExecution, preCompact, sessionStart.
- GitHub Copilot CLI: userPromptSubmitted, postToolUse, preCompact, sessionStart.
- Kimi Code CLI.
- Charm Crush.
- Goose.
- OpenHands.
- Amp: `amp.md` (plugin-only).
- Aider: `aider.md` (no seam).

---

## 4. Conflicts and unverified points
1. **Kiro exit codes and timeout.**
   - Exit codes: the docs tab as I fetched it says *any non-zero* exit blocks prompt submit and preToolUse; another reading says only exit 2.
   - Default timeout: 60 s on the actions page, 30 s elsewhere.
   - Which harness (V2 or V3) is the default is also unverified.
   - Mitigation: always exit 0, and set `timeout_ms` explicitly.
2. **Open Interpreter.** Whether PostToolUse fires for shell commands with non-zero exit. The `continue:false` replacement semantics come from a source reading of `registry.rs` and should be tested once.
3. **Cline.** Parallel research passes disagreed on whether 3.x has file hooks. Group A verified them against the `@cline/core` 0.0.83 bundle, and that result is used here. B3 via `registerMessageBuilder` is untested.
4. **Auggie.** PromptSubmit is fire-and-forget according to a bundle reading of 0.36.0. It could change in 0.37.
5. **Continue `cn`.** Hook code exists (PR #11029), but nothing calls it as of `main` 5522c6f (2026-07-20) and bundle 1.5.47. If it gets wired, it will run hooks from `~/.claude/settings.json`.
6. **Other gaps:**
   - Cortex Code: version, and whether it supports B2.
   - Kode and Neovate: config paths.
   - Codebuff: absence of hooks.
   - Rovo Dev: version and event names.
   - Docker Agent: field names beyond those quoted.
   - CodeBuddy: whether PreToolUse uses `modifiedInput` or `updatedInput`.
7. **Grok Build.** Stdout from an allowing hook is discarded, so do not rely on B1. `updatedToolOutput` must match the tool's tagged result shape, or it is presumably ignored (UNVERIFIED).
