# Amp CLI (ampcode.com): subcortex adapter spec

Verified 2026-09-21 against official docs and the published `@ampcode/plugin` type package. Nothing was installed and nothing on this machine was used.

**Verdict:** there are no command hooks, so write a plugin-only adapter: one Bun TS/JS plugin that calls the daemon on 127.0.0.1:7707.

| Behavior | Seam | Status |
|---|---|---|
| 1. Prompt submit: hint injection | `agent.start` returns `{message:{content, display:false}}` | Native |
| 2. Replace large tool output | `tool.result` returns `{status, output}` | Native |
| 3. Pre-compaction snapshot | No compaction event exists | Workaround: rolling snapshot on `agent.end` (+ `tool.result`) |
| 4. Re-inject after compaction | No post-compaction event. `session.start` cannot return anything | Workaround: detect compaction on the next `agent.start` and inject there |

MCP (`amp.mcpServers`) works but is only a fallback. The plugin is strictly better.

---

## 1. Sources and latest versions

| Item | Value | Source |
|---|---|---|
| CLI npm `@sourcegraph/amp` (shim that pulls `@ampcode/cli` plus a per-platform native Bun binary) | `0.0.1790006436-gaf5042`, published 2026-09-21T16:32Z. Rolling builds, several per day, auto-update on by default | registry.npmjs.org/@sourcegraph/amp |
| Plugin types `@ampcode/plugin` | `0.0.0-20260921001857-g6fa23da`, 2026-09-21. `index.d.ts` header says `sitemap-lastmod: /docs/plugin-api 2026-09-01` | registry.npmjs.org/@ampcode/plugin (tarball `index.d.ts`, 2032 lines) |
| Architecture | "Neo" rebuild announced 2026-05-06, GA ("Drop the Neo") 2026-05-27. Described as "compaction-first, plugin-powered". Handoff removed. Permissions became a built-in plugin | https://ampcode.com/news/neo , https://ampcode.com/chronicle |
| Plugin guide | https://ampcode.com/docs/customize/plugins | |
| Plugin API reference | https://ampcode.com/docs/plugin-api (also /manual/plugin-api) | |
| Settings | https://ampcode.com/docs/cli/settings | |
| MCP | https://ampcode.com/docs/customize/mcp | |
| Execute mode | https://ampcode.com/docs/cli/execute-mode | |
| Streaming JSON | https://ampcode.com/docs/cli/streaming-json | |

**Legacy hooks.** Before Neo, Amp had `amp.hooks` in settings, with events `tool:pre-execute`/`tool:post-execute` and actions `send-user-message`/`redact-tool-input`. The current settings reference no longer lists `amp.hooks`, and the old /manual page now redirects to the intro. Treat it as removed. It could not inject context or replace output anyway, so do not use it.

**Claude Code compatibility:** none. Amp loads Claude Code skills (`.claude/skills`, `~/.claude/skills`, `~/.claude/plugins/cache`; turn off with `amp.skills.disableClaudeCodeSkills`). It does not read Claude Code hooks from `.claude/settings.json`. The `--stream-json` output format is Claude-Code-compatible (the docs say so), which helps e2e assertions.

---

## 2. Config paths and environment variables

