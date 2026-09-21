# subcortex

A local decision layer for coding-agent TUIs. A small HTTP daemon keeps a
typed-decision backend warm and answers System-1 questions — prompt complexity,
tool-output disposability — in milliseconds, instead of burning a full
frontier-model turn on them.

Two backends:

- **laya** — local model ([laya-mlx](https://pypi.org/project/laya-mlx/) on
  Apple Silicon, [laya](https://pypi.org/project/laya/) elsewhere). Private,
  offline, millisecond latency.
- **jev** — hosted typed-decision API (TypeSafe `jev-latest`, or any endpoint
  speaking the same wire shape, e.g. OpenRouter).

## Install

```sh
pip install subcortex            # daemon + jev backend (zero deps)
pip install "subcortex[laya]"    # + local laya backend
```

## Quickstart

```sh
subcortex doctor                 # check config, backend, daemon
subcortex serve                  # start the background daemon (127.0.0.1:7707)
subcortex decide --state "Refactor the parser to be iterative" \
    --questions '{"hard": {"type": "noul", "instructions": "Is `prompt` hard?"}}'
subcortex stats                  # counters + uptime
subcortex serve --stop
```

HTTP surface (all on `127.0.0.1:<port>`):

- `POST /decide` `{state, questions}` → `{success, answers|error}`
- `POST /verdict/prompt` `{prompt}` → `{success, verdict: {label, confidence}}`
- `POST /verdict/output` `{output, context}` → `{success, verdict: {needed, p_needed}}`
- `GET /health` → `{ok, backend, model}`
- `GET /stats` → counters + uptime

Config lives at `~/.config/subcortex/config.json` (see `subcortex/config.py`
for defaults); `SUBCORTEX_*` env vars override. Daemon logs, PID and lock live
in `~/.local/share/subcortex/`.

## TUI integration

```sh
subcortex install --tui claude-code --backend laya   # or: codex, opencode; --backend jev
subcortex uninstall --tui claude-code
```

| Feature | Claude Code | Codex CLI | OpenCode |
|---|---|---|---|
| Simple-prompt hint | `UserPromptSubmit` → context | `UserPromptSubmit` → context | `chat.message` → synthetic part |
| Disposable-output truncation | `PostToolUse` → `updatedToolOutput` | `PostToolUse` → replaced result | `tool.execute.after` → replaced output |
| Compaction guard | `PreCompact` snapshot + `SessionStart(compact)` re-inject | same pattern | `experimental.session.compacting` note |

Every hook fails open: if the daemon is down or slow, the TUI proceeds untouched.

Per-TUI details: [docs/claude-code.md](docs/claude-code.md),
[docs/codex.md](docs/codex.md), [docs/opencode.md](docs/opencode.md).
Codex note: trust the hooks once with `/hooks` after install.

## Any other TUI (MCP)

```sh
subcortex mcp   # stdio MCP server: subcortex_decide / classify_prompt / judge_output
```

Register it as an MCP server in any MCP-capable TUI (Gemini CLI, Crush, Amp, …).
Writing a native hook adapter for a new TUI: [docs/adding-a-tui.md](docs/adding-a-tui.md).

## Development

```sh
python3 -m unittest discover -s tests     # 117 tests, no model/API key needed
```

Releases publish to PyPI via Trusted Publishing (`.github/workflows/publish.yml`):
bump `version` in `pyproject.toml`, then `gh release create v<X.Y.Z> --generate-notes`.

