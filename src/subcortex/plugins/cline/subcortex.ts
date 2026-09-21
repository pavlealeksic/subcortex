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