**Plugin locations.** Precedence when names collide: project, then system, then personal, then workspace.
- Project: `<repo>/.amp/plugins/<name>.ts|.js`, or a directory plugin `<repo>/.amp/plugins/<name>/index.ts|index.js`. `index.ts` wins if both exist.
- System: `$XDG_CONFIG_HOME/amp/plugins/` if `XDG_CONFIG_HOME` is set. Otherwise `~/.config/amp/plugins/` on macOS/Linux or `%USERPROFILE%\.config\amp\plugins\` on Windows. **Put the subcortex installer target here.**
- Personal and workspace plugins live in Git repos hosted on ampcode.com (`amp plugins repositories`, `amp clone user-plugins`). They are not a local file path.
- `amp plugins add <url>` installs a single-file plugin into the system dir. `--target workspace` installs into `.amp/plugins/` instead.
- The runtime is Bun, bundled in the Amp binary. `import type { PluginAPI } from '@ampcode/plugin'` is type-only and erased at runtime, so no npm install is needed.
- Plugins load automatically with no enable flag. "Plugin activation settings apply to both interactive `amp` sessions and `amp --execute` runs."
- Reload with command palette (Ctrl+O) `plugins: reload`. List with `amp plugins list`, which shows loaded plugins, their sources, registered events, commands and tools.

**Settings files.** All keys carry the `amp.` prefix. JSONC is allowed.
- User: `~/.config/amp/settings.json` or `settings.jsonc`. Windows: `%USERPROFILE%\.config\amp\settings.json`.
- Workspace: the nearest `.amp/settings.json(c)`, searched upward from cwd to the repo root. Workspace settings override user settings.
- `--settings-file <path>` replaces the user settings file.
- Enterprise managed settings override everything: `/Library/Application Support/ampcode/managed-settings.json` (macOS), `/etc/ampcode/managed-settings.json` (Linux), `%ProgramData%\ampcode\managed-settings.json` (Windows).

**Environment variables and flags relevant to e2e:**
- `AMP_API_KEY=sgamp_...`: required for non-interactive runs. The CLI rejects the short-lived `amp login` session token.
- `XDG_CONFIG_HOME`: relocates the system plugin dir. Whether it also relocates `settings.json` is UNVERIFIED, since the settings doc names only `~/.config/amp/`. Pass `--settings-file` explicitly.
- `AMP_SKIP_UPDATE_CHECK=1` disables all update checks. You can also set `amp.updates.mode: "disabled"`.
- `AMP_URL`: custom server URL (per `PluginSystem.ampURL` doc comment).
- `AMP_DISABLE_AMP_THREAD_TRAILER=1`, `AMP_DISABLE_AMP_COAUTHOR_TRAILER=1`, `AMP_FORCE_BEL`, plus `HTTP(S)_PROXY` and `NODE_EXTRA_CA_CERTS`.
- `-x/--execute "<prompt>"`: one turn, then exit. It is also turned on automatically when stdout is redirected.
- `--stream-json` (requires `-x`), `--stream-json-input`, `--stream-json-thinking`.
- **`--plugin-ready-timeout [secs]`**: execute mode waits for plugins before starting the turn. Bare flag means 10s, max 300, 0 disables. The docs warn: "Without it, the turn can start before plugins finish loading, and those events are skipped." **Always pass it in e2e.**
- `--mcp-config '<json>'`: adds MCP servers for this run with the highest precedence and no approval prompt.
- `--executor local|orb|runner:<id>`. Plugins in an orb or runner run remotely, where 127.0.0.1:7707 is not the user's daemon, so they must fail open.

**Isolated e2e recipe.** Pieces marked UNVERIFIED have not been run.
```sh
T=$(mktemp -d); mkdir -p "$T/xdg/amp/plugins" "$T/proj"
cp subcortex-amp.ts "$T/xdg/amp/plugins/subcortex.ts"
echo '{"amp.updates.mode":"disabled"}' > "$T/settings.json"
cd "$T/proj" && git init -q
XDG_CONFIG_HOME="$T/xdg" AMP_API_KEY=sgamp_... AMP_SKIP_UPDATE_CHECK=1 \
  amp --settings-file "$T/settings.json" -x "run: seq 1 200000" \
      --plugin-ready-timeout 30 --stream-json
# Verify registration with: XDG_CONFIG_HOME="$T/xdg" amp plugins list
```
Caveat (UNVERIFIED): personal and workspace plugins hosted on ampcode.com still load for the authenticated account. Use a clean test account, or tolerate other plugins.

---

## 3. Registration snippet (the plugin)

Install to `~/.config/amp/plugins/subcortex.ts` (or `$XDG_CONFIG_HOME/amp/plugins/subcortex.ts`). The daemon paths below are placeholders; point them at the same endpoints the OpenCode plugin uses.

```ts
// subcortex adapter for Amp. Fail-open: every handler catches everything and returns undefined/{}.
import type { PluginAPI } from '@ampcode/plugin'
export const description = 'subcortex: prompt hints, large-output trimming, compaction snapshots'

const BASE = 'http://127.0.0.1:7707'
async function call(path: string, body: unknown, ms: number): Promise<any> {
  try {
    const r = await fetch(BASE + path, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body), signal: AbortSignal.timeout(ms),
    })
    return r.ok ? await r.json() : null
  } catch { return null }
}

