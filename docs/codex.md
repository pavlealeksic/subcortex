# Codex CLI adapter

subcortex hooks into [Codex CLI](https://github.com/openai/codex) via its hook
system (`~/.codex/config.toml`). Every hook command is
`subcortex hook codex <event>`: the event payload arrives as JSON on stdin and
any response is JSON on stdout. All hooks fail open — if the daemon is down,
times out (5s), or anything raises, the hook prints nothing and exits 0, so
Codex behaves exactly as if subcortex were not installed.

## What it does per event

- **UserPromptSubmit** — the prompt is classified via `POST /verdict/prompt`.
  If the local model rates it `simple` with confidence ≥
  `thresholds.prompt_simple_confidence` (default 0.8), a hint is injected as
  `additionalContext`:

  > `[subcortex] Local decision model rates this request as simple (confidence X). Prefer the most direct, minimal path.`

  Complex or uncertain prompts get no output.

- **PostToolUse** (matcher `Bash`) — tool output of at least
  `thresholds.min_output_chars` (default 6000) that looks successful is judged
  via `POST /verdict/output`. If it is disposable (`p_needed` below
  `thresholds.output_needed_threshold`, default 0.3), the result the model sees
  is replaced (`{"decision": "block", "reason": ...}`) with the first 1000
  chars, a marker noting how many chars were truncated, and the last 500 chars.
  The command's side effects have already happened; only the context payload
  shrinks. Outputs containing tracebacks or non-zero exit codes pass through
  untouched — errors are high-value.

- **PreCompact** — never vetoes. The last ~5 non-empty message texts from
  `transcript_path` (jsonl; missing/corrupt tolerated) are written to
  `~/.local/share/subcortex/compact/<session_id>.json`.

- **SessionStart** (matcher `compact`) — if a snapshot exists for the session,
  it is re-injected as `additionalContext` and the file is deleted. Other
  sources (`startup`, `resume`, `clear`) are ignored.

## Install

```
subcortex install --tui codex
```

(once the CLI subcommand is wired up). This appends a marked block
(`# >>> subcortex` … `# <<< subcortex`) to `~/.codex/config.toml`, backing the
original up to `config.toml.subcortex.bak`. Everything else in the file is
preserved, the merged file is validated as TOML before writing, and running
install twice is a no-op.

**Trust step (required):** Codex CLI does not run new hooks until you trust
them — hook definitions are hashed, so editing `config.toml` re-triggers the
prompt. After installing, open the Codex TUI and run `/hooks` to trust the
subcortex hooks. The installer prints this reminder too.

### Manual config

Equivalent snippet for `~/.codex/config.toml` (adjust the command path):

```toml
# >>> subcortex
[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = "/path/to/subcortex hook codex UserPromptSubmit"
timeout = 10

[[hooks.PostToolUse]]
matcher = "Bash"

[[hooks.PostToolUse.hooks]]
type = "command"
command = "/path/to/subcortex hook codex PostToolUse"
timeout = 10

[[hooks.PreCompact.hooks]]
type = "command"
command = "/path/to/subcortex hook codex PreCompact"
timeout = 10

[[hooks.SessionStart]]
matcher = "compact"

[[hooks.SessionStart.hooks]]
type = "command"
command = "/path/to/subcortex hook codex SessionStart"
timeout = 10
# <<< subcortex
```

If `subcortex` is not on PATH the installer falls back to
`<python> -m subcortex hook codex <event>`.

## Uninstall

```
subcortex uninstall --tui codex
```

Removes exactly the marked block (another backup is made first) and leaves the
rest of `config.toml` untouched.

## Fail-open contract

The daemon must be running (`subcortex serve`) for verdicts; when it is not,
hooks simply produce no output and exit 0. Codex never blocks, errors, or
changes behavior because of subcortex. Hook `timeout = 10` seconds is set in
the config so even a wedged hook process cannot stall a turn for long.
