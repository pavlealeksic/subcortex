# Cline CLI (`cline`, `@cline/core`): subcortex adapter spec

Verified 2026-09-21 against the source at git tag `cli-v3.0.62` in https://github.com/cline/cline (commit `d718dd16`, the same commit as `sdk/core/v0.0.83`). I also checked the published npm tarballs:
- `cline@3.0.62`: a Node wrapper that launches the compiled binary
- `@cline/core@0.0.83`, `@cline/shared@0.0.83`, `@cline/agents@0.0.83`, `@cline/llms@0.0.83`, `@cline/sdk@0.0.83`

The shipped `dist/*.d.ts` and bundles match the quoted source. For example, `hookTimeoutMs,3000` appears in `@cline/core/dist/index.js`, `dist/hub/index.js` and `dist/hub/daemon/entry.js`.

Nothing was installed and the compiled `@cline/cli-<platform>` binary was not run. The plugin in §6 was transpiled with Bun and smoke-tested against a fake daemon (`sketch/smoke.test.ts`: 3/3 pass, including a daemon-hang test).

**Verdict:** write a TS plugin adapter (a "Cline Plugin", `AgentPlugin` with `capabilities: ["hooks"]`) and use only `hooks.beforeModel` and `hooks.afterTool`.
- Cline's file hooks (`~/.cline/hooks/<Event>`) cannot replace output. Their `PreCompact` event is mapped to `undefined`.
- **Fail-open is entirely the plugin's job.** Cline does not catch plugin hook errors, and a sandboxed hook that takes over 3000 ms fails the whole run.

| Behavior | Seam (cline 3.0.62 / core 0.0.83) | Status |
|---|---|---|
| 1. Hidden hint on prompt submit | `hooks.beforeModel` returns `{messages}` with the hint appended as an extra text part on the user's prompt message | Native. Applies to the request only and is never persisted, so it is re-applied on every request |
| 2. Replace successful shell output | `hooks.afterTool` returns `{result}` for tool `run_commands`, per entry with `success === true` | Native |
| 3. Pre-compaction snapshot | No plugin hook fires before compaction. Compaction projects the request only, so `snapshot.messages` in the first post-compaction `beforeModel` is still the full, uncompacted transcript | Workaround with the same content: snapshot at the first `beforeModel` where a new `compaction_summary` message appears |
| 4. Re-inject after compaction | Same `beforeModel`: `/v1/restore`, then append the text to the compaction-summary message on every request | Native, in the very next model request |
| MCP | `cline_mcp_settings.json` (`mcpServers`), or a plugin with the `mcp` capability | Works |

---

## 1. Sources, latest version, version detection

| Item | Value |
|---|---|
| CLI npm | `cline` **3.0.62** (`latest`; `nightly` is `3.0.62-nightly.1789993298`, modified 2026-09-21T12:25Z). It is a Node resolver script (`bin/cline`) that `spawnSync`s the Bun-compiled binary from `@cline/cli-{darwin,linux,windows}-{arm64,x64}@3.0.62` |
| SDK npm | `@cline/core`, `@cline/shared`, `@cline/agents`, `@cline/llms`, `@cline/sdk`, all **0.0.83** (`latest`, 2026-09-20). Ignore `@cline/cli@0.0.13`: it is stale, from the old `cline/sdk` repo |
| Repo and tags | https://github.com/cline/cline. `cli-v3.0.62` → `d718dd16f850c4c915a8214441a831e00cb28c75`; `sdk/core/v0.0.83` → same commit. CLI in `apps/cli`, SDK in `sdk/packages/{shared,core,agents,llms,sdk}` |
| Hook types | `sdk/packages/shared/src/agent.ts` (l.347–470: hook contexts, results, `AgentRuntimeHooks`) |
| Plugin type | `sdk/packages/shared/src/agents/types.ts` l.518 (`AgentExtension`, exported as `AgentPlugin` from `@cline/core`); `sdk/packages/shared/src/extensions/contribution-registry.ts` (`AgentExtensionApi`, `PluginManifest`, capability validation) |
| Discovery | `sdk/packages/shared/src/storage/paths.ts` (`resolveClineDir` l.151, `resolvePluginConfigSearchPaths` l.562, `discoverPluginModulePaths` l.746, `resolveMcpSettingsPath` l.440); `sdk/packages/core/src/extensions/plugin/plugin-config-loader.ts` |
| Sandbox | `sdk/packages/core/src/extensions/plugin/plugin-sandbox.ts` (hook timeout l.335, `makeHookHandler` l.788); `plugin-sandbox-bootstrap.ts`; `sdk/packages/core/src/runtime/tools/subprocess-sandbox.ts` |
| Hook dispatch | `sdk/packages/core/src/runtime/orchestration/session-runtime-orchestrator.ts` (`mergeRuntimeHooks` l.174, `createRuntimeHooks` l.1027); `sdk/packages/agents/src/agent-runtime.ts` (`beforeModel` loop l.1230, `afterTool` loop l.2033, run catch l.903, `applyStopControl` l.2205) |
| Compaction | `sdk/packages/core/src/extensions/context/compaction.ts`, `compaction-shared.ts` (`buildSummaryMessage` l.756, `isCompactionSummaryMessage` l.204); `sdk/packages/core/src/runtime/host/local-runtime-host.ts` (sidecar compaction state) |
| Shell tool | `sdk/packages/core/src/extensions/tools/definitions.ts` (`run_commands` l.514, `executeShellCommands` l.180); `executors/bash.ts`, `executors/output-limits.ts` |
| Docs (repo `docs/`, published at docs.cline.bot) | `docs/sdk/plugins.mdx`, `docs/customization/plugins.mdx`, `docs/sdk/guides/writing-plugins.mdx`, `docs/cli/cli-reference.mdx`, `apps/cli/README.md`. Example plugins in `sdk/examples/plugins/` (`custom-compaction.ts`, `telemetry.ts`, …) |

