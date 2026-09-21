# Pi (`pi`, earendil-works/pi): subcortex adapter spec

Verified 2026-09-21 against the source at git tag `v0.87.0` (commit `16787ad5`) and the published npm tarball `@earendil-works/pi-coding-agent@0.87.0`. The shipped `dist/core/extensions/types.d.ts` matches the source types quoted below.

Nothing was installed. The plugin in §6 was transpiled with Bun and smoke-tested against a fake daemon (`sketch/smoke.test.ts`: 3/3 pass). It has **not** been run inside a real Pi process.

**Verdict:** use a plugin-only adapter. Write one TypeScript extension file; Pi auto-discovers it and runs it through jiti, so there is no build step. Pi has no command hooks and no MCP.

| Behavior | Seam (Pi 0.87.0) | Status |
|---|---|---|
| 1. Hidden hint on prompt submit | `before_agent_start` returns `{message:{customType, content, display:false}}` | Native. The hint is persisted as a hidden custom message and sent to the LLM as a user-role message right after the prompt |
| 2. Replace successful shell output | `tool_result` returns `{content}` (a partial patch) | Native. Filter on `toolName` `bash`/`powershell` and `isError === false` |
| 3. Pre-compaction snapshot | `session_before_compact` exposes `preparation.messagesToSummarize` | Native |
| 4. Re-inject after compaction | `session_compact` (fetch `/v1/restore`), then `context` (insert before every LLM call) | Native, in two hooks. `before_agent_start` alone is not enough (see §3.4) |
| MCP | none | Pi has no built-in MCP ("**No MCP.**", README) |

---

## 1. Sources, latest version, version detection

| Item | Value |
|---|---|
| npm package | `@earendil-works/pi-coding-agent`, latest **0.87.0**. `dist-tags: {latest: 0.87.0, legacy-node20: 0.74.2}`. Registry `time.modified` 2026-09-21T16:51Z. `engines.node >= 22.19.0`. `bin.pi = dist/bundle/cli.js` |
| Former name | `@mariozechner/pi-coding-agent`, last version 0.73.1 (2026-05-07), repo `badlogic/pi-mono`. **Do not target it** |
| Repo | https://github.com/earendil-works/pi, tag `v0.87.0` → `16787ad5b2dc748047f314ca1bfe7708f30f54f3` |
| Extension API types | `packages/coding-agent/src/core/extensions/types.ts` (dist: `dist/core/extensions/types.d.ts`) |
| Dispatch and error handling | `packages/coding-agent/src/core/extensions/runner.ts` |
| Loader and discovery | `packages/coding-agent/src/core/extensions/loader.ts` |
| Hook call sites | `packages/coding-agent/src/core/agent-session.ts`: `afterToolCall` ~l.551, `emitBeforeAgentStart` ~l.1703, compaction ~l.2441/2511 (manual) and ~l.2782/2850 (auto) |
| LLM conversion | `packages/coding-agent/src/core/messages.ts` (`convertToLlm`) |
| Bash tool | `packages/coding-agent/src/core/tools/bash.ts`, `truncate.ts` |
| Docs | `packages/coding-agent/docs/extensions.md`, `compaction.md`, `models.md`, `usage.md`, `rpc.md`, `environment-variables.md`, `settings.md` |
| Other deps (same version) | `@earendil-works/pi-agent-core` (agent loop, `afterToolCall`, `transformContext`), `@earendil-works/pi-ai` (providers) |

**Version detection.**
- `pi --version` (or `-v`) prints the bare version, e.g. `0.87.0` (`main.ts`: `if (parsed.version) { console.log(VERSION); process.exit(0); }`).
- Alternatively read `package.json` from `$(npm root -g)/@earendil-works/pi-coding-agent/`.
- Recommended minimum is **0.87.0**. It is the only version verified here, and it changed the `context` event:

  > "Fixed `context` handlers that filter or slice messages dropping the prompt and tool declarations … Handlers no longer see system messages; Pi restores the prompt and tool state after they run." (CHANGELOG 0.87.0)
- The plugin only does `import type`, which is erased, so the package rename does not break loading. Behaviour on versions below 0.87 is UNVERIFIED.

**Relevant recent breaking changes:**
- 0.87.0: `TurnEndEvent` and `agent_before_settle` became actionable boundaries. `SessionManager` is canonical for provider context.
- 0.86.0: "`user_bash` now fails closed". Do not register `user_bash`.

---

## 2. Install, discovery, module format, runtime, isolation, non-interactive runs

### 2.1 Locations and discovery

From `docs/extensions.md` and `loader.ts` `discoverAndLoadExtensions`:

