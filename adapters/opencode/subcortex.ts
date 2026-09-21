// subcortex OpenCode plugin.
//
// Routes every prompt/tool output through the local subcortex daemon
// (System-1 verdicts) and mutates hook outputs to nudge the LLM:
//
//   chat.message                    — classify the prompt; if it is simple with
//                                     high confidence, append a synthetic note
//                                     telling the model to take the direct path.
//   tool.execute.after              — judge long tool outputs; if an output is
//                                     disposable, replace it with head+tail.
//   experimental.session.compacting — leave a pointer to the daemon stats.
//
// Everything is fail-open: any error, timeout, or malformed response leaves
// the session untouched. This plugin must never throw.

import type { Plugin } from "@opencode-ai/plugin"

// Daemon base URL; override with SUBCORTEX_URL (e.g. a non-default port).
const SUBCORTEX_URL = (process.env.SUBCORTEX_URL || "http://127.0.0.1:7707").replace(/\/+$/, "")

// OpenCode awaits hooks sequentially before the LLM call continues, so every
// request gets a hard timeout — a slow/missing daemon must never stall a turn.
const REQUEST_TIMEOUT_MS = 3000

// Mirrors ~/.config/subcortex/config.json thresholds.* defaults; keep in sync.
const SIMPLE_CONFIDENCE = 0.8 // min confidence to flag a prompt as simple
const MIN_OUTPUT_CHARS = 6000 // shorter tool outputs are cheap to keep, never judged
const P_NEEDED_THRESHOLD = 0.3 // p_needed below this = disposable output, truncate

const HEAD_CHARS = 1000 // kept prefix when truncating tool output
const TAIL_CHARS = 500 // kept suffix when truncating tool output
const ERROR_SCAN_CHARS = 2000 // leading slice scanned for error markers
const ARGS_SNIPPET_CHARS = 200 // how much of the tool args becomes verdict context

interface Verdict {
  [key: string]: unknown
}

// POST to the daemon and unwrap the verdict body. The daemon answers
// {"success": bool, "verdict": {...}}; a flat {...} body is accepted too so the
// plugin keeps working if the envelope changes. Returns null on ANY failure.
async function postVerdict(path: string, payload: unknown): Promise<Verdict | null> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  try {
    const res = await fetch(`${SUBCORTEX_URL}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    })
    if (!res.ok) return null
    const data = await res.json()
    if (!data || typeof data !== "object") return null
    if ((data as Verdict).success === false) return null
    const verdict = (data as Verdict).verdict
    return verdict && typeof verdict === "object" ? (verdict as Verdict) : (data as Verdict)
  } catch {
    return null // daemon down, timeout, bad JSON — fail open
  } finally {
    clearTimeout(timer)
  }
}

function textFromParts(parts: unknown): string {
  if (!Array.isArray(parts)) return ""
  return parts
    .filter((p) => p && typeof p === "object" && (p as Verdict).type === "text")
    .map((p) => String((p as Verdict).text ?? ""))
    .join("\n")
    .trim()
}

function looksLikeError(output: string): boolean {
  const probe = output.slice(0, ERROR_SCAN_CHARS).toLowerCase()
  return probe.includes("error") || probe.includes("traceback") || probe.includes("failed")
}

function argsSnippet(args: unknown): string {
  try {
    return JSON.stringify(args ?? {}).slice(0, ARGS_SNIPPET_CHARS)
  } catch {
    return ""
  }
}

export const Subcortex: Plugin = async ({ directory }) => {
  return {
    // Fires before the message is persisted and the LLM loop starts; mutations
    // to output.parts are persisted and seen by the model.
    "chat.message": async (input, output) => {
      try {
        const prompt = textFromParts(output.parts)
        if (!prompt) return
        const verdict = await postVerdict("/verdict/prompt", { prompt })
        if (!verdict) return
        const confidence = Number(verdict.confidence)
        if (verdict.label === "simple" && Number.isFinite(confidence) && confidence >= SIMPLE_CONFIDENCE) {
          output.parts.push({
            type: "text",
            synthetic: true,
            text: `[subcortex] simple request (confidence ${confidence.toFixed(2)}) — prefer the most direct path`,
          })
        }
      } catch {
        // fail open: never block or corrupt the user message
      }
    },

    // Fires after a tool runs; output.output is exactly what the LLM sees next.
    "tool.execute.after": async (input, output) => {
      try {
        if (typeof output.output !== "string") return
        const original = output.output
        if (original.length < MIN_OUTPUT_CHARS) return
        if (looksLikeError(original)) return // errors stay verbatim for debugging
        const context = `${String(input.tool ?? "tool")}: ${argsSnippet(input.args)}`
        const verdict = await postVerdict("/verdict/output", { output: original, context })
        if (!verdict) return
        const pNeeded = Number(verdict.p_needed)
        const disposable = Number.isFinite(pNeeded) ? pNeeded < P_NEEDED_THRESHOLD : verdict.needed === false
        if (!disposable) return
        const truncated = original.length - HEAD_CHARS - TAIL_CHARS
        output.output =
          original.slice(0, HEAD_CHARS) +
          `\n\n[subcortex: truncated ${truncated} chars of low-value output; re-run the tool if you need the rest]\n\n` +
          original.slice(-TAIL_CHARS)
      } catch {
        // fail open: the full tool output passes through untouched
      }
    },

    // Runs when the session context is compacted; just leaves a breadcrumb.
    "experimental.session.compacting": async (input, output) => {
      try {
        output.context?.push(
          `[subcortex] verdict/truncation stats: GET ${SUBCORTEX_URL}/stats (or run: subcortex stats)`,
        )
      } catch {
        // fail open
      }
    },
  }
}

export default Subcortex
