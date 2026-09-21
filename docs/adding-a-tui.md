# Wiring another TUI to subcortex

subcortex is TUI-agnostic: a local daemon answers typed decisions over HTTP, and each
TUI gets a thin adapter translating its hook payloads into verdicts. Claude Code,
Codex CLI, and OpenCode ship built-in; this page is for everything else
(Gemini CLI, Crush, Amp, your own agent…).

## The daemon contract

```
POST http://127.0.0.1:7707/verdict/prompt   {"prompt": "..."}
  -> {"label": "simple"|"complex", "confidence": 0.0-1.0}

POST http://127.0.0.1:7707/verdict/output   {"output": "...", "context": "..."}
  -> {"needed": true|false, "p_needed": 0.0-1.0}

POST http://127.0.0.1:7707/decide           {"state": ..., "questions": {...}}
  -> {"success": true, "answers": {...}}      # full typed decisions (choice/score/noul)

GET  http://127.0.0.1:7707/health           -> {"ok": true, "backend": "laya", ...}
GET  http://127.0.0.1:7707/stats            -> usage counters
```

Start it with `subcortex serve` (auto-started by `subcortex decide` too).

## What a hook adapter does

1. **Read** the TUI's hook payload (usually JSON on stdin for command hooks).
2. **Map** the event:
   - prompt-submitted → `/verdict/prompt`; if `simple` with high confidence, ask the TUI
     to inject a short hint into the model's context (most TUIs support this).
   - tool-finished with large successful output → `/verdict/output`; if disposable
     (`p_needed < 0.3`), replace what the model sees with head + marker + tail.
   - pre-compaction → snapshot recent context to disk; post-compaction/session-resume →
     re-inject it.
3. **Emit** the TUI's response contract (stdout JSON, exit codes, or native API calls).
4. **Fail open**: any error, timeout, or unreachable daemon → emit nothing, exit 0.
   The TUI must never notice subcortex was there. Keep hook latency under ~5s.

## The MCP fallback

If the TUI supports MCP but has no hooks (or you just want on-demand decisions),
register the stdio MCP server:

```
command: subcortex mcp
```

It exposes `subcortex_decide`, `subcortex_classify_prompt`, and
`subcortex_judge_output`. Note MCP tools are *model-invoked* — the agent pays tokens
to call them — so prefer hook-based interception where the TUI offers it.

## Contributing an adapter

Adapters live in `src/subcortex/adapters/<tui>.py` (Python, for command-style hooks)
or `adapters/<tui>/` (for plugin-file TUIs like OpenCode), plus an installer in
`src/subcortex/installers/<tui>.py` and contract tests with recorded hook payloads
in `tests/`. See the existing adapters as templates.
