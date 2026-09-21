// subcortex plugin for Pi (@earendil-works/pi-coding-agent >= 0.87.0).
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