| Location | Scope |
|---|---|
| `~/.pi/agent/extensions/*.ts` (or `*.js`) | Global. **Put the installer target here:** `$PI_CODING_AGENT_DIR/extensions/subcortex.ts` |
| `~/.pi/agent/extensions/*/index.ts` (or a `package.json` with `"pi": {"extensions": [...]}`) | Global, as a directory |
| `<cwd>/.pi/extensions/*.ts`, `<cwd>/.pi/extensions/*/index.ts` | Project. Loaded **only after the project is trusted** |
| `settings.json` `"extensions": ["/abs/path.ts", ...]`, `"packages": ["npm:…", "git:…"]` | Explicit paths |
| CLI `-e/--extension <path\|npm:\|git:>` (repeatable) | This run only |

- **Auto-load.** No registration command and no config entry are needed. Discovery goes one level deep only. Paths are de-duplicated. Order: project dir, then global dir, then configured paths.
- `--no-extensions` (`-ne`) disables discovery. Explicit `-e` still loads.
- `/reload` hot-reloads auto-discovered extensions.
- `pi install` manages *packages*, which you don't need for a single file.
- `$PI_CODING_AGENT_DIR` replaces `~/.pi/agent` entirely (`config.ts` `getAgentDir()`: `process.env[ENV_AGENT_DIR]` → `expandTildePath`, else `join(homedir(), ".pi", "agent")`).

### 2.2 Module format

The default export must be a function. `loader.ts` does `jiti.import(extensionPath, { default: true })`; if the result `typeof factory !== "function"`, the load fails with "Extension does not export a valid factory function".

```ts
// types.ts l.1716
export type ExtensionFactory = (pi: ExtensionAPI) => void | Promise<void>;
// types.ts l.1344
export type ExtensionHandler<E, R = undefined> = (event: E, ctx: ExtensionContext) => Promise<R | void> | R | void;
// ExtensionAPI.on(...) overloads return an unsubscribe function: () => void
```

- **The factory runs once per session runtime.** After `/new`, `/resume`, `/fork` or `/reload`, Pi fires `session_shutdown` on the old instance, then reloads and rebinds extensions for the new session, which calls the factory again.
  - jiti itself runs with `moduleCache: false`. However, `loadExtensionModule` can reuse a cached factory (`extensionCache`), so module-level state *may* persist while closure state inside the factory is always new.
  - Keep per-session state inside the factory.
- Async factories are awaited before `session_start`.
- Docs rule: do not start timers, sockets or processes from the factory itself.
- **Types package:** `import type { ExtensionAPI } from "@earendil-works/pi-coding-agent"`. This is type-only and erased by jiti. At runtime, Pi provides `@earendil-works/pi-coding-agent`, `typebox`, `@earendil-works/pi-ai` and `@earendil-works/pi-tui` as virtual modules or aliases.

### 2.3 Runtime

- The npm install runs under **Node ≥ 22.19** (`dist/bundle/cli.js` → `cli-runtime.js`). The loader also supports Bun-compiled and bundled-Node builds through `virtualModules`.
- TypeScript is transpiled by jiti; there is no type checking. Global `fetch`, `AbortController` and `AbortSignal` are available.
- **A load failure is fatal.** `main.ts` ~l.897:

  ```ts
  const hasRuntimeErrors = runtime.diagnostics.some((diagnostic) => diagnostic.type === "error");
  ...
  if (hasRuntimeErrors) { … console.error(chalk.yellow(EXTENSION_LOAD_FAILURE_HINT)); process.exit(1); }
  // EXTENSION_LOAD_FAILURE_HINT = `Hint: Start without extensions using "pi -ne".`
  ```

  Any of these makes **every** `pi` invocation exit 1: an extension that fails to import, a syntax error, an unresolvable runtime import, or a factory that throws. The plugin must therefore have no runtime imports and a factory that cannot throw.

### 2.4 Environment and flags for an isolated e2e run

From `docs/environment-variables.md`, `usage.md` and `settings.md`:

| Env / flag | Effect |
|---|---|
| `PI_CODING_AGENT_DIR=<dir>` | Config dir: `extensions/`, `models.json`, `settings.json`, `auth.json`, `trust.json`, `sessions/`, `prompts/`, `themes/` |
| `PI_CODING_AGENT_SESSION_DIR=<dir>` / `--session-dir <dir>` / `--no-session` | Session storage. Precedence: flag, then env, then `sessionDir` in settings |
| `PI_OFFLINE=1` | "Disable startup network operations, including update checks, package updates, and install/update telemetry" |
| `PI_SKIP_VERSION_CHECK=1`, `PI_TELEMETRY=0` | No pi.dev version request, no telemetry or attribution headers |
| `--no-context-files` (`-nc`), `--no-skills`, `--no-prompt-templates`, `--no-themes` | Ignore AGENTS.md, CLAUDE.md, skills and so on |
| `-na/--no-approve` | Ignore project-local `.pi` files for this run |