**Version detection.**
- `cline --version` (or `-V`) prints the version (commander `.version(version, "-V, --version")`). `cline version` also works.
- The SDK version that runs plugins equals the `@cline/core` dependency pinned by the `cline` package: 3.0.62 → 0.0.83.
- Recommended minimum is **3.0.62**, the only version verified. Older 3.0.x may differ: the plugin sandbox and hook proxy changed repeatedly (see the CLI CHANGELOG entries for 3.0.9, 3.0.15, 3.0.55 and 3.0.62).

**The sweep's claims, corrected.**
- **No hook policies.** The doc page `docs/sdk/plugins.mdx` lists "Hook Policies" (`timeoutMs`, `failureMode: "fail_open"`, …). **Nothing in `sdk/packages` implements them**: a grep for `failureMode` or `fail_open` finds zero hits. Plugin hooks have no per-hook policy.
- **No `registerMessageBuilder` for B3.** It is not used for B3 (see §3.3).

---

## 2. Install, discovery, module format, runtime, isolation, non-interactive runs

### 2.1 Where plugin files live and how they load

`resolvePluginConfigSearchPaths(workspacePath)` in `paths.ts` l.562 returns, in order:

```ts
workspacePath ? join(workspacePath, ".cline", "plugins") : "",   // project
join(resolveClineDir(), "plugins"),                               // global: $CLINE_DIR or --config <dir> or ~/.cline
resolveDocumentsExtensionPath("Plugins"),                         // ~/Documents/Cline/Plugins
```

- **Auto-load, no registration needed.** `discoverPluginModulePaths` scans each directory **recursively** and loads every `*.ts` / `*.js` file.
  - It skips `node_modules` and dot-entries.
  - A subdirectory whose `package.json` declares `"cline": {"plugins": [...]}` contributes only those entries.
  - Directories that look like Agent Plugins (`~/.agents/plugins`) are skipped.
- **Installer target:** a single file, `$CLINE_DIR/plugins/subcortex.ts` (default `~/.cline/plugins/subcortex.ts`). **Never put helper `.ts`/`.js` files beside it**, because each one would be imported as a plugin. The loader imports a module *before* it validates the module.
- **CLI alternative:** `cline plugin install <path|npm:…|git…|https://…ts> [--cwd <dir>] [--force] [--json]`. This copies into `~/.cline/plugins/_installed/{local,npm,git,remote}/…`, or into `<dir>/.cline/plugins` with `--cwd`. `cline config` shows loaded plugins.
- **Enable/disable:** everything is enabled by default. Disabled plugin paths live in global settings (`filterDisabledPluginPaths`, `core/src/services/global-settings.ts` l.499).
- **When plugins load.** They are loaded per session in `core/src/services/local-runtime-bootstrap.ts` ~l.443 with `resolveAndLoadAgentPlugins({...})`. No `mode` is passed there, so the mode is **sandbox**; `in_process` exists only for SDK callers. A load failure is logged and the session continues: `plugin loading failed; continuing without plugins`, plus per-plugin `failures`.

### 2.2 Module format

`plugin-loader.ts` and `plugin-sandbox-bootstrap.ts` l.533 read the plugin from `moduleExports.default ?? moduleExports["plugin"]`. The export must be an object with a non-empty `name` and a `manifest` whose `capabilities` is a non-empty string array.

```ts
// shared/src/agents/types.ts l.518   (re-exported as `AgentPlugin` from "@cline/core")
export interface AgentExtension extends ContributionRegistryExtension<AgentTool, Message[]> {
	name: string;
	manifest: PluginManifest;
	hooks?: AgentExtensionHooks;            // = Partial<AgentRuntimeHooks>
	setup?: (api: AgentExtensionApi<AgentTool, Message[]>, ctx: AgentExtensionContext) => void | Promise<void>;
}
// shared/src/extensions/contribution-registry.ts l.204/219
const ExtensionCapabilityOptions = ["hooks","tools","commands","rules","skills","messageBuilders","providers","automationEvents","mcp"] as const;
export interface PluginManifest { paths?: string[]; capabilities: AgentExtensionCapability[]; providerIds?: string[]; modelIds?: string[]; }
// normalizeManifest(): "runtime hooks require the \"hooks\" capability"
```

- **Types:** `import type { AgentPlugin } from "@cline/core"`. This is type-only.
- At runtime the host provides `@cline/sdk`, `@cline/core`, `@cline/agents`, `@cline/llms` and `@cline/shared` (`plugin-module-import.ts` `HOST_PROVIDED_SDK_SPECIFIERS`). The plugin needs none of them.
- `setup(api, ctx)` is optional. `ctx.session?.sessionId` is the root session id.
- The example plugins warn: "If your host runs multiple sessions in one process, key any *data* by ctx.session?.sessionId". Hooks do not receive `ctx`, so the sketch keys its state by `snapshot.conversationId`.

### 2.3 Runtime: a sandbox subprocess over JSON IPC

`loadSandboxedPlugins` (`plugin-sandbox.ts` l.308) spawns **one sandbox subprocess per session, shared by all plugins in that session**.
- **Spawn details** (`subprocess-sandbox.ts` ~l.265):
  - It uses `stdio: ["ignore","ignore","pipe","ipc"]`, so plugin stdout is discarded and stderr is captured.
  - It uses `env: withResolvedClineBuildEnv(process.env)`, so **the plugin inherits the host's environment**.
- **Which JS runtime runs it.** The executable is `$CLINE_JS_RUNTIME_PATH`, else `process.execPath` if that is node or bun, else `$BUN_EXEC_PATH`, `$npm_node_execpath`, `$NODE`, else **`node` from PATH**. For the compiled `cline` binary, `process.execPath` is `cline`, so in practice the plugin probably runs under **Node** (UNVERIFIED). Write plugin code for both runtimes: `fetch`, `AbortController`, no Bun or Node-only APIs.
- TypeScript is loaded through jiti.
- **Timeouts** (`plugin-sandbox.ts` l.330–340):
  - import: `CLINE_PLUGIN_IMPORT_TIMEOUT_MS`, default 4000 ms
  - **hooks: 3000 ms, hard-coded, with no env var and no CLI flag**
  - contributions (tools, message builders, commands): 60 000 ms
  - idle reclaim: `CLINE_PLUGIN_IDLE_TIMEOUT_MS`, default 30 min
