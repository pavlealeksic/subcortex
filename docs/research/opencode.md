# OpenCode: subcortex plugin adapter spec (verified September 2026)

Status legend: **[doc]** official docs · **[src]** TypeScript source read at tag `v1.18.31` of `anomalyco/opencode` (the repo formerly at `sst/opencode`; GitHub redirects) · **[unverified]**.

OpenCode has no command hooks. The adapter is an **in-process JS/TS plugin** running on Bun inside the OpenCode server process. It calls the subcortex daemon over HTTP.

---

## 1. Sources, versions, detection

| What | Value |
|---|---|
| Latest release | **1.18.31** (GitHub tag `v1.18.31`, 2026-09-14; npm `opencode-ai@1.18.31`, `@opencode-ai/plugin@1.18.31`) |
| Hook availability (bisected over `packages/plugin/src/index.ts` tags) | `chat.message` with `output.parts`, `tool.execute.after` ≥ v1.0.0 · `experimental.session.compacting` and `experimental.chat.messages.transform` **≥ v1.0.165** · `tool.execute.after` `input.args` **≥ v1.1.62** (absent in v1.1.61) · `experimental.compaction.autocontinue`, `experimental.text.complete` present in 1.18.31 |
| **Minimum for the current plugin** | **v1.1.62** (it reads `input.args`); v1.0.165 if `args` is made optional. For npm distribution declare `"engines": {"opencode": ">=1.1.62"}` (checked by the loader: "Plugin requires opencode <range> but running <ver>") [src `plugin/shared.ts::checkPluginCompatibility`]. |
| Version detection | `opencode --version` (semver). Inside a plugin there is no version field in `PluginInput`, so feature-detect instead. |
| Stability | Hooks prefixed `experimental.` are explicitly unstable. `chat.message` and `tool.execute.*` are the documented core. |

Sources:
- https://opencode.ai/docs/plugins/ · https://opencode.ai/docs/config/ · https://opencode.ai/docs/mcp-servers/
- https://github.com/anomalyco/opencode/releases/tag/v1.18.31
- Source (v1.18.31): `packages/plugin/src/index.ts` (the `Hooks` type), `packages/opencode/src/plugin/{index,shared,loader}.ts`, `packages/opencode/src/config/{config,plugin,paths}.ts`, `packages/core/src/{global.ts,flag/flag.ts}`, `packages/opencode/src/session/{tools,prompt,compaction,processor}.ts`, `packages/opencode/src/tool/{shell,truncate}.ts`, `packages/core/src/session/projector.ts`, `packages/schema/src/v1/session.ts`, `packages/schema/src/identifier.ts`, `packages/core/src/id/id.ts`

---

## 2. Plugin locations, env relocation, enablement

- **Local files** [doc+src `config/plugin.ts`]: every `{plugin,plugins}/*.{ts,js}` (the singular form is kept for backwards compatibility) in each config directory, auto-loaded at startup:
  - global: `~/.config/opencode/plugins/`, which is really `$XDG_CONFIG_HOME/opencode/plugins/` (`xdg-basedir`) [src `core/src/global.ts`]
  - project: every `.opencode/plugins/` from cwd up to the worktree root (skipped when `OPENCODE_DISABLE_PROJECT_CONFIG=1`)
  - `~/.opencode/plugins/` (home is overridable with `OPENCODE_TEST_HOME`)
  - `$OPENCODE_CONFIG_DIR/plugins/` (an **additional** directory; it does not replace the global one) [src `config/paths.ts::directories`]