Pi sets `PI_SESSION_ID`, `PI_SESSION_FILE`, `PI_PROVIDER` and `PI_MODEL` for `bash` tool children. It also exports `AI_AGENT=pi` and `PI_CODING_AGENT=true` to child processes.

### 2.5 Non-interactive runs

- `pi -p "prompt"` (print mode) sends the prompt, prints the final assistant text and exits. The exit code is 1 when the last assistant message has `stopReason` `error` or `aborted` (`modes/print-mode.ts`).
- `pi --mode json "prompt"` streams every event as JSONL. The first line is the session header.
- **Several prompts in one process:** `pi -p "first" "second" …`. `cli/initial-message.ts` takes `messages[0]` as the initial prompt, and print mode then calls `session.prompt()` for each remaining message in order. A message that starts with `/` is first matched against *extension* commands. Built-ins such as `/compact` are interactive-only.
- Print mode also reads piped stdin into the first prompt. Run e2e with `stdin=DEVNULL`.
- `pi --mode rpc` accepts JSONL commands on stdin, e.g. `{"type":"prompt","message":"…"}` and `{"type":"compact"}` (`docs/rpc.md` §compact). This is the deterministic way to trigger compaction.
- In print and json modes, extension errors are printed to stderr as `Extension error (<path>): <msg>` (`print-mode.ts` `onError`) and the run continues.

### 2.6 Pointing Pi at a mock LLM

There is **no** base-URL env var: a grep of pi-ai and coding-agent for `BASE_URL` found none. Use `models.json` in `$PI_CODING_AGENT_DIR`, as documented in `docs/models.md`:

```json
{
  "providers": {
    "mock": {
      "baseUrl": "http://127.0.0.1:PORT/v1",
      "api": "openai-completions",
      "apiKey": "sk-e2e",
      "compat": { "supportsDeveloperRole": false, "supportsReasoningEffort": false },
      "models": [ { "id": "mock-model", "reasoning": false, "input": ["text"], "contextWindow": 32000, "maxTokens": 4096 } ]
    }
  }
}
```

- `openai-completions` POSTs `${baseUrl}/chat/completions` with `stream: true` and `stream_options: {include_usage: true}` (`pi-ai/src/api/openai-completions.ts`). The mock must answer with SSE.
- Select the model with `--model mock/mock-model`, or `--provider mock --model mock-model`.
- `apiKey` literals are used as-is; `"$VAR"` interpolates an env var.
- **Anthropic alternative.** Override the built-in provider with `{"providers":{"anthropic":{"baseUrl":"http://127.0.0.1:PORT"}}}` and set `ANTHROPIC_API_KEY=x`. The built-in base URL is `https://api.anthropic.com` with no `/v1`, and the SDK appends `/v1/messages`. Then select `--model anthropic/<a built-in id>`.

**Isolated e2e recipe.** Pieces marked UNVERIFIED have not been run.

```sh
T=$(mktemp -d); mkdir -p "$T/agent/extensions" "$T/work"
cp pi-subcortex.ts "$T/agent/extensions/subcortex.ts"      # what `subcortex install pi` would write
printf '%s' "$MODELS_JSON" > "$T/agent/models.json"        # the JSON above with PORT filled in
cd "$T/work" && git init -q
PI_CODING_AGENT_DIR="$T/agent" PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0 \
SUBCORTEX_URL="http://127.0.0.1:$DAEMON_PORT" \
  pi -p --no-session --no-context-files --no-skills --model mock/mock-model "run the build" </dev/null
```

- **Mock script for the Pi tool:** `{"tool": "bash", "input": {"command": "yes 'compiling module ok' | head -3000"}}`, then `{"text": "Done."}`.
- Bash output is already cut to the **last 2000 lines or 50 KB** (`truncate.ts`: `DEFAULT_MAX_LINES = 2000`, `DEFAULT_MAX_BYTES = 50 * 1024`). A notice `[Showing lines …. Full output: <tmpfile>]` is then appended. The plugin trims the body and keeps that notice.
- **Assertions:**
  - The first request with tools contains the hint. It is a separate user message after the prompt.
  - The second request contains `[subcortex: truncated`.
- **Compaction e2e (UNVERIFIED wiring).** Use `pi --mode rpc` and send `{"type":"prompt",…}`, wait for `agent_end`, send `{"type":"compact"}`, then send another prompt. Assert that `/v1/snapshot` was hit and that the next agent request contains the restored text.
  - Alternative: a test-only helper extension, `pi.registerCommand("sc-compact", {handler: (_a, ctx) => new Promise(r => ctx.compact({onComplete: r, onError: r}))})`, driven by `pi -p "prompt 1" "/sc-compact" "prompt 2"`.
  - The summarization call is a normal chat completion. Whether it carries tools is UNVERIFIED, so the mock should treat any request with no script left as "OK".