- Hook contexts and results cross the process boundary as JSON. Return plain data only.
- **Where the host runs.** In the default backend mode (`auto`: CLI → local **Hub daemon**), sessions and their plugin sandboxes run inside the Hub process. The plugin then sees the *Hub's* environment, not that of the `cline` invocation.
  - `--data-dir`, `CLINE_SANDBOX=1` or `--yolo` force the local backend (`apps/cli/src/runtime/run-agent.ts` l.173: `forceLocalBackend: isYoloMode || config.sandbox === true`).
  - `CLINE_SESSION_BACKEND_MODE=local` does the same (`core/src/runtime/host/host.ts`).

### 2.4 Environment variables and flags for an isolated e2e run

| Env / flag | Effect (source) |
|---|---|
| `HOME=<tmp>` | `setHomeDir(homedir())` in `apps/cli/src/main.ts` l.160. Relocates `~/.cline` and `~/Documents/Cline/Plugins` |
| `CLINE_DIR=<dir>` or `--config <dir>` | Config dir: plugins, hooks, rules and so on (`resolveClineDir`). `--config` is applied before commander parses |
| `--data-dir <dir>` | "Use isolated local state" and turns on sandbox mode. `configureSandboxEnvironment` sets `CLINE_SANDBOX=1`, `CLINE_DATA_DIR`, `CLINE_DB_DATA_DIR`, `CLINE_SESSION_DATA_DIR`, `CLINE_TEAM_DATA_DIR` and `CLINE_PROVIDER_SETTINGS_PATH=<dir>/settings/providers.json`. **Forces the local backend (no Hub)** |
| `CLINE_DATA_DIR`, `CLINE_PROVIDER_SETTINGS_PATH`, `CLINE_MCP_SETTINGS_PATH`, `CLINE_GLOBAL_SETTINGS_PATH` | Individual overrides (`paths.ts` l.179–447) |
| `CLINE_SESSION_BACKEND_MODE=local` | Forces the local backend |
| `CLINE_NO_AUTO_UPDATE=1` | Skips the startup auto-update (`commands/update.ts` l.312) |
| `CLINE_DISABLE_CLINE_PASS_NOTICE=1` | Suppresses the startup notices (3.0.62 changelog) |
| `CLINE_LOG_ENABLED=0`, `CLINE_LOG_PATH` | Runtime file log |
| `CLINE_PLUGIN_IMPORT_TIMEOUT_MS`, `CLINE_PLUGIN_IDLE_TIMEOUT_MS`, `CLINE_JS_RUNTIME_PATH` | Plugin sandbox knobs (not the 3 s hook timeout) |
| `CLINE_HOOKS_DIR` / `--hooks-dir` | Extra file-hook dir (not used by this adapter) |

### 2.5 Non-interactive runs

`apps/cli/README.md` and `commands/program.ts`:
- `cline "prompt"` runs one-shot and exits. Tools are auto-approved by default.
- Add `--json` for NDJSON events, `-P/--provider <id>`, `-m/--model <id>`, `-k/--key <api-key>`, `-c/--cwd`, `-t/--timeout <s>`, `--compaction agentic|basic|off` (default `agentic`), and `--id <session-id>` to continue a session.
- Prompt text must be a single argv token.
- **Avoid `--yolo` in e2e.** It enables `submit_and_exit`, which has `lifecycle.completesRun: true`. That makes `runtime-builder.ts` ~l.806 set `completionPolicy.requireCompletionTool`, so a plain-text answer from the mock never ends the run. Use `--data-dir` to get the local backend instead.

### 2.6 Pointing Cline at a mock LLM

The provider id `openai-compatible` (alias `openai`) uses AI SDK openai-compatible and POSTs `${baseUrl}/chat/completions` with streaming. `anthropic` uses `@ai-sdk/anthropic`, whose default base URL is `https://api.anthropic.com/v1`, so a custom `baseUrl` must **include `/v1`**. Settings are stored in `providers.json`; the schema is in `core/src/types/provider-settings.ts` and `core/src/services/llms/provider-settings.ts` l.142.

```sh
# either use the non-interactive auth command…
cline auth --provider openai-compatible --apikey sk-e2e --modelid mock-model \
           --baseurl "http://127.0.0.1:$PORT/v1" --config "$T/cline" --data-dir "$T/data"
```

```jsonc
// …or write <data-dir>/settings/providers.json yourself (StoredProviderSettingsSchema)
{"version":1,"lastUsedProvider":"openai-compatible","modes":{},
 "providers":{"openai-compatible":{"settings":{"provider":"openai-compatible","apiKey":"sk-e2e","model":"mock-model",
   "baseUrl":"http://127.0.0.1:PORT/v1","contextWindow":64000,"maxTokens":4096},
   "updatedAt":"2026-09-21T00:00:00.000Z","tokenSource":"manual"}}}
```

**Isolated e2e recipe.** Pieces marked UNVERIFIED have not been run.

```sh
T=$(mktemp -d); mkdir -p "$T/home" "$T/cline/plugins" "$T/data/settings" "$T/work"
cp cline-subcortex.ts "$T/cline/plugins/subcortex.ts"      # what `subcortex install cline` would write
printf '%s' "$PROVIDERS_JSON" > "$T/data/settings/providers.json"
cd "$T/work" && git init -q
HOME="$T/home" CLINE_DIR="$T/cline" CLINE_NO_AUTO_UPDATE=1 CLINE_DISABLE_CLINE_PASS_NOTICE=1 \
SUBCORTEX_URL="http://127.0.0.1:$DAEMON_PORT" \
  cline --config "$T/cline" --data-dir "$T/data" --json -P openai-compatible -m mock-model \
        "run the build" </dev/null
```

- **Mock script for Cline's shell tool:** `{"tool": "run_commands", "input": {"commands": ["yes 'compiling module ok' | head -3000"]}}`, then `{"text": "Done."}`.
- Command output is capped at **48 000 chars**, middle-elided (`executors/output-limits.ts` `MAX_COMMAND_OUTPUT_CHARS`).
- **Assertions:**
  - The first request with tools contains the hint, inside the same user message as `<user_input mode="act">run the build</user_input>`.
  - The second request contains `[subcortex: truncated`. The tool result content is the JSON of `ToolOperationResult[]`.