export default function (amp: PluginAPI) {
  const firstSeen = new Map<string, string>() // threadID -> id of first visible (post-compaction) message

  // (1) + (4): prompt submit; also post-compaction re-injection
  amp.on('agent.start', async (event, ctx) => {
    try {
      if (amp.system.executor.kind !== 'local') return {}
      let compacted = false
      try {
        const head = await ctx.thread.messages({ from: 'start', limit: 1 }) // default view: compaction summary first if compacted
        const id = head[0] ? String(head[0].id) : ''
        const prev = firstSeen.get(event.thread.id)
        compacted = prev !== undefined && id !== '' && id !== prev
        if (id) firstSeen.set(event.thread.id, id)
      } catch {}
      const res = await call('/v1/amp/agent.start', {
        thread_id: event.thread.id, prompt: event.message, message_id: event.id, compacted,
        cwd: amp.system.workspaceRoot ? amp.helpers.filePathFromURI(amp.system.workspaceRoot) : null,
      }, 1500)
      const text = typeof res?.additionalContext === 'string' ? res.additionalContext : ''
      return text ? { message: { content: text, display: false } } : {}
    } catch { return {} }
  })

  // (2): replace huge disposable shell output
  amp.on('tool.result', async (event) => {
    try {
      if (event.status !== 'done' || amp.system.executor.kind !== 'local') return
      const sh = amp.helpers.shellCommandFromToolCall(event) // Bash / shell_command only
      if (!sh) return
      const out = event.output
      const text = typeof out === 'string' ? out
        : (out && typeof (out as any).output === 'string') ? (out as any).output : null
      if (text === null || text.length < 20_000) return        // cheap local pre-filter
      const res = await call('/v1/amp/tool.result', {
        thread_id: event.thread.id, tool: event.tool, command: sh.command, cwd: sh.dir, output: text,
      }, 3000)
      const repl = res?.replacement
      if (typeof repl !== 'string') return                     // undefined => keep original
      const newOut = typeof out === 'string' ? repl : { ...(out as object), output: repl }
      return { status: 'done', output: newOut }                // never change status
    } catch { return }
  })

  // (3): rolling snapshot (no pre-compaction event exists)
  amp.on('agent.end', async (event, ctx) => {
    try {
      let msgs = event.messages
      try { msgs = await ctx.thread.messages({ full: true, from: 'end', limit: 20 }) } catch {}
      void call('/v1/amp/snapshot', { thread_id: event.thread.id, status: event.status, messages: msgs }, 1500)
    } catch {}
    return // NEVER return {action:'continue'}
  })

  amp.on('session.start', async (event, ctx) => {
    // fire-and-forget event; it cannot inject context. Only prime the compaction detector.
    try {
      const head = await ctx.thread.messages({ from: 'start', limit: 1 })
      if (head[0]) firstSeen.set(event.thread.id, String(head[0].id))
    } catch {}
  })
}
```
Do not register `tool.call`. It is a request event, and a registered handler must return a decision on every tool call.

---

## 4. Per-event payloads and responses

Everything below is verbatim from `@ampcode/plugin` `index.d.ts` unless marked otherwise. Handler signature: `amp.on(event, (event, ctx) => result)`. `ctx` holds `{ logger, $, ui, ai, system, span?, thread: PluginThread }`. "All plugin events are thread-scoped." "Plugins receive events for every thread the client hosts, including side threads", so key all state by `event.thread.id`. When several plugins handle the same event, their order is undefined.

`PluginEventMap` = `session.start | tool.call | tool.result | agent.start | agent.end`. The request events, which expect a result, are `tool.call | tool.result | agent.start | agent.end`. The docs also describe `changes.prompt`, which fires for the Ship/Push-to-Branch prompt and returns `{append}`, plus `event.changesWorkflow` on `agent.start`. Neither appears in the 2026-09-21 d.ts. Not needed.

### 4.1 Prompt submit: `agent.start` (behavior 1)
- Fires "when a user submits a prompt (initial or reply)".
- Payload: `{ thread: { id: ThreadID /* "T-..." */ }, message: string /* user prompt */, id: ThreadMessageID /* number|string */ }`.
- Result `AgentStartResult`: `{ message?: { content: string; display?: boolean } }`. Per the d.ts: "A message to append after the user's content in the user message. If display is true, the message is shown in the UI. Defaults to false." The guide adds: "The appended text is hidden unless display is true."
- **Context injection: yes.** Return `{message:{content:hint}}`. For no-op, return `{}` (the docs' turn-stats example does this) or `undefined` (the kitchen-sink example does a bare `return`).
- You cannot rewrite or replace the prompt itself. You can only append.
- Execute mode needs `--plugin-ready-timeout`, or `agent.start` may be skipped.

### 4.2 After tool: `tool.result` (behavior 2)
- Fires "after a tool finishes and before the result is sent back to the model".
- Payload `ToolResultEvent`: `{ toolUseID: string, tool: string, input: Record<string,unknown>, status: 'done'|'error'|'cancelled', error?: string, output?: unknown, thread: { id } }`.
- Result `ToolResultResult`: `{status:'done', output?: unknown} | {status:'error', error?: string, output?: unknown} | {status:'cancelled', error?: string, output?: unknown} | undefined | void`. The guide says: "Return nothing to keep the original result, or return a replacement status/output."
- **Output replacement: yes, native.** Return `{ status: event.status, output: <new> }` and keep `error` when status is `error`.
- The shell tool is named `Bash` in `--stream-json` tool lists. `amp.helpers.shellCommandFromToolCall(event)` returns `{command, dir?}` for "Bash or shell_command" tool calls, otherwise `null`. It accepts `ToolResultEvent`, which is structurally a `ToolCall`.
- UNVERIFIED: the runtime shape of `output` for Bash. It is typed `unknown`. The `synthesize` result type `{ output: string; exitCode?: number }` suggests Bash results look like `{output: string, exitCode: number}`. The adapter handles both string and `{output}` object shapes and leaves anything else untouched. Confirm with a one-off logging plugin (`ctx.logger.log(JSON.stringify(event.output).slice(0,500))`).
- Alternative, not recommended: `tool.call` → `{action:'modify', input}` to pipe the command through a trimmer. That changes semantics and exit codes.

### 4.3 Before compaction (behavior 3): impossible natively
- Amp compacts automatically "when the context window is 90% full" (news/neo). A custom agent can override the trigger with `createAgent({compactionThresholdTokens})`: "Estimated input-token count at which Amp automatically compacts the thread... defaults to 90%."
- No plugin event exists for pre- or post-compaction, and no event can veto or observe it. The `PluginEventMap` above is the complete list.
- **Workaround:** keep a rolling snapshot.
  - On `agent.end`, whose payload is `{thread, message, id, status:'done'|'error'|'cancelled', messages: ThreadMessage[] /* all messages since agent.start */}`, snapshot `event.messages`, or `ctx.thread.messages({ full: true, from: 'end', limit: 20 })` (max `limit` 20).
  - Optionally refresh on every N-th `tool.result`, because compaction can happen mid-turn during long tool loops, and a snapshot taken only at `agent.end` can then be one turn stale.
  - `ThreadMessage` roles are `user | assistant | info`, with content blocks `text | thinking | tool_use | tool_result`.

### 4.4 Session start after compaction (behavior 4): impossible natively, workaround exists
- `session.start` payload is only `{ thread: { id } }`. It fires "when the user sends the first message in a new thread or opens/switches to an existing thread". It is **fire-and-forget** (not in the request map), so **it cannot inject context**, and it does **not** fire after compaction (compaction continues inside the same thread). There is also no `session.end`.
- **Workaround:** detect compaction on the next `agent.start` and inject the snapshot through its `message.content`.
  - Detection relies on documented behavior of `ctx.thread.messages()`: "By default this reads the messages a new inference turn would see: after a compaction, the latest compaction summary and the messages from the compaction cut point onward. Pass `full: true` to read the entire transcript."
  - So the first message in the default view (`messages({from:'start', limit:1})`) changes identity after each compaction. Track it per thread; the adapter above does this.
  - UNVERIFIED: that the compaction-summary message gets a new, distinct `id` each time.
  - Limitation: when compaction happens mid-turn, re-injection waits for the next user prompt.
- Not recommended: `ctx.thread.appendUserMessage({type:'user-message', content}, {steer:true})` from `tool.result`. It injects immediately but creates a visible user message that steers the agent.

---

## 5. Exit codes and blocking responses to avoid

This is not a process hook, so there are no exit codes. Everything fails open inside the plugin.

| Where | Never do | Effect |
|---|---|---|
| `tool.call` | Register it at all. If you must: never return `reject-and-continue`, `modify`, `synthesize`, or **`{action:'error'}`** | `error` "stops the thread worker and shows an ephemeral error". `reject-and-continue` blocks the tool |
| `tool.result` | Change `status` (for example to `'error'`/`'cancelled'`), or drop `error` | The model sees a failed or cancelled tool |
| `agent.start` | Call `ctx.thread.cancel()` | "During agent.start, prevents the turn from starting" |
| `agent.end` | Return `{action:'continue', userMessage}` | Starts extra agent turns (up to `maxContinuations`, default 5) |
| any | `ctx.ui.confirm/input/select` | Blocks waiting on the user. Headless/orb may throw (`amp.helpers.isPluginUINotAvailableError`) |
| any | Throw, or hang | Throw semantics are undocumented, and handlers are awaited with no documented timeout. Wrap every handler in try/catch and put `AbortSignal.timeout()` on every fetch (roughly 1.5s for prompts, 3s for tool results) |
| top level | Side effects or throwing at module load | A load failure presumably disables the plugin (UNVERIFIED). Keep the module body declarative |
| executor | Assume a local daemon | In orbs or runners (`amp.system.executor.kind !== 'local'`), skip or no-op |

`onDispose` callbacks get about 3s on unload, reload or graceful exit, and do not run on crash or SIGKILL.

---

## 6. MCP registration (fallback only)

CLI:
```sh
amp mcp add subcortex -- subcortex mcp
```
User settings (`~/.config/amp/settings.json`):
```json
{ "amp.mcpServers": { "subcortex": { "command": "subcortex", "args": ["mcp"], "env": {} } } }
```
- Local fields: `command`, `args`, `env`. Remote fields: `url`, `headers`. `${VAR}` expansion is supported in config files.
- Precedence: `--mcp-config` beats workspace `.amp/settings.json`, which beats user `~/.config/amp/settings.json`, which beats skills (`mcp.json` in a skill dir, lazy-loaded).
- Workspace-level servers require approval (`amp mcp approve subcortex`, see `amp mcp doctor`). User settings and `--mcp-config` servers do not.
- `amp.mcpPermissions` rules (first match wins, and servers are allowed when nothing matches) can reject it. `amp.tools.disable` can hide its tools.
- Tools are exposed as `mcp__subcortex__<tool>`.
- E2E: `amp --mcp-config '{"subcortex":{"command":"subcortex","args":["mcp"]}}' -x "..."`.
- MCP cannot do behaviors 2–4. The model must choose to call the tools.

---

## 7. Unverified claims and open risks
1. The runtime shape of `ToolResultEvent.output` for `Bash` (string vs `{output, exitCode}`). Log it once before shipping.
2. What happens when a handler throws or never resolves: undocumented. The d.ts states no handler timeout.
3. Whether a new compaction yields a new first-message `id` in the default `messages()` view (the basis of behavior-4 detection).
4. Whether returning `undefined` from `agent.start` is officially valid. The type says `AgentStartResult`, but the docs' own kitchen-sink example does it. Return `{}` to be safe.
5. Whether `XDG_CONFIG_HOME` also moves `settings.json`.
6. Whether hosted personal and workspace plugins can be suppressed for a hermetic e2e.
7. `changes.prompt` and `changesWorkflow` are documented but absent from the 2026-09-21 d.ts, which shows docs and types drift. Re-check the d.ts at implementation time: `curl -sL $(npm view @ampcode/plugin dist.tarball)`.
8. Legacy `amp.hooks` is assumed removed in Neo, based on its absence from the current settings reference.
9. Old CLI builds may lack `--plugin-ready-timeout` or `tool.result` output replacement. Auto-update makes this unlikely, but `amp.updates.mode: "disabled"` users can lag.