---

## 3. Exact hook API per behavior

The generic dispatcher (`runner.ts` l.988 `emit`) wraps every handler in `try/catch`, sends errors to `emitError`, and continues with the next handler. The dedicated emitters (`emitBeforeAgentStart`, `emitToolResult`, `emitContext`, `emitInput`, …) do the same. **Nothing has a timeout. Every handler is simply `await`ed.**

### 3.1 Behavior 1: `before_agent_start` (append hidden context to the user's turn)

```ts
// types.ts l.737
export interface BeforeAgentStartEvent {
	type: "before_agent_start";
	/** The raw user prompt text (after expansion). */
	prompt: string;
	/** Images attached to the user prompt, if any. */
	images?: ImageContent[];
	/** The current system prompt, rendered from systemPromptOptions and earlier handler changes. */
	readonly systemPrompt: string;
	/** Mutable prompt sections. Later handlers observe mutations made by earlier handlers. */
	systemPromptOptions: NormalizedBuildSystemPromptOptions;
}
// types.ts l.1253
export interface BeforeAgentStartEventResult {
	message?: Pick<CustomMessage, "customType" | "content" | "display" | "details">;
	/** Replace the complete system prompt for this turn. Later handlers observe this exact override. */
	systemPrompt?: string;
}
// messages.ts
export interface CustomMessage<T = unknown> {
	role: "custom"; customType: string; content: string | (TextContent | ImageContent)[];
	display: boolean; details?: T; timestamp: number;
}
```

- **When it fires.** After the user submits and after extension commands, the `input` event, and skill/template expansion. It fires before `agent_start`, once per user prompt. That includes print-mode prompts, RPC `prompt` and `pi.sendUserMessage`.
- **Where the message goes.** In `agent-session.ts` ~l.1719–1747 the new run's messages are built in this order:
  1. `{role:"user", content:[text, …images]}`
  2. pending `nextTurn` messages
  3. one `{role:"custom", customType, content, display, details, timestamp}` per handler that returned a `message`
- **How the LLM sees it.** `convertToLlm` (`messages.ts` l.162) turns `case "custom"` into `{ role: "user", content }`. The LLM therefore sees a second user message right after the prompt.
- **Visibility.** `display:false` hides it in the TUI (`interactive-mode.ts` renders only `display` entries). It **is persisted** in the session JSONL as a `custom_message` entry, so it stays in history for later turns and for compaction.
- **Return value:** `{ message: { customType: "subcortex-hint", content: hint, display: false } }`, or `undefined`.
- Never return `systemPrompt`: it forces a full system-prompt replacement and a cache miss.
- **Alternatives, both rejected.**
  - `input` with `{action:"transform", text}` would make the hint part of the visible user text.
  - `context` would be non-persistent and change the history on every request.

### 3.2 Behavior 2: `tool_result` (replace model-visible tool output)

```ts
// types.ts l.1040
interface ToolResultEventBase {
	type: "tool_result";
	toolCallId: string;
	input: Record<string, unknown>;
	content: (TextContent | ImageContent)[];
	isError: boolean;
	/** Usage from the tool execution itself, if available. */
	usage?: Usage;
}
export interface BashToolResultEvent extends ToolResultEventBase { toolName: "bash"; details: BashToolDetails | undefined; }
export interface PowerShellToolResultEvent extends ToolResultEventBase { toolName: "powershell"; details: PowerShellToolDetails | undefined; }
// ... read/edit/write/grep/find/ls, CustomToolResultEvent { toolName: string; details: unknown }
export function isBashToolResult(e: ToolResultEvent): e is BashToolResultEvent { return e.toolName === "bash"; }
// types.ts l.1241
export interface ToolResultEventResult {
	content?: (TextContent | ImageContent)[];
	details?: unknown;
	isError?: boolean;
	usage?: Usage;
}
// tools/bash.ts l.50
export interface BashToolDetails { truncation?: TruncationResult; fullOutputPath?: string; }
```

- **Semantics** (`runner.ts` l.1082 `emitToolResult`):
  - Handlers chain like middleware. Each handler sees the previous handler's patch.
  - Fields that are `undefined` are left unchanged.
  - A throw is logged and skipped.
  - `agent-session.ts` l.551 then applies `{content, details, isError, usage}` onto the tool result that goes to the model. The hook runs before `tool_execution_end` and before the persisted `toolResult` message, so the replacement is what the LLM sees and what is stored.
- **Shell tools:** `toolName === "bash"`; `"powershell"` is the Windows built-in. Input is `{ command: string; timeout?: number }`.
- **Success vs failure.**
  - `bash.ts` ~l.368 **throws** on a non-zero exit (`Command exited with code N`), on a timeout or abort, and on a missing exit code.
  - pi-agent-core turns that throw into `isError: true`, with the output plus the status line as the text content.
  - So **success is exactly `isError === false`**. There is no exit code on the event.