- **Compaction e2e (UNVERIFIED tuning).** Use `--compaction basic`, which is deterministic and makes no summarizer LLM call. Set a small `contextWindow` in providers.json and produce a large tool output.
  - The trigger is 0.9 of the usable input budget, which is 0.9 of `contextWindow` (`compaction-shared.ts` l.13–19).
  - Cline's system prompt and tool schemas are large, so tune the window so the first request fits and the second does not.
  - Then assert that `/v1/snapshot` and `/v1/restore` were hit and that the next agent request contains the restored text next to `Context summary:`.
- The mock's `GET …/models` handler is useful: shared-catalog providers may refresh model lists (3.0.62 changelog; UNVERIFIED for `openai-compatible`).

---

## 3. Exact hook API per behavior

All seven runtime hooks, quoted from `shared/src/agent.ts` l.347–470:

```ts
export interface AgentBeforeModelContext { snapshot: AgentRuntimeStateSnapshot; request: AgentModelRequest; }
export interface AgentStopControl { stop?: boolean; reason?: string; }
export interface AgentBeforeModelResult {
	stop?: boolean; reason?: string;
	messages?: readonly AgentMessage[];
	tools?: readonly AgentToolDefinition[];
	options?: Record<string, unknown>;
}
export interface AgentBeforeToolResult {
	skip?: boolean; stop?: boolean; reason?: string; input?: unknown; policy?: ToolPolicy;
	appendContext?: string;   // appended after this iteration's tool results as a `<hook_context>` user message
}
export interface AgentAfterToolContext {
	snapshot: AgentRuntimeStateSnapshot; tool: AgentTool; toolCall: AgentToolCallPart; input: unknown;
	result: AgentToolResult; startedAt: Date; endedAt: Date; durationMs: number;
}
export interface AgentAfterToolResult { stop?: boolean; reason?: string; result?: AgentToolResult; appendContext?: string; }
export interface AgentRuntimeHooks {
	beforeRun?:  (context: AgentRunLifecycleContext) => AgentStopControl | undefined | Promise<AgentStopControl | undefined>;
	afterRun?:   (context: AgentRunLifecycleContext & { result: AgentRunResult }) => void | Promise<void>;
	beforeModel?:(context: AgentBeforeModelContext) => AgentBeforeModelResult | undefined | Promise<AgentBeforeModelResult | undefined>;
	afterModel?: (context: AgentAfterModelContext) => AgentStopControl | undefined | Promise<AgentStopControl | undefined>;
	beforeTool?: (context: AgentBeforeToolContext) => AgentBeforeToolResult | undefined | Promise<AgentBeforeToolResult | undefined>;
	afterTool?:  (context: AgentAfterToolContext) => AgentAfterToolResult | undefined | Promise<AgentAfterToolResult | undefined>;
	onEvent?:    (event: AgentRuntimeEvent) => void | Promise<void>;
}
// shared/src/agent.ts l.122/151/183/217
export interface AgentMessage { id: string; role: "user" | "assistant" | "tool"; content: AgentMessagePart[]; createdAt: number; metadata?: Record<string, unknown>; modelInfo?: {...}; metrics?: {...}; }
export interface AgentRuntimeStateSnapshot { agentId: string; agentRole?: string; parentAgentId?: string | null; conversationId?: string; runId?: string;
	status: AgentRunStatus; iteration: number; messages: readonly AgentMessage[]; pendingToolCalls: readonly string[]; usage: AgentUsage; lastError?: string; lastErrorClass?: ProviderErrorClass; }
export interface AgentToolResult<TOutput = unknown> { output: TOutput; isError?: boolean; metadata?: Record<string, unknown>; }
export interface AgentModelRequest { systemPrompt?: string; messages: readonly AgentMessage[]; tools: readonly AgentToolDefinition[]; modelTools?: …; signal?: AbortSignal; options?: Record<string, unknown>; }
// parts: { type:"text", text } | reasoning | image | file | media | { type:"tool-call", toolCallId, toolName, input } | { type:"tool-result", toolCallId, toolName, output, isError? }
```

**Dispatch order.** `SessionRuntime.createRuntimeHooks()` (l.1027) merges the host hooks and every plugin's hooks with `mergeRuntimeHooks` (l.174). For `beforeModel`, it then passes the (possibly replaced) messages through `prepareMessagesForModelRequest`: plugin `messageBuilders` first, then `messageBuilder.buildForApi`.

**One model call inside `AgentRuntime`** (`agents/src/agent-runtime.ts` ~l.1180–1245) runs these steps in order:
1. `request.messages = clone(state.messages)`, plus a pending steer message when `iteration > 1`.
2. `prepareTurnForModelRequest`, which is the **compaction** pipeline.
3. The `beforeModel` hooks.
4. The provider stream.

`state.messages` is the canonical transcript. Compaction only changes the request.

### 3.1 Behavior 1: `beforeModel` (append hidden context to the user's turn)

**Why `beforeModel` and not another hook:**
- `beforeRun` runs *before* the new user message is pushed (`execute()` ~l.733: `callBeforeRunHooks()` comes before `normalizeInput(input)` is pushed). It can only return a stop control.
- `registerRule` changes the system prompt.
- `appendContext` only fires after tool results.
- The file hook `UserPromptSubmit` runs detached (sweep).
- A message builder has a 60 s timeout but no snapshot or iteration.

**Mechanics:**
1. Identify the user's prompt message in `ctx.request.messages`:
   - `role === "user"` with at least one `text` part
   - not `metadata.kind === "compaction_summary"`
   - not `metadata.displayRole === "system"`, which marks `<hook_context>` blocks (agent-runtime ~l.865)
   - not `metadata.userRunSpan === 0`, which marks runtime reminders (~l.705) and codec-split segments
2. Its text looks like `<user_input mode="act">…</user_input>`, optionally with `<mode_notice>…</mode_notice>` (`shared/src/prompt/format.ts`). Strip both to get the prompt.
3. Call `/v1/prompt-hint` once per new prompt text; the cache is keyed by that text.
4. Return `{ messages: request.messages.map(m => m === prompt ? {...m, content: [...m.content, {type:"text", text: hint}]} : m) }`.

