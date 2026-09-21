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
const MAX_SESSIONS = 64 // a long-running server sees many sessions; keep memory bounded
const RING_TEXT_CHARS = 500

const PLUGIN_TUI = "opencode" // keeps sessions of different TUIs apart in the daemon

// >>> subcortex transport (identical in every subcortex plugin) >>>
// The daemon is reached over a raw TCP socket, one HTTP/1.0 request per
// connection. Not fetch: Bun sends even loopback requests through HTTP_PROXY,
// which breaks every call or hands prompts and output to a proxy. Our side is
// never half-closed before the reply (Bun then drops the response). node:net is
// imported lazily: a static import that fails to resolve would stop the host
// from starting. Every failure, timeout or abort resolves to null.
const DAEMON = (() => {
  try {
    const url = new URL(BASE)
    return { host: url.hostname.replace(/^\[|\]$/g, ""), port: Number(url.port) || 80 }
  } catch {
    return { host: "127.0.0.1", port: 7707 }
  }
})()
const MAX_RESPONSE_BYTES = 8_000_000
const TEMPLATED_TOKEN_FILE = "__SUBCORTEX_TOKEN_FILE__" // replaced by the installer
let net: any = undefined // undefined: not loaded yet; null: unavailable, use fetch
let token: string | undefined // the daemon's per-user token (a 0600 file in its data dir)

async function daemonToken(): Promise<string> {
  if (token !== undefined) return token
  try {
    const fs = await import("node:fs")
    const dir = process.env.SUBCORTEX_DATA_DIR
    const file = dir
      ? dir.replace(/\/+$/, "") + "/token"
      : TEMPLATED_TOKEN_FILE.startsWith("/")
        ? TEMPLATED_TOKEN_FILE
        : (process.env.HOME ?? "") + "/.local/share/subcortex/token"
    token = String(fs.readFileSync(file, "utf8")).trim()
  } catch {
    token = ""
  }
  if (!/^[0-9a-f]*$/.test(token)) token = ""
  return token
}

async function post(path: string, body: unknown, ms: number, outer?: AbortSignal): Promise<any> {
  try {
    if (outer?.aborted) return null
    if (net === undefined) {
      try {
        net = await import("node:net")
      } catch {
        net = null
      }
    }
    const payload = JSON.stringify({ tui: PLUGIN_TUI, ...(body as object) })
    const secret = await daemonToken()
    const reply = net ? await viaSocket(path, payload, secret, ms, outer) : await viaFetch(path, payload, secret, ms, outer)
    if (reply === null) token = undefined // re-read next time: the daemon may have made a new one
    return reply
  } catch {
    return null
  }
}

function viaSocket(path: string, payload: string, secret: string, ms: number, outer?: AbortSignal): Promise<any> {
  return new Promise((resolve) => {
    const chunks: any[] = []
    let size = 0
    let settled = false
    let sock: any
    const finish = (value: any) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      outer?.removeEventListener("abort", abort)
      try {
        sock?.destroy()
      } catch {}
      resolve(value)
    }
    const abort = () => finish(null)
    const timer = setTimeout(abort, ms)
    outer?.addEventListener("abort", abort, { once: true })
    try {
      const bytes = Buffer.from(payload, "utf8")
      sock = net.connect({ host: DAEMON.host, port: DAEMON.port })
      sock.on("connect", () => {
        sock.write(`POST ${path} HTTP/1.0\r\nHost: ${DAEMON.host}\r\nContent-Type: application/json\r\n` +
          `Content-Length: ${bytes.length}\r\nX-Subcortex-Token: ${secret}\r\nX-Subcortex-Timeout-Ms: ${ms}\r\n\r\n`)
        sock.write(bytes)
      })
      sock.on("data", (chunk: any) => {
        size += chunk.length
        if (size > MAX_RESPONSE_BYTES) finish(null)
        else chunks.push(chunk)
      })
      sock.on("error", abort)
      sock.on("close", () => finish(parseReply(Buffer.concat(chunks).toString("utf8"))))
    } catch {
      finish(null)
    }
  })
}

async function viaFetch(path: string, payload: string, secret: string, ms: number, outer?: AbortSignal): Promise<any> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), ms)
  const abort = () => controller.abort()
  outer?.addEventListener("abort", abort, { once: true })
  try {
    const res = await fetch(BASE + path, {
      method: "POST",
      headers: { "content-type": "application/json", "x-subcortex-token": secret, "x-subcortex-timeout-ms": String(ms) },
      body: payload,
      signal: controller.signal,
    })
    return parseReply(`HTTP/1.0 ${res.status} -\r\n\r\n${await res.text()}`)
  } catch {
    return null
  } finally {
    clearTimeout(timer)
    outer?.removeEventListener("abort", abort)
  }
}

function parseReply(raw: string): any {
  const split = raw.indexOf("\r\n\r\n")
  if (split < 0 || !/^HTTP\/1\.[01] 200 /.test(raw)) return null
  try {
    const data = JSON.parse(raw.slice(split + 4))
    return data && typeof data === "object" && data.success !== false ? data : null
  } catch {
    return null
  }
}
// <<< subcortex transport <<<

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
  rings.delete(sessionID) // re-insert: Map order is least recently used first
  rings.set(sessionID, ring)
  if (rings.size > MAX_SESSIONS) rings.delete(rings.keys().next().value as string)
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
      const res = await post("/v1/prompt-hint", { prompt, session_id: String(input?.sessionID ?? "") }, PROMPT_TIMEOUT_MS)
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
        { output: output.output, tool: "bash", input: input?.args ?? {}, session_id: String(input?.sessionID ?? "") },
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