- **Pre-truncated output.** Output is already tail-truncated (2000 lines or 50 KB). When that happens, `details.truncation.truncated === true`, `details.fullOutputPath` points at a temp file, and the text ends with `\n\n[Showing … Full output: <path>]`.
- **Return value:** `{ content: [{ type: "text", text: replacement }] }`.
  - Omit `details` and `isError`; they keep their current values.
  - Only replace when every content part is text (images are possible for custom tools).
- `ctx.signal` is the agent abort signal ("lets Esc cancel … fetch()"). Linking it to the request is recommended.

### 3.3 Behavior 3: `session_before_compact` (observe before compaction)

```ts
// types.ts l.598
export interface SessionBeforeCompactEvent {
	type: "session_before_compact";
	preparation: CompactionPreparation;
	branchEntries: SessionEntry[];
	customInstructions?: string;
	/** What triggered the compaction: manual /compact, the context threshold, or context overflow recovery */
	reason: "manual" | "threshold" | "overflow";
	/** True when the aborted turn is retried after this compaction (overflow recovery) */
	willRetry: boolean;
	signal: AbortSignal;
}
// compaction/compaction.ts l.794
export interface CompactionPreparation {
	firstKeptEntryId: string;
	/** Messages that will be summarized and discarded */
	messagesToSummarize: AgentMessage[];
	/** Messages that will be turned into turn prefix summary (if splitting) */
	turnPrefixMessages: AgentMessage[];
	isSplitTurn: boolean;
	tokensBefore: number;
	previousSummary?: string;
	fileOps: FileOperations;
	settings: CompactionSettings;
}
// types.ts l.1268
export interface SessionBeforeCompactResult { cancel?: boolean; compaction?: CompactionResult; }
```

- **When it fires.**
  - Manual `/compact` or RPC `compact`: `compact()`, ~l.2441.
  - Auto `threshold` and `overflow`: `_runAutoCompaction`, ~l.2782.
  - In both cases it fires after `prepareCompaction()` succeeds and before the summarizer LLM call. It does not fire when there is "Nothing to compact".
- **Awaited.** The compaction waits for the handler, so keep it short. The plugin uses 1.5 s and links `event.signal`.
- **Snapshot payload:** `[...preparation.messagesToSummarize, ...preparation.turnPrefixMessages].slice(-20)`. These are the raw `AgentMessage` objects that are about to leave the model context. `branchEntries` holds the whole branch as session entries.
- **Return value:** nothing. See §4 for why `cancel` and `compaction` must never be returned.

### 3.4 Behavior 4: `session_compact` + `context` (re-inject after compaction)

```ts
// types.ts l.611
export interface SessionCompactEvent {
	type: "session_compact";
	compactionEntry: CompactionEntry;      // { type:"compaction", id, parentId, timestamp: string(ISO), summary, firstKeptEntryId, tokensBefore, details?, fromHook?, systemMessage? }
	fromExtension: boolean;
	reason: "manual" | "threshold" | "overflow";
	willRetry: boolean;
}
// types.ts l.698
export interface ContextEvent { type: "context"; messages: AgentMessage[]; }
// types.ts l.1206
export interface ContextEventResult { messages?: AgentMessage[]; }
// messages.ts
export interface CompactionSummaryMessage { role: "compactionSummary"; summary: string; tokensBefore: number; timestamp: number; }
// session-manager.ts l.461: createCompactionSummaryMessage(entry.summary, entry.tokensBefore, entry.timestamp)
//   -> timestamp = new Date(entry.timestamp).getTime()   (== Date.parse(compactionEntry.timestamp))
```