**`messages` replaces the request's message list** (`mergeRuntimeHooks`: `request = {...request, messages: result.messages}`). Always return the **full** list; `[]` would send an empty conversation.

**The result is not persisted.**
- `AgentRuntimeConfig.prepareTurn` doc: "Returned messages affect only the provider request for the current call. They do not replace the canonical runtime transcript…".
- The same applies to `beforeModel`: `state.messages` is untouched.
- So the hint must be re-applied on every request, including later runs, to keep the prompt prefix stable for caching. The sketch caches hints by prompt text, up to 256 entries.

Only the root agent is handled (`snapshot.parentAgentId == null`). Spawned sub-agents and teammates run the same hooks.

### 3.2 Behavior 2: `afterTool` (replace a tool result)

`agent-runtime.ts` l.2033:

```ts
if (prepared.tool) {
	for (const hook of this.hooks.afterTool) {
		const after = (await hook({ snapshot: this.snapshot(), tool: prepared.tool, toolCall: prepared.toolCall,
			input: prepared.input, result, startedAt, endedAt, durationMs })) as AgentAfterToolResult | undefined;
		if (after?.appendContext?.trim()) { this.pendingHookContexts.push(formatHookContextBlock("PostToolUse", prepared.toolCall, after.appendContext)); }
		this.applyStopControl(after);
		if (after?.result) { result = after.result; }
	}
}
const message = createMessage("tool", [{ type: "tool-result", toolCallId, toolName, output: result.output, isError: result.isError }]);
```

Returning `{result}` **replaces the whole `AgentToolResult`**, `{output, isError?, metadata?}`, that the model sees and that is persisted.

**The shell tool is `run_commands`** (`DefaultToolNames.RUN_COMMANDS`).
- Input is `{commands: string[] | structured[]}`, plus lenient variants (`RunCommandsInputUnionSchema`).
- Output is **`ToolOperationResult[]`, one entry per command** (`core/src/extensions/tools/types.ts` l.27):

  ```ts
  export interface ToolOperationResult { query: string; result: unknown; error?: string; success: boolean; duration?: number; }
  ```

- In `executeShellCommands` (l.180), a command that exits 0 yields `{query, result: output, success: true}`.
  - A non-zero exit gives `CommandExitError` → `{query, result: error.output, error: "Command exited with code N", success: false}`.
  - A timeout or other failure gives `{query, result: "", error: "Command failed: …", success: false}`.
  - The tool-level `result.isError` stays unset in all these cases. It is only `true` when the tool itself threw (for example a validation error).
- So "successful shell command" means **`toolName === "run_commands"`, the entry has `success === true`, and `typeof entry.result === "string"`**.
- **Return value:** `{ result: { ...ctx.result, output: entries.map(e => trimmed ? {...e, result: replacement} : e) } }`. Keep `isError` and `metadata` as they are.

### 3.3 Behavior 3: pre-compaction snapshot (no plugin hook, same-content workaround)

**Seams that don't work:**
- File hook `PreCompact` → `undefined` (`core/src/hooks/hook-file-config.ts` l.41), so it never fires.
- `CoreSessionConfig.compaction.compact(context)` is an SDK/`ClineCore.start` config callback. File plugins can't set it.
- Compaction does emit `status-notice` events (`"auto-compacting"` / `"compacting"` with `metadata.phase: "started"`, then `"…compacted"` with `phase: "completed"`; `compaction.ts` ~l.410/560). But:
  - They are sent as `void this.emit({...})` (agent-runtime l.1702–1709), fire-and-forget. A rejection becomes an unhandled promise rejection.
  - Receiving them needs `hooks.onEvent`, which is **awaited for every runtime event, including every `assistant-text-delta` token** (agent-runtime ~l.1301 and l.2200). In sandbox mode each one is a JSON IPC round trip carrying the full snapshot. **Do not register `onEvent`.**

**How compaction works.**
- It runs in `prepareTurn` right before `beforeModel`. It is a **sidecar projection**: `SessionCompactionState`, persisted per session and re-projected onto the canonical transcript each turn (`local-runtime-host.ts` ~l.700–720, `session/models/session-compaction.ts`).
- The projected request contains a summary message built by `buildSummaryMessage` (`compaction-shared.ts` l.756):

  ```ts
  { role: "user", content: [{ type: "text", text: `Context summary:\n\n${summary}` }],
    metadata: { kind: "compaction_summary", displayRole: "system", userRunSpan, summary, details: fileOps, tokensBefore, generatedAt: Date.now() } }
  ```

- `messagesToAgentMessages` keeps `metadata`.
- Meanwhile `ctx.snapshot.messages` is **still the full uncompacted transcript**, and no new messages are added between compaction and `beforeModel`.

**Workaround.**
1. In `beforeModel`, locate the last message with `metadata.kind === "compaction_summary"`. Key it by `generatedAt`, `tokensBefore` and the text.
2. Keep the previously seen key per `snapshot.conversationId`.
3. When the key changes from what was seen on an earlier request, POST `/v1/snapshot` with `{session_id: conversationId, messages: snapshot.messages.slice(-20)}` (raw `AgentMessage` objects). That content is identical to a pre-compaction snapshot.
4. The first request seen for a conversation only records a baseline. This covers resume and sandbox respawn, so an old summary is not mistaken for a new compaction.

**What this covers:** auto compaction, overflow recovery (`prepareTurn` with `overflowRecovery`, then `beforeModel` on the retry), and manual `/compact` in the TUI (which persists a sidecar used by the next turn).

### 3.4 Behavior 4: re-inject after compaction

In the same `beforeModel` call:
1. After the snapshot, POST `/v1/restore` `{session_id: conversationId}`.
2. Append `{type:"text", text: context}` as an extra part of **the compaction-summary message itself**. This avoids adding a message and any role-alternation issues.
3. Re-apply it on every later request while that summary is live, keyed by the summary key.

The model gets the restored text in the **first** request after compaction.

**Time budget.** `hint || (snapshot → restore)` runs in parallel (1200 ms ∥ 800 + 1000 ms) under a 2300 ms whole-hook deadline, below the 3000 ms sandbox limit.

