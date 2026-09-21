# subcortex

A local decision layer for coding-agent TUIs. A small daemon keeps a
typed-decision model warm and answers System-1 questions — *is this prompt
simple? is this tool output disposable?* — in milliseconds, so the big model
doesn't spend tokens on them. Hooks and plugins wire it into 19 terminal
agents.

Two decision backends:

- **laya** — local model ([laya-mlx](https://pypi.org/project/laya-mlx/) on
  Apple Silicon, [laya](https://pypi.org/project/laya/) elsewhere). Private,
  offline, millisecond latency.
- **jev** — hosted typed-decision API (TypeSafe `jev-latest`, or any endpoint
  speaking the same wire shape, e.g. OpenRouter).

## What it does inside a TUI

1. **Prompt hint** — a prompt the local model rates *simple* gets a one-line,
   factual note ("the most direct, minimal change is likely sufficient").
2. **Output trimming** — a large, successful, *disposable* shell output is
   replaced by head + marker + tail. Anything that looks like an error is
   never touched.
3. **Compaction snapshot** — before context compaction, the last few messages
   are saved to disk (never vetoing anything).
4. **Compaction restore** — after compaction they are handed back as context.

Each TUI gets whichever of these its extension seam can safely support:

| TUI | Seam | Hint | Trim | Compaction |
|---|---|---|---|---|
| Claude Code | hooks | ✅ | ✅ `updatedToolOutput` | ✅ PreCompact → SessionStart |
| Codex CLI ≥ 0.133 | hooks (`/hooks` trust) | ✅ | ✅ `continue:false` + `reason` | ✅ |
| Open Interpreter | hooks (Codex engine) | ✅ | ✅ | ✅ |
| Qoder CLI | hooks | ✅ | ✅ | ✅ |
| CodeBuddy Code | hooks | ✅ | ✅ | ✅ |
| GitHub Copilot CLI ≥ 1.0.67 | hooks | ✅ | ✅ `modifiedResult` | ✅ restored with next prompt |
| Factory Droid | hooks | ✅ | — can only append | ✅ |
| Qwen Code ≥ 0.16 | hooks | ✅ | — truncates natively | ✅ |
| Gemini CLI ≥ 0.27 | hooks | ✅ | — truncates natively | ◐ on `/compress` |
| Cursor CLI | hooks | ✅ interactive | — Shell can't be replaced | ✅ restored with next prompt |
| Kimi Code CLI ≥ 0.33 | hooks | ✅ | — output hook ignored | ✅ restored with next prompt |
| OpenHands CLI ≥ 1.12 | hooks | ✅ | — truncates natively | ✅ via its event log |
| Junie CLI (EAP) | hooks | ✅ | — | — |
| Devin CLI | hooks | ✅ | — | — |
| OpenCode ≥ 1.1.62 | plugin | ✅ | ✅ bash only | ✅ into the compaction prompt |
| Kilo Code CLI | plugin | ✅ | ✅ | ✅ |
| Amp | plugin | ✅ | ✅ | ✅ rolling snapshot |
| Crush | MCP | on-demand tools | — truncates natively | — |
| Goose | MCP | on-demand tools | — truncates natively | — |
| Aider | none | — | `subcortex wrap` for test/lint | — |

"—" means the TUI has no seam that can do it without blocking or failing a
tool call, so subcortex doesn't try. Per-TUI details, config paths and caveats:
[docs/tuis.md](docs/tuis.md).

## Install

```sh
pip install subcortex            # daemon + jev backend (zero dependencies)
pip install "subcortex[laya]"    # + local laya backend
```

## Wire it into a TUI

```sh
subcortex tuis                            # supported TUIs, what's installed, what's on PATH
subcortex install claude-code --dry-run   # show exactly what would change
subcortex install claude-code             # shows the diff, asks, self-tests, writes
subcortex install codex gemini-cli --yes  # several at once, no prompt
subcortex install cursor --mcp            # also register the on-demand MCP server
subcortex uninstall claude-code           # removes exactly what subcortex added
```

## Safety

A hook that misbehaves can take a TUI down: an earlier version of this project
exited 2 from a Claude Code hook, and exit 2 means "block this prompt". So:

- **Hooks always exit 0.** One hardened entry point (`subcortex-hook`) catches
  everything, including bad arguments, invalid JSON and interrupts; a watchdog
  ends it silently once its time budget (4 s) is spent; stray prints never
  reach stdout; nothing is written to stderr.
- **Blocking responses can't be emitted.** Every response passes a guard that
  drops `decision: block/deny`, `continue: false`, permission denials and the
  like — except where a TUI's spec uses one as a non-blocking replacement
  (Codex's `continue: false` on PostToolUse), allowed for that event only.
- **A dangling hook stays harmless.** Every command is written as
  `'<abs path>/subcortex-hook' <tui> <event> 2>/dev/null || true`, so an
  uninstalled package or deleted venv can't make a TUI block.
- **Installs are self-tested.** Before writing anything, the installer runs the
  exact commands it's about to write — with the daemon down, against a stub
  daemon, and with the executable missing — and refuses to write if any run
  exits non-zero, writes to stderr, emits a blocking response, runs over
  budget, or stays silent where it must answer.
- **Config edits are conservative.** A diff is shown and confirmed first;
  writes are atomic with timestamped backups; files that aren't plain JSON /
  valid TOML are refused, never rewritten; subcortex's entries live in their
  own groups (or files) and uninstall removes only those — including entries
  written by 0.1.0.
- **Other TUIs' hooks are respected.** Cursor, Droid, Grok Build, Devin and
  Cortex Code also execute hooks from `~/.claude/settings.json`; the Claude
  Code adapter recognises foreign payloads and stays silent.

## Daemon

```sh
subcortex doctor                 # check config, backend, daemon
subcortex serve                  # start the background daemon (127.0.0.1:7707)
subcortex stats                  # counters + uptime
subcortex serve --stop
subcortex decide --state "Refactor the parser to be iterative" \
    --questions '{"hard": {"type": "noul", "instructions": "Is `prompt` hard?"}}'
```

HTTP surface (all on `127.0.0.1:<port>`):

- `POST /decide` `{state, questions}` → `{success, answers|error}`
- `POST /verdict/prompt` `{prompt}` → `{success, verdict: {label, confidence}}`
- `POST /verdict/output` `{output, context}` → `{success, verdict: {needed, p_needed}}`
- `POST /v1/prompt-hint`, `/v1/tool-output`, `/v1/snapshot`, `/v1/restore` —
  the four behaviors, for plugins (see [docs/adding-a-tui.md](docs/adding-a-tui.md))
- `GET /health`, `GET /stats`

## Configuration

`~/.config/subcortex/config.json` (`SUBCORTEX_CONFIG` overrides the path;
`SUBCORTEX_*` env vars override keys):

```json
{
  "backend": "laya",
  "port": 7707,
  "features": {"prompt_hint": true, "trim_output": true, "compaction_snapshot": true},
  "thresholds": {"prompt_simple_confidence": 0.8, "output_needed_threshold": 0.3, "min_output_chars": 6000},
  "hooks": {"autostart_daemon": true, "budget_s": 4.0, "http_timeout_s": 3.0, "head_chars": 1000, "tail_chars": 500,
            "snapshot_messages": 5, "snapshot_chars": 500}
}
```

Hooks start the daemon themselves when they find it down (at most once a
minute; set `hooks.autostart_daemon` to `false` or `SUBCORTEX_AUTOSTART=0` to
turn that off) — the hook that notices still passes through untouched.
`subcortex doctor` also checks every installed integration's hook executable.

Runtime state (daemon log/PID, compaction snapshots, `hooks.log`) lives in
`~/.local/share/subcortex/` (`SUBCORTEX_DATA_DIR`). Set `SUBCORTEX_DEBUG=1` to
log every hook invocation to `hooks.log`.

## Any other TUI

```sh
subcortex mcp     # stdio MCP server: subcortex_decide / _classify_prompt / _judge_output
subcortex wrap -- <command>   # run a command, trim its disposable output, keep its exit code
```

MCP tools are model-invoked (the agent spends tokens calling them), so hooks
or plugins are preferred wherever a TUI has them. Adding a native adapter:
[docs/adding-a-tui.md](docs/adding-a-tui.md).

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests   # no model or API key needed; bun runs the plugin tests
SUBCORTEX_E2E=1 .venv/bin/python -m unittest discover -s tests -p test_e2e.py   # real TUIs, mock LLM API
```

The end-to-end suite drives the real Claude Code, Kimi Code and OpenCode
binaries (whichever are installed) against a local fake LLM API
(`tests/e2e/mock_llm.py`) — no network, no cost — and asserts on what each TUI
actually sent to the model. It never touches your real TUI configs.

Releases publish to PyPI via Trusted Publishing (`.github/workflows/publish.yml`):
bump `version` in `pyproject.toml`, then `gh release create v<X.Y.Z> --generate-notes`.