**Why `before_agent_start` is not enough.** Threshold compaction runs *between turns inside one run* (`prepareNextTurnWithContext` → `_compactBeforeNextAssistantResponse`, ~l.587). Overflow recovery with `willRetry: true` then calls `agent.continue()`. Neither path fires `before_agent_start`, so re-injecting on the next user prompt would be one or more LLM calls too late. (The sweep's "inject on next before_agent_start" plan is superseded.)

**Mechanism.**
1. In `session_compact`, POST `/v1/restore` and keep `{ts: Date.parse(compactionEntry.timestamp), text}` for the session.
2. In `context`, which fires before **every** LLM call via `transformContext` → `runner.emitContext`, find the last message with `role === "compactionSummary"` whose `timestamp === ts`.
3. Insert `{ role: "custom", customType: "subcortex-restore", content: text, display: false, timestamp: ts }` right after it and return `{ messages }`.

Properties of this approach:
- **Timing.** `context` sees the post-compaction projection on the very next request. Compaction runs in `prepareNextTurn`, before `transformContext`.
- **Not persisted.** The `context` event messages are a `structuredClone`. The insertion is re-applied the same way on each request, so the prompt prefix stays stable. It lasts until the next compaction replaces the entry.
- **Cache cost.** When a `context` handler changes the list, Pi folds all system messages into one leading system message (`restoreSystemMessages`, `runner.ts` l.281). This can cost one extra cache miss for models that accept mid-conversation system patches.
- **Guard.** Matching on `timestamp` stops the text from appearing under a different compaction summary, for example after `/tree` navigation.
- **Rejected alternatives.**
  - Calling `pi.sendMessage({…, display:false})` from `session_compact` would persist the text. But while streaming it becomes a `steer`, and auto-compaction then returns `agent.hasQueuedMessages()` and continues the run. Ordering relative to the compacted context is UNVERIFIED.
  - `deliverAs:"nextTurn"` waits for the next *user* prompt.

---

## 4. Error semantics and return values the plugin must never produce

**Handler throws or rejects.** Pi catches, logs and continues, for every event this plugin registers:
- Error handling doc: "Extension errors are logged, agent continues."
- Print and json modes print `Extension error (<path>): <msg>` to stderr.
- Code: `runner.ts` `emit` l.1003, `emitToolResult` l.1109, `emitContext` l.1208/1237, `emitBeforeAgentStart` l.1350.

**Exceptions to that rule. Do not register these events:**
- `tool_call`: `emitToolCall` (l.1134) has **no try/catch**. `agent-session.ts` ~l.529–548 (`beforeToolCall`) rethrows, which blocks the tool: "Extension failed, blocking execution". The doc says "`tool_call` errors block the tool (fail-safe)".
- `user_bash`: fails closed since 0.86.0. A throw or an invalid defined result aborts the `!` command.
- Load time: a factory throw or import failure makes **`pi` exit 1 at startup** (§2.3).

**Handler hangs.** There is **no timeout anywhere**. A hung `before_agent_start` stalls the prompt, a hung `tool_result` stalls the tool batch, a hung `session_before_compact` stalls compaction, and a hung `context` stalls every LLM call. Every network call needs its own abort (≤ 3 s), and `context` must stay synchronous and cheap.

**Return values that block, cancel, stop, skip or loop. The plugin must never return:**

| Event | Dangerous return | Effect |
|---|---|---|
| `tool_call` | `{block: true, reason?, terminate?}` | Tool not executed. `terminate` can end the agent early |
| `input` | `{action: "handled"}` | Skips the agent entirely. `{action:"transform"}` rewrites the user text |
| `session_before_compact` | `{cancel: true}` | "Compaction cancelled" (manual: error; auto: `session_compact_failed`) |
| `session_before_compact` | `{compaction: {...}}` | Replaces Pi's summary with ours |
| `session_before_switch` / `session_before_fork` / `session_before_tree` | `{cancel: true}` | Blocks `/new`, `/resume`, `/fork`, `/clone`, `/tree` |
| `turn_end` / `agent_before_settle` | `{continue: true}` | Forces another provider request. Unconditional use "can create an endless loop" |
| `turn_end` / `agent_before_settle` | `{entries}` | Persists structural entries |
| `before_agent_start` | `{systemPrompt}` | Replaces the whole system prompt (cache miss) |
| `context` / `context_with_system` | a list without the summary or user messages | Truncates the model context. For `context_with_system`, dropping index-0 system loses the prompt and tools |
| `before_provider_request` | any non-`undefined` value | Replaces the provider payload |
| `message_end` | `{message}` | Replaces the finalized message |
| `tool_result` | `{isError: true}` | Flips success into error. The plugin never sets `isError` |
| `cache_warming_decision` | `{action: "stop"}` | Stops cache warming |
| `project_trust` | `{trusted: "yes"\|"no"}` | Overrides trust |
| `user_bash` | `{result}` / `{operations}` | Replaces or skips the user's `!` command |

The plugin returns only:
- `undefined`
- `{message:{customType, content, display:false}}` from `before_agent_start`
- `{content:[{type:"text",…}]}` from `tool_result`
- `{messages}` from `context`, the same list plus one inserted message

---

## 5. MCP

**None.** README: "**No MCP.** Build CLI tools with READMEs (see Skills), or build an extension that adds MCP support." `docs/usage.md`: "It intentionally does not include built-in MCP…". A grep of `packages/coding-agent/src` finds no MCP client.

Third-party pi *packages* that bridge MCP may exist, but none were checked (UNVERIFIED). Recommendation: expose subcortex to Pi only through the extension. If tool access is wanted later, register subcortex tools with `pi.registerTool()` inside the same extension.

---

## 6. Plugin source (ready to use)

This is also saved as `specs2/sketch/pi-subcortex.ts`; it transpiles cleanly and passes the smoke test (`bun test specs2/sketch/smoke.test.ts`). Installer target: `${PI_CODING_AGENT_DIR:-~/.pi/agent}/extensions/subcortex.ts`, with `__SUBCORTEX_URL__` templated.

```ts
// subcortex extension for Pi (@earendil-works/pi-coding-agent >= 0.87.0).
//
// Installed by `subcortex install pi` to $PI_CODING_AGENT_DIR/extensions/subcortex.ts
// (default ~/.pi/agent/extensions/subcortex.ts). Pi auto-discovers it, loads it
// with jiti (no build step) and calls the default-exported factory once per
// session runtime. All decisions come from the local subcortex daemon's policy
// endpoints; this file holds no thresholds or heuristics of its own:
//
//   before_agent_start     -> POST /v1/prompt-hint; returns a hidden custom
//                             message (display:false). Pi appends it right after
//                             the user's prompt and sends it as a user-role message
//   tool_result            -> POST /v1/tool-output for bash/powershell results with
//                             isError=false; returns {content} only (details,
//                             isError and usage keep their values)
//   session_before_compact -> POST /v1/snapshot with the messages about to be
//                             summarized (never returns {cancel} or {compaction})
//   session_compact        -> POST /v1/restore; the text is kept in memory
//   context                -> before every LLM call, inserts the restored text
//                             right after the compaction summary it belongs to
//
// Fail-open everywhere. Pi awaits every handler with no timeout, so each request
// has its own abort timer. Pi catches handler throws for these events, but every
// handler also catches everything. `tool_call`, `input` and `user_bash` are
// deliberately not registered: those events block or fail closed on error.
// The only import is `import type`, which jiti erases. A runtime import that
// fails to resolve makes Pi exit with status 1 at startup.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent"

const TEMPLATED_URL = "__SUBCORTEX_URL__" // replaced by the installer
const BASE = (
  process.env.SUBCORTEX_URL || (TEMPLATED_URL.startsWith("http") ? TEMPLATED_URL : "http://127.0.0.1:7707")
).replace(/\/+$/, "")

const PROMPT_TIMEOUT_MS = 1500
const OUTPUT_TIMEOUT_MS = 3000
const LOCAL_MIN_OUTPUT_CHARS = 2000 // cheap pre-filter; the daemon applies the real threshold
const SNAPSHOT_MESSAGES = 20
const SHELL_TOOLS = new Set(["bash", "powershell"])
// Pi's shell tools end truncated output with "\n\n[Showing ... Full output: <path>]".
const TRUNCATION_NOTICE = /\n\n\[Showing [^\n]*Full output: [^\n]*\]$/

async function post(path: string, body: unknown, ms: number, outer?: AbortSignal): Promise<any> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), ms)
  const onAbort = () => controller.abort()
  try {
    if (outer?.aborted) return null
    outer?.addEventListener("abort", onAbort, { once: true })
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
    outer?.removeEventListener("abort", onAbort)
  }
}

function sessionId(ctx: any): string {
  try {
    return String(ctx?.sessionManager?.getSessionId?.() ?? "")
  } catch {
    return ""
  }
}

export default function subcortex(pi: ExtensionAPI) {
  // sessionId -> restored context for the compaction whose summary has timestamp `ts`
  const restored = new Map<string, { ts: number; text: string }>()

  // Behavior 1: hidden hint appended to the user's turn.
  pi.on("before_agent_start", async (event: any, ctx: any) => {
    try {
      const prompt = typeof event?.prompt === "string" ? event.prompt.trim() : ""
      if (!prompt) return
      const res = await post("/v1/prompt-hint", { prompt }, PROMPT_TIMEOUT_MS, ctx?.signal)
      const hint = res?.hint
      if (typeof hint !== "string" || !hint.trim()) return
      return { message: { customType: "subcortex-hint", content: hint, display: false } }
    } catch {
      return // fail open: the prompt goes through untouched
    }
  })

  // Behavior 2: trim large successful shell output before the model sees it.
  pi.on("tool_result", async (event: any, ctx: any) => {
    try {
      if (!SHELL_TOOLS.has(event?.toolName) || event?.isError !== false) return
      const parts = event.content
      if (!Array.isArray(parts) || parts.length === 0) return
      if (!parts.every((p: any) => p && p.type === "text" && typeof p.text === "string")) return // leave images alone
      const text: string = parts.map((p: any) => p.text).join("")
      const notice = event.details?.truncation?.truncated ? (TRUNCATION_NOTICE.exec(text)?.[0] ?? "") : ""
      const output = notice ? text.slice(0, -notice.length) : text
      if (output.length < LOCAL_MIN_OUTPUT_CHARS) return
      const res = await post(
        "/v1/tool-output",
        { output, tool: String(event.toolName), input: event.input ?? {} },
        OUTPUT_TIMEOUT_MS,
        ctx?.signal,
      )
      const replacement = res?.replacement
      if (typeof replacement !== "string") return
      // Keep Pi's "Full output: <path>" pointer so the model can still read the spill file.
      return { content: [{ type: "text", text: replacement + notice }] }
    } catch {
      return // fail open: the full tool output passes through untouched
    }
  })

  // Behavior 3: snapshot what compaction is about to summarize away.
  pi.on("session_before_compact", async (event: any, ctx: any) => {
    try {
      const sid = sessionId(ctx)
      const prep = event?.preparation ?? {}
      const toSummarize = Array.isArray(prep.messagesToSummarize) ? prep.messagesToSummarize : []
      const prefix = Array.isArray(prep.turnPrefixMessages) ? prep.turnPrefixMessages : []
      const messages = [...toSummarize, ...prefix].slice(-SNAPSHOT_MESSAGES)
      if (sid && messages.length) {
        await post("/v1/snapshot", { session_id: sid, messages }, PROMPT_TIMEOUT_MS, event?.signal)
      }
    } catch {}
    return // never {cancel: true} and never {compaction}: Pi keeps its own summary
  })

  // Behavior 4 (part 1): fetch the context to re-inject once compaction succeeded.
  pi.on("session_compact", async (event: any, ctx: any) => {
    try {
      const sid = sessionId(ctx)
      if (!sid) return
      restored.delete(sid)
      const ts = Date.parse(String(event?.compactionEntry?.timestamp ?? ""))
      const res = await post("/v1/restore", { session_id: sid }, PROMPT_TIMEOUT_MS)
      const text = res?.context
      if (Number.isFinite(ts) && typeof text === "string" && text.trim()) restored.set(sid, { ts, text })
    } catch {}
  })

  // Behavior 4 (part 2): on every LLM call, place the restored text right after the
  // summary of that compaction. Not persisted; the same insertion on each request
  // keeps the prompt cache stable. Also covers threshold compaction mid-run and
  // overflow retries, where before_agent_start does not fire.
  pi.on("context", (event: any, ctx: any) => {
    try {
      const entry = restored.get(sessionId(ctx))
      const messages = event?.messages
      if (!entry || !Array.isArray(messages)) return
      let index = -1
      for (let i = messages.length - 1; i >= 0; i--) {
        if (messages[i]?.role === "compactionSummary") {
          index = i
          break
        }
      }
      if (index < 0 || messages[index].timestamp !== entry.ts) return // another branch or compaction is active
      const out = messages.slice()
      out.splice(index + 1, 0, {
        role: "custom",
        customType: "subcortex-restore",
        content: entry.text,
        display: false,
        timestamp: entry.ts,
      })
      return { messages: out }
    } catch {
      return
    }
  })

  pi.on("session_shutdown", () => {
    try {
      restored.clear()
    } catch {}
  })
}
```

**Notes for the installer and e2e:**
- **Target path:** `$PI_CODING_AGENT_DIR/extensions/subcortex.ts`, falling back to `~/.pi/agent/extensions/subcortex.ts`. Create the dir; don't touch `settings.json`.
- **Uninstall:** delete the file.
- **Where the daemon URL comes from:** `SUBCORTEX_URL` env first, then the templated value, then the default.

---

## 7. UNVERIFIED claims

1. **The plugin has not run inside a real Pi process.** It was transpiled (Bun and Node strip-types) and smoke-tested with fake `pi.on`/`ctx` objects against a fake daemon. The following were verified only by reading the source:
   - hook firing
   - custom-message placement
   - the `context` insertion reaching the provider payload
2. **Provider payload shape of the hint.** It is a separate user message after the prompt. Whether pi-ai's OpenAI or Anthropic serializers merge consecutive user messages or send them separately was not checked. The e2e test should assert on the whole request JSON, not on message positions.
3. **Whether hidden `display:false` custom messages appear in `--mode json`.** They probably do, as `message_start`/`message_end` events, but this was not checked. Assert at the mock LLM instead.
4. **Compaction e2e flows.** Not executed:
   - RPC `{"type":"compact"}` sequencing
   - the helper-command approach
   - whether the summarizer request carries `tools`
5. **Behaviour on Pi < 0.87.0**, and on the old `@mariozechner/pi-coding-agent` builds. The `context` event semantics changed in 0.87.0.
6. **Windows `powershell` tool.** Its output and notice format are assumed to match bash, since both go through `createShellToolDefinition`. Not tested.
7. **`pi.sendMessage` as a persistent B4 alternative.** Its ordering relative to threshold compaction was not verified (§3.4).
8. **Third-party MCP bridges for Pi** (§5) were not checked.
