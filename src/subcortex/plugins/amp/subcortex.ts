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

const PLUGIN_TUI = "amp" // keeps sessions of different TUIs apart in the daemon

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
      if (id) {
        firstSeen.delete(threadID) // re-insert: Map order is least recently used first
        firstSeen.set(threadID, id)
        if (firstSeen.size > 256) firstSeen.delete(firstSeen.keys().next().value as string)
      }
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
      const hint = await post("/v1/prompt-hint", { prompt: String(event.message ?? ""), session_id: thread }, PROMPT_TIMEOUT_MS)
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
      const exit = (event.output as any)?.exitCode
      if (typeof exit === "number" && exit !== 0) return // failures stay whole: Amp reports them as status "done"
      const res = await post(
        "/v1/tool-output",
        { output: text, tool: String(event.tool ?? "Bash"), input: { command: shell.command },
          session_id: String(event?.thread?.id ?? "") },
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
