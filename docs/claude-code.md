# Claude Code adapter

The Claude Code adapter wires subcortex into [Claude Code](https://docs.anthropic.com/en/docs/claude-code) hooks. Claude Code runs `subcortex hook claude <Event>` at four lifecycle points; the adapter reads the hook JSON from stdin, consults the local daemon, and prints a response JSON to stdout. **It always fails open**: if the daemon is down, slow, or the verdict fails, the hook exits 0 with no output and Claude Code behaves exactly as if subcortex were not installed.

## What it does per event

| Event | Matcher | Behavior |
|---|---|---|
| `UserPromptSubmit` | — | Classifies your prompt via `POST /verdict/prompt`. When the local model rates it **simple** with confidence ≥ `prompt_simple_confidence` (default 0.8), it injects `additionalContext` nudging Claude to take the most direct, minimal path. Complex prompts pass through silently. |
| `PostToolUse` | `Bash` | When Bash output is ≥ `min_output_chars` (default 6000) and looks successful (no nonzero exit, no `Traceback`/`error` markers, not interrupted), it asks `POST /verdict/output` whether the output is still needed. Disposable output (`p_needed` < `output_needed_threshold`, default 0.3) is replaced with head (1000 chars) + a truncation marker + tail (500 chars), preserving the Bash output schema (`stdout`/`stderr`/`interrupted`/`isImage`). Errors and failures always pass through untouched. |
| `PreCompact` | `auto\|manual` | Before context compaction, writes a snapshot to `~/.local/share/subcortex/compact/<session_id>.json` with the last 5 non-empty user/assistant message texts (≤500 chars each) pulled from the transcript JSONL. Never blocks. |
| `SessionStart` | `compact` | After a compaction restart, re-injects the snapshot as `additionalContext` ("Pre-compaction context: …") and deletes it. |

## Install

Once the CLI wiring lands:

```
subcortex install --tui claude-code
```

Until then, add this to `~/.claude/settings.json` manually (merge with your existing `hooks`; the installer does exactly this, with a backup):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {"hooks": [{"type": "command", "command": "subcortex hook claude UserPromptSubmit", "timeout": 10}]}
    ],
    "PostToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "subcortex hook claude PostToolUse", "timeout": 10}]}
    ],
    "PreCompact": [
      {"matcher": "auto|manual", "hooks": [{"type": "command", "command": "subcortex hook claude PreCompact", "timeout": 10}]}
    ],
    "SessionStart": [
      {"matcher": "compact", "hooks": [{"type": "command", "command": "subcortex hook claude SessionStart", "timeout": 10}]}
    ]
  }
}
```

The installer resolves `subcortex` via `PATH` and falls back to `<python> -m subcortex`. It is idempotent (re-running skips entries already present), backs up the settings file to `settings.json.subcortex.bak`, and `uninstall()` removes only subcortex entries — your other hooks and settings keys are never touched.

The daemon must be running for verdicts: `subcortex serve`.

## Tuning

All knobs live in `~/.config/subcortex/config.json` under `thresholds`:

- `prompt_simple_confidence` (0.8) — raise to inject the "keep it simple" nudge less often.
- `min_output_chars` (6000) — Bash outputs shorter than this are never judged or truncated.
- `output_needed_threshold` (0.3) — raise to truncate more aggressively, lower to keep more.

Hook timeouts are set to 10s in the settings entries; the adapter's own HTTP calls time out at 5s.

## Failure mode

Fail-open, everywhere. Daemon unreachable, verdict error, malformed payload, unreadable transcript — every path exits 0 with no stdout, so Claude Code never blocks and no output is ever modified unless a verdict explicitly says it is disposable. The only side effect that runs without the daemon is the `PreCompact` snapshot, which is local file I/O.
