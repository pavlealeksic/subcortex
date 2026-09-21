// subcortex plugin for OpenCode (>= 1.1.62) and Kilo Code CLI (an OpenCode fork).
//
// Installed by `subcortex install opencode` / `subcortex install kilo`. All
// decisions come from the local subcortex daemon's policy endpoints — no
// thresholds or heuristics live here:
//
//   chat.message                    -> POST /v1/prompt-hint; pushes one fully
//                                      formed synthetic text part (id/sessionID/
//                                      messageID set — a malformed part can fail
//                                      the user's prompt)
//   tool.execute.after              -> POST /v1/tool-output for `bash` only, exit 0,
//                                      not already spilled to a file by OpenCode;
//                                      a replacement is assigned to output.output
//   experimental.text.complete      -> records assistant text (never mutated)
//   experimental.session.compacting -> pushes the last few messages into the
//                                      compaction prompt so the summary keeps them
//
// OpenCode awaits plugin hooks with no timeout and turns any throw into an
// error for the operation the hook is attached to, so every hook catches
// everything and every request has a hard timeout.

import type { Plugin } from "@opencode-ai/plugin"

const TEMPLATED_URL = "__SUBCORTEX_URL__" // replaced by the installer
const BASE = (
  process.env.SUBCORTEX_URL || (TEMPLATED_URL.startsWith("http") ? TEMPLATED_URL : "http://127.0.0.1:7707")
).replace(/\/+$/, "")

const PROMPT_TIMEOUT_MS = 1500
const OUTPUT_TIMEOUT_MS = 3000
const LOCAL_MIN_OUTPUT_CHARS = 2000 // cheap pre-filter; the daemon applies the real threshold
const RING_SIZE = 6
const RING_TEXT_CHARS = 500

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

// Part ids mirror OpenCode's identifier scheme: "prt_" + 12 hex (ms * 0x1000 +
// counter) + 14 base62 chars. Never reuse an existing id — it would overwrite
// that part.
const B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
let lastTs = 0
let counter = 0
function partId(): string {
  const now = Date.now()
  if (now !== lastTs) {
    lastTs = now
    counter = 0
  }
  counter++
  const value = BigInt(now) * 0x1000n + BigInt(counter)
  let hex = ""
  for (let i = 0; i < 6; i++) hex += Number((value >> BigInt(40 - 8 * i)) & 0xffn).toString(16).padStart(2, "0")
  const random = crypto.getRandomValues(new Uint8Array(14))
  let tail = ""
  for (const b of random) tail += B62[b % 62]
  return "prt_" + hex + tail
}

type Message = { role: "user" | "assistant"; text: string }
const rings = new Map<string, Message[]>()

function remember(sessionID: string, role: Message["role"], text: string) {
  if (!sessionID || !text.trim()) return
  const ring = rings.get(sessionID) ?? []
  ring.push({ role, text: text.trim().slice(0, RING_TEXT_CHARS) })
  if (ring.length > RING_SIZE) ring.splice(0, ring.length - RING_SIZE)
  rings.set(sessionID, ring)
}

function userText(parts: unknown): string {
  if (!Array.isArray(parts)) return ""
  return parts
    .filter((p: any) => p && p.type === "text" && !p.synthetic && typeof p.text === "string")
    .map((p: any) => p.text)
    .join("\n")
    .trim()
}

const server: Plugin = async () => ({
  "chat.message": async (input: any, output: any) => {
    try {
      const prompt = userText(output?.parts)
      if (!prompt) return
      remember(String(input?.sessionID ?? ""), "user", prompt)
      const res = await post("/v1/prompt-hint", { prompt }, PROMPT_TIMEOUT_MS)
      const hint = res?.hint
      const messageID = output?.message?.id
      const sessionID = input?.sessionID ?? output?.message?.sessionID
      if (typeof hint !== "string" || !messageID || !sessionID || !Array.isArray(output.parts)) return
      output.parts.push({ id: partId(), sessionID, messageID, type: "text", synthetic: true, text: hint })
    } catch {
      // fail open: the user's message goes through untouched
    }
  },

  "tool.execute.after": async (input: any, output: any) => {
    try {
      if (input?.tool !== "bash" || typeof output?.output !== "string") return
      const meta = output.metadata ?? {}
      if (meta.exit !== 0 || meta.truncated === true) return // failures stay; spilled output stays
      if (output.output.length < LOCAL_MIN_OUTPUT_CHARS) return
      const res = await post(
        "/v1/tool-output",
        { output: output.output, tool: "bash", input: input?.args ?? {} },
        OUTPUT_TIMEOUT_MS,
      )
      if (typeof res?.replacement === "string") output.output = res.replacement
    } catch {
      // fail open: the full tool output passes through untouched
    }
  },

  "experimental.text.complete": async (input: any, output: any) => {
    try {
      if (typeof output?.text === "string") remember(String(input?.sessionID ?? ""), "assistant", output.text)
    } catch {}
  },

  "experimental.session.compacting": async (input: any, output: any) => {
    try {
      const ring = rings.get(String(input?.sessionID ?? ""))
      if (!ring?.length || !Array.isArray(output?.context)) return
      const lines = ring.map((m) => `${m.role}: ${m.text}`).join("\n")
      output.context.push(`[subcortex] Most recent messages, verbatim (keep their key facts):\n${lines}`)
    } catch {}
  },
})

// One export only (any other export breaks OpenCode's plugin loader); the
// `{id, server}` object form is accepted by OpenCode and required by Kilo.
export default { id: "subcortex", server }