---

## 4. Error semantics and return values the plugin must never produce

**Throw or reject in any runtime hook: the run fails.**
- `mergeRuntimeHooks` has no `try/catch` in any branch.
- `AgentRuntime` calls hooks bare: `beforeModel` l.1230, `afterTool` l.2033, `beforeTool` l.1872, `beforeRun` l.971, `afterModel` ~l.1537, `onEvent` in `emit` ~l.2200.
- The provider-retry and overflow wrappers only retry *provider* stream errors; they do not catch hook exceptions.
- The exception reaches the run-level `catch` (l.903). There, `status = "failed"` and a `run-failed` event is emitted. For `ControlledStopError` or an abort, the status is `"aborted"` instead.
- `afterRun` hooks are also called inside that catch. A throw there escapes the runtime.
- `hookErrorMode` only affects `setup()` errors (`session-runtime-orchestrator.ts` l.997–1020).

**Hang: the sandbox kills the process at 3 s.** Every hook call goes through `sandbox.call("invokeHook", …, { timeoutMs: 3000 })`. `subprocess-sandbox.ts` l.343–357 handles a timeout like this:

```ts
this.shutdownProcess(entry.child).catch(() => {});
entry.reject(new Error(`${this.processLabel} call timed out after ${options.timeoutMs}ms: ${method}`));
```

- So a slow hook **fails the run** ("plugin-sandbox call timed out after 3000ms: invokeHook").
- It also **kills the shared plugin sandbox** for every plugin in that session. The next call respawns it; `reinitialize` then clears all module state.
- There is no configurable hook timeout. Hook payloads that aren't serializable are retried once with a JSON-safe clone (`callWithSerializableFallback`).

**Other loading failures:**
- Import failures and `setup()` throws are recorded as plugin load failures and logged; the session continues without that plugin.
- Imports time out after `CLINE_PLUGIN_IMPORT_TIMEOUT_MS` (default 4000).
- Message builders (60 s) propagate errors the same way as hooks. The sketch uses none.

**Fire-and-forget emits.** `status-notice` events from compaction (`void this.emit`) run `onEvent` hooks without awaiting them. A rejection there is an **unhandled rejection** in the host. This is another reason not to register `onEvent`.

**Return values that stop, skip, block or alter behaviour. The plugin must never return these:**

| Hook | Field | Effect (source) |
|---|---|---|
| any of `beforeRun`, `beforeModel`, `afterModel`, `beforeTool`, `afterTool` | `stop: true` (+`reason`) | `applyStopControl` throws `ControlledStopError`. The run ends as `aborted`, and `reason` becomes `lastError`. `mergeRuntimeHooks` also short-circuits the remaining hooks |
| `beforeTool` | `skip: true` | The tool is not executed ("blocked by a runtime hook") |
| `beforeTool` | `input` | Rewrites the tool input |
| `beforeTool` | `policy` | Overrides tool policy (`enabled:false` disables it; `autoApprove:false` forces approval, which is denied without a TTY) |
| `beforeTool` / `afterTool` | `appendContext` | Injects a persisted `<hook_context>` user message |
| `beforeModel` | `messages: []` or a partial list | Replaces the whole request conversation |
| `beforeModel` | `tools` | Replaces the tool list (`[]` removes all tools) |
| `beforeModel` | `options` | Merged into the provider options |
| `afterTool` | `result` with `isError: true` | Turns success into an error |
| file hooks (not used) | `{cancel: true}` | Cancels. `review` / `overrideInput` are only honoured by the file-hook layer |

The plugin returns only:
- `undefined`
- `{messages: <full list with extra text parts>}` from `beforeModel`
- `{result: {...original, output: <same array with some entries' result replaced>}}` from `afterTool`

---

## 5. MCP

**Settings file.** `resolveMcpSettingsPath()` (`paths.ts` l.440) resolves to `$CLINE_MCP_SETTINGS_PATH`, else `<CLINE_DATA_DIR or $CLINE_DIR/data or ~/.cline/data>/settings/cline_mcp_settings.json`. With `--data-dir X`, that is `X/settings/cline_mcp_settings.json`.

**Schema** (`core/src/extensions/mcp/config-loader.ts` l.60–222). The root is `{ "mcpServers": { <name>: <registration> } }`, with passthrough for other keys. Each registration can use the legacy flat form or the nested form:

```json
{
  "mcpServers": {
    "subcortex": { "command": "subcortex", "args": ["mcp"], "disabled": false }
  }
}
```

```json
{
  "mcpServers": {
    "subcortex": { "transport": { "type": "stdio", "command": "subcortex", "args": ["mcp"] }, "disabled": false }
  }
}
```

- **Optional fields:** `env` (string map), `cwd`, `timeout` (seconds), `metadata`.
- **Legacy form:** `type`/`transportType` may be `"stdio"`; `http` maps to `streamableHttp`.
- Unknown keys such as `autoApprove` are stripped by zod, not rejected. Merge into an existing file; don't overwrite it.
- **CLI wizard:** `cline mcp install subcortex -- subcortex mcp` (alias `cline mcp add`). It needs a TTY, so it is unusable from an installer.
- **Plugin alternative (UNVERIFIED end-to-end):** add `"mcp"` to `manifest.capabilities` and call `api.registerMcpServer({ name: "subcortex", transport: { type: "stdio", command: "subcortex", args: ["mcp"] } })` in `setup`. Plugin MCP support exists since CLI 3.0.25 ("Added MCP server support to plugins"). It keeps everything in one file, but a plugin load failure would then also drop MCP. (The 3.0.62 note about MCP servers starting "without touching `cline_mcp_settings.json`" refers to *Agent Plugins* under `~/.agents/plugins`, a separate mechanism.)

---

## 6. Plugin source (ready to use)

This is also saved as `specs2/sketch/cline-subcortex.ts`; it transpiles cleanly and passes the smoke test (`bun test specs2/sketch/smoke.test.ts`), including a 5 s daemon hang that resolves in under 2.6 s. Installer target: `${CLINE_DIR:-~/.cline}/plugins/subcortex.ts`, a single file, with `__SUBCORTEX_URL__` templated. If Cline runs through the Hub, the plugin reads the Hub's env, so the templated URL matters more than `SUBCORTEX_URL`.