- **npm** [doc]: `opencode.json` → `"plugin": ["subcortex-opencode"]` or `[["subcortex-opencode", {"url":"http://127.0.0.1:7707"}]]` (the tuple's second element is passed as `options`). A bare name resolves to `name@latest`, and Bun installs it at startup into `~/.cache/opencode/node_modules/`. The same name and version load once. A local file and an npm plugin with similar names both load.
- Load order [doc]: global config → project config → global plugin dir → project plugin dir. **All hooks of all plugins run sequentially, in load order** [src `Plugin.trigger`: a `for` loop that `await`s each hook]. OpenCode sets **no timeout** on plugin hooks; a hung `await` stalls the turn.
- Config precedence [doc]: remote `.well-known` → `~/.config/opencode/opencode.json` → `$OPENCODE_CONFIG` → project `opencode.json[c]` → `.opencode/` dirs → `$OPENCODE_CONFIG_CONTENT` (inline JSON) → managed (`/Library/Application Support/opencode/`, MDM).
- There is **no trust or approval step** for plugins. Every file present runs. `OPENCODE_PURE=1` skips external plugins [src `flags.pure`].
- On startup, OpenCode also runs a background `bun install @opencode-ai/plugin` in each config directory [src `config.ts`]. A type-only `import type { Plugin } from "@opencode-ai/plugin"` is erased by Bun anyway.
- **Isolated e2e recipe**:
```bash
T=/tmp/sc-e2e/oc; mkdir -p $T/cfg/opencode/plugins
cp adapters/opencode/subcortex.ts $T/cfg/opencode/plugins/
XDG_CONFIG_HOME=$T/cfg XDG_DATA_HOME=$T/data XDG_STATE_HOME=$T/state XDG_CACHE_HOME=$T/cache \
OPENCODE_TEST_HOME=$T/home OPENCODE_DISABLE_PROJECT_CONFIG=1 OPENCODE_DISABLE_AUTOUPDATE=1 \
ANTHROPIC_API_KEY=... SUBCORTEX_URL=http://127.0.0.1:7707 \
opencode run --print-logs "run: seq 1 20000"
```
Auth normally lives in `$XDG_DATA_HOME/opencode/auth.json`, so provide the provider key through env in isolation. Alternatively set `OPENCODE_CONFIG_DIR=$T/oc` (plugins in `$T/oc/plugins/`) while keeping your real login. That is not hermetic, because the global plugins still load. Force compaction via `"compaction": {"auto": true}` with a small-context model, or `/compact` in the TUI [unverified: fastest headless trigger].

---

## 3. Registration and tagging

- File drop: `~/.config/opencode/plugins/subcortex.ts` (respect `$XDG_CONFIG_HOME`). **The tag is the filename.** Uninstall deletes exactly that file. A `.bak` next to it is not loaded, because the glob only matches `*.ts`/`*.js`.
- npm: add or remove exactly the `"subcortex-opencode"` string, or the `[name, opts]` tuple, in the `plugin` array of `~/.config/opencode/opencode.json`. The file may be **JSONC** (`opencode.jsonc` is also read), so edit with a JSONC-aware parser or refuse on comments.
- Module format [src `plugin/index.ts`, `plugin/shared.ts`]:
  - *Legacy* (what we use): every export must be a plugin function (or an object with `.server`). **Any other export (e.g. `export const URL = …`) throws "Plugin export is not a function" and the plugin fails to load** (reported as a session error; OpenCode continues). Duplicate exports of the same function are de-duplicated.
  - *v1*: `export default { id: "subcortex", server: async (input, options) => hooks }`. **`id` is mandatory for file plugins** ("Path plugin … must export id").
- Plugin signature: `type Plugin = (input: PluginInput, options?: Record<string,unknown>) => Promise<Hooks>`. `PluginInput = { client, project, directory, worktree, experimental_workspace, serverUrl, $ }`.

---

## 4. Hooks: exact names, payloads, and what mutation does

Mechanics [src `packages/opencode/src/plugin/index.ts`]: `trigger(name, input, output)` runs `yield* Effect.promise(async () => fn(input, output))` for each plugin, then returns the **same, mutated `output` object**. Mutation is the only channel; return values are ignored.

### 4.1 `chat.message` (behavior 1)
Signature: `(input: {sessionID, agent?, model?: {providerID, modelID}, messageID?, variant?}, output: {message: UserMessage, parts: Part[]})`. It fires in `SessionPrompt.createUserMessage` **after** parts get IDs (`assign`) and **before** the message and parts are validated and persisted [src `session/prompt.ts` ~L998-1047]. Parts pushed here are persisted and sent to the model.

Sample `output` at call time:
```json
{"message":{"id":"msg_0196a1b2c3d4ZqX9…","sessionID":"ses_0196a1b2…","role":"user","time":{"created":1789999999000},
            "agent":"build","model":{"providerID":"anthropic","modelID":"claude-opus-5"}},
 "parts":[{"id":"prt_0196a1b2c3d5Kf2…","sessionID":"ses_0196a1b2…","messageID":"msg_0196a1b2c3d4ZqX9…",
           "type":"text","text":"rename foo to bar in utils.py"}]}
```
(a) context injection: push a **fully formed** TextPart. The schema requires `id` (must start with `prt`), `sessionID` and `messageID` [src `packages/schema/src/v1/session.ts` `partBase`]. OpenCode's own synthetic parts always set all three [src `compaction.ts`]:
```ts
output.parts.push({
  id: partId(),                          // "prt_" + 12 hex (ms*0x1000+counter) + 14 base62, see §4.6
  sessionID: input.sessionID,
  messageID: output.message.id,
  type: "text",
  synthetic: true,
  text: "[subcortex] A local classifier rated this request as simple (confidence 0.91); the most direct, minimal change is likely sufficient.",
})
```
A part without `id`/`sessionID`/`messageID` is schema-invalid. Validation only logs, but `sessions.updatePart` then publishes `PartUpdated`. Its projector does `insert(PartTable).values({ id, message_id, session_id, … })` with `.pipe(Effect.orDie)` [src `core/src/session/projector.ts`], so an undefined id or session id most likely makes the user's prompt fail [unverified at runtime; strong source evidence]. Alternatives that avoid persistence: `experimental.chat.system.transform` (`output.system.push(…)`; this changes the system prefix and breaks prompt caching, so avoid) or `experimental.chat.messages.transform` (in-memory, per request).

### 4.2 `tool.execute.after` (behavior 2): **replacement works by mutating `output.output`**
Signature: `(input: {tool, sessionID, callID, args}, output: {title: string, output: string, metadata: any})`. For built-in tools, `output` is the exact object `execute()` returns to the AI SDK after the hook [src `session/tools.ts` L102-128]. **Assigning `output.output = "…"` replaces what the model sees.** `output.metadata.output` is the TUI preview and can be left alone. For MCP tools the object is the raw MCP `CallToolResult` (`{content:[…]}`) and has **no `output` string** [src `tools.ts` L418-425]. For the `task` tool it is the task result.

Shell tool (id **`bash`**) result [src `tool/shell.ts` L585-594]:
```json
{"title":"npm ls --all",
 "metadata":{"output":"<preview>","exit":0,"truncated":false},
 "output":"proj@1.0.0 /Users/me/proj\n├── …(18,000 chars)…"}
```
When the output passed OpenCode's own limit (default 2,000 lines or 50 KB, configurable via `tool_output.max_lines/max_bytes`), `metadata.truncated` is `true`, `metadata.outputPath` is set, and `output` starts with `...output truncated...\n\nFull output saved to: <file>` [src `tool/shell.ts`, `tool/truncate.ts`].
(b) The rule: act only when `input.tool === "bash"`, `metadata.exit === 0`, `!metadata.truncated`, and `typeof output.output === "string"` with length ≥ `min_output_chars`. Then:
```ts
output.output = head + `\n\n[subcortex: trimmed ${n} chars of low-value output; re-run the command if you need the rest]\n\n` + tail
```
Never touch `read`, `grep`, `glob`, `webfetch`, `task` or MCP results.

### 4.3 `experimental.session.compacting` (behaviors 3 and 4)
Signature: `(input: {sessionID}, output: {context: string[], prompt?: string})`. It fires inside compaction **before** the summary request. `output.context` strings are appended to the compaction prompt, and setting `output.prompt` **replaces the whole compaction prompt** [src `session/compaction.ts` L372-392; doc]. After this hook, `experimental.chat.messages.transform` is also called on the message copy used for summarization.
(c) Output (never veto; there is no veto field):
```ts
output.context.push(
  "## Verbatim recent messages (preserve key facts from these)\nuser: …\nassistant: …")
```
This is how OpenCode "re-injects": the snapshot is fed to the summarizer, so it survives compaction in summarized form. For a verbatim re-inject after compaction, subscribe to the `session.compacted` event (via the `event` hook: `{event:{type:"session.compacted", properties:{sessionID}}}`) and add the snapshot on the next request. The candidates are `experimental.chat.messages.transform`, which appends an in-memory synthetic text part to the first user message after the compaction boundary, or `chat.message` on the next user turn (auto-continue messages are created **without** `chat.message` [src `compaction.ts` L503-530]).

Transcript source: OpenCode has no transcript file. Keep a per-session ring buffer from `chat.message` (user text from `output.parts` where `type==="text" && !synthetic`) and `experimental.text.complete` (`(input:{sessionID,messageID,partID}, output:{text})`, which fires per completed assistant text part; do NOT mutate `output.text`) [src `session/processor.ts` L530]. Also write it to `~/.local/share/subcortex/compact/<sessionID>.json` for parity with the other adapters. Reading via `client.session.messages({path:{id}})` from inside the hook is possible but re-enters the server [unverified: deadlock and latency risk].

### 4.4 `experimental.chat.messages.transform`
`(input: {}, output: {messages: {info: Message, parts: Part[]}[]})`. It runs before every LLM request (and on the compaction input). Mutations are in-memory only. Use it only for optional verbatim re-injection (section 4.3). Avoid rewriting history, because it changes the cache prefix.

### 4.5 Events (via the `event` hook)
`session.created`, `session.compacted`, `session.deleted`, `session.diff`, `session.error`, `session.idle`, `session.status`, `session.updated`, `message.updated`, `message.part.updated`, `message.removed`, `message.part.removed`, `tool.execute.before/after`, `command.executed`, `file.edited`, `permission.asked/replied`, `server.connected`, `todo.updated`, `tui.*` [doc]. The event hook is invoked as `void hook.event?.(…)` (not awaited) [src], so **a rejected promise here is an unhandled rejection**. Always wrap it in try/catch.

### 4.6 Part-ID generator (mirrors `packages/schema/src/identifier.ts`)
```ts
let lastTs = 0, ctr = 0
const B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
function partId(): string {
  const now = Date.now(); if (now !== lastTs) { lastTs = now; ctr = 0 } ctr++
  const v = BigInt(now) * 0x1000n + BigInt(ctr)
  let hex = ""; for (let i = 0; i < 6; i++) hex += Number((v >> BigInt(40 - 8 * i)) & 0xffn).toString(16).padStart(2, "0")
  const rnd = crypto.getRandomValues(new Uint8Array(14)); let tail = ""; for (const b of rnd) tail += B62[b % 62]
  return "prt_" + hex + tail
}
```
Never reuse an existing part's id. The projector upserts on `id`, which would **overwrite the user's own text part**.

---

## 5. Error semantics (the "exit codes" equivalent): what breaks OpenCode

| Situation | Effect [src] |
|---|---|
| Hook throws or rejects | `Effect.promise` turns the rejection into a **defect**, which propagates into the caller. `chat.message`: the prompt fails (a session error). `tool.execute.before`: the tool call is blocked (the **documented** way to block, e.g. the `.env` protection example `throw new Error(...)`). `tool.execute.after`: the tool call errors (the model sees an error). `experimental.session.compacting`: compaction fails. `event`: unhandled rejection. |
| Hook hangs | The whole turn waits (no OpenCode timeout). |
| Plugin init (the factory) throws | Caught, logged "failed to load plugin"; OpenCode continues without it. |
| Module has a non-function export, or a v1 file plugin has no `id` | Load error reported as a session error; continues. |
| Invalid part pushed in `chat.message` | Logged "invalid user part before save", then persisted anyway; the DB projector is likely to die (section 4.1). |

**Never do:** throw or reject from any hook (wrap every hook body in `try { … } catch {}`, including `event`); await without a hard timeout (use `AbortController`, ≤3 s); set `output.prompt` in `experimental.session.compacting` (it replaces the compaction prompt); set `output.enabled=false` in `experimental.compaction.autocontinue`; mutate `output.args` in `tool.execute.before`; set `output.status` in `permission.ask`; mutate `output.text` in `experimental.text.complete`; push malformed parts; push to `experimental.chat.system.transform` (breaks prompt caching); add non-function exports to the plugin module.

---

## 6. MCP fallback registration (`~/.config/opencode/opencode.json`)
```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "subcortex": { "type": "local", "command": ["/opt/homebrew/bin/subcortex", "mcp"], "enabled": true,
                   "environment": { "SUBCORTEX_PORT": "7707" } }
  }
}
```
Tools appear as `subcortex_<tool>`. Remove by deleting the `mcp.subcortex` key. The file may be JSONC.

---

## 7. Mismatches in the existing code (file:line → fix)

1. **HIGH** `adapters/opencode/subcortex.ts:99-103`: the pushed part lacks `id`, `sessionID` and `messageID` (all required by the schema). When the "simple" hint fires, persistence very likely dies in `projector.ts` (`Effect.orDie`) and the **user's prompt fails**. Fix per section 4.1 with `partId()` from section 4.6. Also rephrase the text factually.
2. **HIGH** `adapters/opencode/subcortex.ts:111-131`: truncation applies to **every tool** (`read`, `grep`, `webfetch`, `task`, …), not just shell. Truncating a `read` result corrupts what the model believes a file contains. Add `if (input.tool !== "bash") return`.
3. `adapters/opencode/subcortex.ts:74-77, 116` `looksLikeError`: the substrings `error`/`failed` in the first 2 KB skip most real outputs (e.g. "0 errors", test names) while missing errors that appear later. Use `output.metadata?.exit !== 0` (the shell sets `metadata.exit`) plus a line-anchored regex. Also skip `output.metadata?.truncated === true` (OpenCode already spilled to a file, and our head/tail would cut the `Full output saved to:` pointer at the top).
4. `adapters/opencode/subcortex.ts:117`: `input.args` exists only in ≥1.1.62. Guard it with `input.args ?? {}` (already done in `argsSnippet`), and document the minimum version.
5. `adapters/opencode/subcortex.ts:134-142`: `experimental.session.compacting` only pushes a `/stats` URL line into the **summarizer prompt** (noise sent to the LLM). It implements neither behavior 3 (snapshot) nor behavior 4 (re-inject). Replace it with the ring-buffer snapshot per section 4.3 (write the file and push to `output.context`).
6. `adapters/opencode/subcortex.ts:19-28`: the port (7707) and all thresholds are hard-coded, which diverges from `~/.config/subcortex/config.json` (`port`, `thresholds.*`). Read them from plugin `options` (npm tuple), `SUBCORTEX_URL`, or a `/config` daemon endpoint.
7. `adapters/opencode/subcortex.ts:98`: the `SIMPLE_CONFIDENCE` check is fine. Note that `chat.message` also fires for subagent (`task`) prompts and `command.execute` expansions, so consider skipping when `input.agent` is a subagent [unverified field values].
8. `adapters/opencode/subcortex.ts:87`: legacy export format is OK. If you migrate to `export default { id, server }`, `id` is mandatory for file plugins. Keep exactly one kind of export; any other named export breaks loading.
9. `src/subcortex/installers/opencode.py:40-41`: hard-codes `~/.config/opencode/plugins`. OpenCode uses `$XDG_CONFIG_HOME/opencode` (xdg-basedir), so honor `XDG_CONFIG_HOME` (and optionally install to `$OPENCODE_CONFIG_DIR/plugins` when set).
10. `src/subcortex/installers/opencode.py:59-74`: overwrites an existing differing `subcortex.ts` after one backup. A second install with yet another version overwrites the `.bak`. Minor.
11. `src/subcortex/cli.py:237`: `hook opencode` is rejected by argparse (`choices` excludes it), giving exit 2. It is harmless because OpenCode never runs command hooks, but keep the hook path argparse-free (see the Claude spec).
12. `docs/opencode.md`: "no Node/npm install step" is only partly true (OpenCode runs `bun install @opencode-ai/plugin` in config dirs at startup). The "hooks are awaited sequentially" claim is correct, and there is also no OpenCode-side timeout. Document the `bash`-only scope and the minimum version v1.1.62.

---

## 8. Unverified or flagged
- [unverified runtime, strong src evidence] That the malformed synthetic part (item 1) kills the prompt, as opposed to merely writing a bad row.
- [unverified] That calling `client.session.messages()` from inside `experimental.session.compacting` is safe (re-entrancy and latency).
- [unverified] The fastest headless way to trigger compaction for e2e (`opencode run` plus a slash command vs a small-context model).
- [src] The `experimental.*` hooks may change without notice. Re-bisect on upgrade (the `packages/plugin/src/index.ts` `Hooks` interface is the contract).
- [src] Hooks of all plugins run sequentially. Another plugin that runs after ours can re-mutate `output.output`.
