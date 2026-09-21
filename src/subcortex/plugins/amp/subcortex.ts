// subcortex plugin for Amp (ampcode.com, Neo plugin API).
//
// Installed by `subcortex install amp` to ~/.config/amp/plugins/subcortex.ts.
// All decisions come from the local subcortex daemon's policy endpoints — this
// file holds no thresholds or heuristics of its own:
//
//   agent.start  -> POST /v1/prompt-hint (+ /v1/restore after a compaction);
//                   the text is appended to the user message, hidden from the UI
//   tool.result  -> POST /v1/tool-output for shell results; a replacement keeps
//                   the original status, only the output changes
//   agent.end    -> POST /v1/snapshot (rolling: Amp has no compaction event)
//   session.start-> primes compaction detection (the first message a new turn
//                   would see changes identity after each compaction)
//
// Fail-open everywhere: every handler catches everything, every request has a
// timeout, and nothing here ever cancels, rejects, continues or blocks a turn.
// `tool.call` is deliberately not registered.

import type { PluginAPI } from "@ampcode/plugin"

export const description = "subcortex: prompt hints, large-output trimming, compaction snapshots"

const TEMPLATED_URL = "__SUBCORTEX_URL__" // replaced by the installer
const BASE = (
  process.env.SUBCORTEX_URL || (TEMPLATED_URL.startsWith("http") ? TEMPLATED_URL : "http://127.0.0.1:7707")
).replace(/\/+$/, "")

const PROMPT_TIMEOUT_MS = 1500
const OUTPUT_TIMEOUT_MS = 3000
const LOCAL_MIN_OUTPUT_CHARS = 2000 // cheap pre-filter; the daemon applies the real threshold

async function post(path: string, body: unknown, ms: number): Promise<any> {
  try {
    const res = await fetch(BASE + path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(ms),
    })
    if (!res.ok) return null
    const data = await res.json()
    return data && typeof data === "object" && data.success !== false ? data : null
  } catch {
    return null
  }
}

function outputText(output: unknown): string | null {
  if (typeof output === "string") return output
  if (output && typeof output === "object" && typeof (output as any).output === "string") {
    return (output as any).output
  }
  return null
}

export default function (amp: PluginAPI) {
  // threadID -> id of the first message a new turn would see
  const firstSeen = new Map<string, string>()

  async function compactedSince(threadID: string, ctx: any): Promise<boolean> {
    try {
      const head = await ctx.thread.messages({ from: "start", limit: 1 })
      const id = head && head[0] ? String(head[0].id) : ""
      const prev = firstSeen.get(threadID)
      if (id) firstSeen.set(threadID, id)
      return prev !== undefined && id !== "" && id !== prev
    } catch {
      return false
    }
  }

  const local = () => {
    try {
      return amp.system.executor.kind === "local"
    } catch {
      return true
    }
  }

  amp.on("session.start", async (event: any, ctx: any) => {
    try {
      await compactedSince(event.thread.id, ctx)
    } catch {}
  })

  amp.on("agent.start", async (event: any, ctx: any) => {
    try {
      if (!local()) return {}
      const thread = String(event.thread.id)
      const parts: string[] = []
      if (await compactedSince(thread, ctx)) {
        const restored = await post("/v1/restore", { session_id: thread }, PROMPT_TIMEOUT_MS)
        if (typeof restored?.context === "string") parts.push(restored.context)
      }
      const hint = await post("/v1/prompt-hint", { prompt: String(event.message ?? "") }, PROMPT_TIMEOUT_MS)
      if (typeof hint?.hint === "string") parts.push(hint.hint)
      return parts.length ? { message: { content: parts.join("\n\n"), display: false } } : {}
    } catch {
      return {}
    }
  })

  amp.on("tool.result", async (event: any) => {
    try {
      if (event.status !== "done" || !local()) return
      const shell = amp.helpers.shellCommandFromToolCall(event)
      if (!shell) return
      const text = outputText(event.output)
      if (text === null || text.length < LOCAL_MIN_OUTPUT_CHARS) return
      const res = await post(
        "/v1/tool-output",
        { output: text, tool: String(event.tool ?? "Bash"), input: { command: shell.command } },
        OUTPUT_TIMEOUT_MS,
      )
      const replacement = res?.replacement
      if (typeof replacement !== "string") return
      const output = typeof event.output === "string" ? replacement : { ...(event.output as object), output: replacement }
      return { status: "done", output }
    } catch {
      return
    }
  })

  amp.on("agent.end", async (event: any, ctx: any) => {
    try {
      let messages = event.messages
      try {
        messages = await ctx.thread.messages({ full: true, from: "end", limit: 20 })
      } catch {}
      void post("/v1/snapshot", { session_id: String(event.thread.id), messages }, PROMPT_TIMEOUT_MS)
    } catch {}
    return // never {action: "continue"}
  })
}