```ts
// subcortex plugin for Cline CLI (cline >= 3.0.62, SDK @cline/core >= 0.0.83).
//
// Installed by `subcortex install cline` as ONE file at <CLINE_DIR>/plugins/subcortex.ts
// (default ~/.cline/plugins/subcortex.ts). Cline discovers every .ts/.js file under
// that directory. Do not put helper files next to it, or they load as plugins too.
// Cline runs plugins in a sandbox subprocess (Node or Bun, loaded through jiti)
// and proxies hooks over JSON IPC. All decisions come from the local subcortex
// daemon's policy endpoints; this file holds no thresholds of its own:
//
//   beforeModel -> B1: POST /v1/prompt-hint once per new user prompt (root agent
//                  only); the hint is appended as an extra text part to that
//                  user message in every later provider request (request-only,
//                  never persisted).
//                  B3+B4: Cline compacts inside prepareTurn, just before this
//                  hook runs, and only changes the request (the transcript in
//                  snapshot.messages stays full). When a new compaction summary
//                  (metadata.kind === "compaction_summary") shows up: POST
//                  /v1/snapshot with the transcript tail, then POST /v1/restore,
//                  and append the restored text to that summary message on every
//                  request while that summary is live.
//   afterTool   -> B2: POST /v1/tool-output for each successful run_commands entry
//                  (success === true); only `result` of that entry changes.
//
// FAIL-OPEN IS ON US: Cline does NOT catch plugin hook errors. A throw, a
// rejection, or a sandbox call over 3000 ms (the host kills the sandbox process)
// fails the whole run. So every hook is raced against a 2300 ms deadline and
// resolves to undefined on anything unexpected. We never return stop, skip,
// policy, input, tools, options or appendContext. `onEvent` is deliberately not
// registered: it would be awaited over IPC for every streamed token.

import type { AgentPlugin } from "@cline/core"

const TEMPLATED_URL = "__SUBCORTEX_URL__" // replaced by the installer
const BASE = (
  process.env.SUBCORTEX_URL || (TEMPLATED_URL.startsWith("http") ? TEMPLATED_URL : "http://127.0.0.1:7707")
).replace(/\/+$/, "")

const HOOK_DEADLINE_MS = 2300 // Cline's sandbox hook timeout is a hard 3000 ms (not configurable)
const PROMPT_TIMEOUT_MS = 1200
const SNAPSHOT_TIMEOUT_MS = 800
const RESTORE_TIMEOUT_MS = 1000
const OUTPUT_TIMEOUT_MS = 2000
const LOCAL_MIN_OUTPUT_CHARS = 2000 // cheap pre-filter; the daemon applies the real threshold
const SNAPSHOT_MESSAGES = 20
const MAX_ENTRIES = 256

async function post(path: string, body: unknown, ms: number): Promise<any> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), ms)
  try {
    const res = await fetch(BASE + path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: controller.signal,
    })
    if (!res.ok) return null
    const data = await res.json()
    return data && typeof data === "object" && data.success !== false ? data : null
  } catch {
    return null
  } finally {
    clearTimeout(timer)
  }
}

// Resolve to `fallback` on error or after `ms`, whichever comes first. Never rejects.
function withDeadline<T>(work: () => Promise<T>, ms: number, fallback: T): Promise<T> {
  return new Promise<T>((resolve) => {
    const timer = setTimeout(() => resolve(fallback), ms)
    let promise: Promise<T>
    try {
      promise = work()
    } catch {
      promise = Promise.resolve(fallback)
    }
    promise.then(
      (value) => {
        clearTimeout(timer)
        resolve(value)
      },
      () => {
        clearTimeout(timer)
        resolve(fallback)
      },
    )
  })
}

function remember<V>(map: Map<string, V>, key: string, value: V) {
  map.delete(key)
  map.set(key, value)
  while (map.size > MAX_ENTRIES) map.delete(map.keys().next().value as string)
}

const meta = (m: any): Record<string, any> => (m && m.metadata && typeof m.metadata === "object" ? m.metadata : {})
const isSummary = (m: any) => meta(m).kind === "compaction_summary"

// A prompt the user typed. Excludes <hook_context> blocks (displayRole "system"),
// runtime reminders (userRunSpan 0) and compaction summaries.
function isUserPrompt(m: any): boolean {
  if (!m || m.role !== "user" || !Array.isArray(m.content)) return false
  const md = meta(m)
  if (md.kind === "compaction_summary" || md.displayRole === "system" || md.userRunSpan === 0) return false
  return m.content.some((p: any) => p && p.type === "text" && typeof p.text === "string")
}

// Cline wraps typed input as <user_input mode="act">…</user_input> and may add <mode_notice> blocks.
function promptText(m: any): string {
  return m.content
    .filter((p: any) => p && p.type === "text" && typeof p.text === "string")
    .map((p: any) => p.text)
    .join("\n")
    .replace(/<mode_notice>[\s\S]*?<\/mode_notice>/g, "")
    .replace(/<\/?user_(?:input|command)\b[^>]*>/g, "")
    .trim()
}

function summaryKey(m: any): string {
  const md = meta(m)
  const text = Array.isArray(m?.content) ? m.content.map((p: any) => (p && typeof p.text === "string" ? p.text : "")).join("") : ""
  return `${md.generatedAt ?? ""}:${md.tokensBefore ?? ""}:${text.length}:${text.slice(0, 64)}`
}

const hints = new Map<string, string | null>() // prompt text -> hint (null = none / pending)
const restores = new Map<string, string | null>() // summary key -> restored context
const lastSummary = new Map<string, string>() // conversation -> summary key seen on the previous request

async function beforeModel(ctx: any): Promise<any> {
  const messages = ctx?.request?.messages
  if (!Array.isArray(messages) || messages.length === 0) return undefined
  const snapshot = ctx?.snapshot ?? {}
  const conversation = String(snapshot.conversationId ?? snapshot.agentId ?? "")
  const rootAgent = snapshot.parentAgentId == null
  const work: Promise<unknown>[] = []

  // B1: one daemon call per new user prompt.
  if (rootAgent) {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (!isUserPrompt(messages[i])) continue
      const text = promptText(messages[i])
      if (text && !hints.has(text)) {
        remember(hints, text, null)
        work.push(
          post("/v1/prompt-hint", { prompt: text }, PROMPT_TIMEOUT_MS).then((res) => {
            if (typeof res?.hint === "string" && res.hint.trim()) remember(hints, text, res.hint)
          }),
        )
      }
      break
    }
  }

  // B3 + B4: detect a compaction that happened in this request's prepareTurn.
  let summaryIndex = -1
  for (let i = messages.length - 1; i >= 0; i--) {
    if (isSummary(messages[i])) {
      summaryIndex = i
      break
    }
  }
  const key = summaryIndex >= 0 ? summaryKey(messages[summaryIndex]) : ""
  const previous = lastSummary.get(conversation)
  if (conversation) remember(lastSummary, conversation, key)
  // `previous === undefined` means the first request we see for this conversation
  // (a new session, a resume, or a respawned sandbox). Record the baseline without
  // restoring, so an old summary does not look like a fresh compaction.
  if (conversation && key && previous !== undefined && previous !== key && !restores.has(key)) {
    remember(restores, key, null)
    const recent = Array.isArray(snapshot.messages) ? snapshot.messages.slice(-SNAPSHOT_MESSAGES) : []
    work.push(
      (async () => {
        await post("/v1/snapshot", { session_id: conversation, messages: recent }, SNAPSHOT_TIMEOUT_MS)
        const res = await post("/v1/restore", { session_id: conversation }, RESTORE_TIMEOUT_MS)
        if (typeof res?.context === "string" && res.context.trim()) remember(restores, key, res.context)
      })().catch(() => {}),
    )
  }

  await Promise.all(work)

  let changed = false
  const out = messages.map((m: any, i: number) => {
    let extra: string | null | undefined
    if (i === summaryIndex) extra = restores.get(key)
    else if (rootAgent && isUserPrompt(m)) extra = hints.get(promptText(m))
    if (!extra) return m
    changed = true
    return { ...m, content: [...m.content, { type: "text", text: extra }] }
  })
  return changed ? { messages: out } : undefined // full list; replaces the request's messages
}

async function afterTool(ctx: any): Promise<any> {
  const name = ctx?.toolCall?.toolName ?? ctx?.tool?.name
  const result = ctx?.result
  if (name !== "run_commands" || !result || result.isError === true || !Array.isArray(result.output)) return undefined
  let changed = false
  const output = await Promise.all(
    result.output.map(async (entry: any) => {
      // ToolOperationResult {query, result, error?, success}; success=false means non-zero exit/timeout.
      if (!entry || entry.success !== true || typeof entry.result !== "string") return entry
      if (entry.result.length < LOCAL_MIN_OUTPUT_CHARS) return entry
      const res = await post(
        "/v1/tool-output",
        { output: entry.result, tool: "run_commands", input: { command: String(entry.query ?? "") } },
        OUTPUT_TIMEOUT_MS,
      )
      if (typeof res?.replacement !== "string") return entry
      changed = true
      return { ...entry, result: res.replacement }
    }),
  )
  return changed ? { result: { ...result, output } } : undefined
}

const plugin: AgentPlugin = {
  name: "subcortex",
  manifest: { capabilities: ["hooks"] },
  hooks: {
    beforeModel: (ctx: any) => withDeadline(() => beforeModel(ctx), HOOK_DEADLINE_MS, undefined),
    afterTool: (ctx: any) => withDeadline(() => afterTool(ctx), HOOK_DEADLINE_MS, undefined),
  },
}

export default plugin
```

**Notes for the installer:**
- Write exactly one file.
- **Uninstall:** delete the file, or use `cline plugin uninstall` if it was installed through the CLI.
- **Duplicate names.** If two files export `name: "subcortex"`, the **later path wins** and `duplicate_plugin_override` warns (`plugin-sandbox-bootstrap.ts` ~l.777). Load order is configured paths, then project, then global, then Documents, so the global file overrides a project-level copy.
- No restart is needed: plugins are resolved per session. With the Hub backend, confirm that a running Hub picks up new files on the next session (UNVERIFIED).

---

## 7. UNVERIFIED claims

1. **The plugin has not run inside a real Cline process.** The following were verified only by reading the source:
   - sandbox IPC
   - `beforeModel` message replacement reaching the provider
   - the compaction-summary metadata surviving into `request.messages`

   The smoke test uses fake contexts.
2. **Sandbox JS runtime in the shipped binary.** From source it is `node` on PATH unless `CLINE_JS_RUNTIME_PATH`, `BUN_EXEC_PATH`, `npm_node_execpath` or `NODE` is set. This was not observed on a real install. The code avoids runtime-specific APIs either way.
3. **Hub-mode behaviour.** Not observed:
   - the plugin sandbox running inside the Hub
   - the Hub's env being what the plugin sees
   - new plugin files being picked up without restarting the Hub
4. **E2E wiring.** Not executed:
   - `cline auth … --baseurl` persisting a working `openai-compatible` config
   - `-P openai-compatible -m mock-model` accepting a model id that isn't in the catalog
   - whether a `GET /models` refresh happens
   - the context window and output size needed to trigger `--compaction basic`
   - the exact `--json` event names
5. **`--yolo` requiring `submit_and_exit`.** Derived from `runtime-builder.ts` ~l.806 and the README ("enables submit_and_exit"). Not observed.
6. **Summary key stability.** The key uses `generatedAt`/`tokensBefore` from the persisted sidecar. The assumption is that re-projection keeps `metadata` unchanged across turns. Message `id`s are *not* stable ("transport identity that the message codec regenerates"), which is why they are not used.
7. **The plugin-contributed MCP server path** (`registerMcpServer`) was not exercised.
8. **The docs' "Hook Policies" (`failureMode`, `timeoutMs`, …)** exist in `docs/sdk/plugins.mdx` but not in code at 0.0.83. They might be implemented later; re-check on upgrade.
