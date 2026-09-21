# subcortex

A local decision layer for coding-agent TUIs. A small daemon keeps a
typed-decision model warm and answers System-1 questions — *is this request
simple? is this tool output still needed for it?* — in milliseconds, so the big
model doesn't spend tokens on them. Hooks, plugins and MCP wire it into 29
terminal coding agents.

Two decision backends:

- **jev** — hosted typed-decision API (TypeSafe `jev-latest`, or any endpoint
  speaking the same wire shape: OpenRouter, Vercel AI Gateway). ~250 ms per
  decision over a kept-alive connection, $0.042 per million input tokens.
- **laya** — local model ([laya-mlx](https://pypi.org/project/laya-mlx/) on
  Apple Silicon, [laya](https://pypi.org/project/laya/) elsewhere). Private,
  offline, ~10 ms per question. Weaker judgments, so it acts more cautiously
  (see [How good are the decisions?](#how-good-are-the-decisions)).

## What it does inside a TUI

1. **Prompt hint** — a request the model rates *simple* gets a one-line,
   factual note ("the most direct, minimal change is likely sufficient").
2. **Output trimming** — a large, successful shell output that the model
   judges *not needed for the user's current request* is cut to its head and
   tail; lines in the middle that mention the request or warn about something
   are kept, and anything that looks like a failure is never touched. The
   user's latest request is the evidence: without one, nothing is trimmed.
   Laya doesn't trim unless you opt in.
3. **Compaction snapshot** — before context compaction, the last few messages
   are saved to disk (never vetoing anything).
4. **Compaction restore** — after compaction they are handed back as context,
   exactly once, and only deleted after they were delivered.

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
| Grok Build | hooks | — output discarded | ✅ tagged `updatedToolOutput` | ✅ restored on next shell call |
| Docker Agent ≥ 1.137 | hooks | ✅ | ✅ `updated_tool_response` | ✅ restored with next prompt |
| Mistral Vibe ≥ 2.25.5 | hooks | — no event | ✅ `post_tool` reason | — no event |
| Letta Code | hooks | ✅ | — | — |
| Junie CLI (EAP) | hooks | ✅ | — | — |
| Devin CLI | hooks | ✅ | — | — |
| OpenCode ≥ 1.1.62 | plugin | ✅ | ✅ bash only | ✅ into the compaction prompt |
| Kilo Code CLI | plugin | ✅ | ✅ | ✅ |
| Amp | plugin | ✅ | ✅ | ✅ rolling snapshot |
| Pi ≥ 0.87 | plugin | ✅ | ✅ | ✅ re-inserted after the summary |
| Cline CLI ≥ 3.0.62 | plugin | ✅ | ✅ | ✅ on the next request |
| Crush, Goose, Warp, Zed, Kiro, Auggie | MCP | on-demand tools | — | — |
| Aider | none | — | `subcortex wrap` for test/lint | — |

"—" means the TUI has no seam that can do it without blocking or failing a
tool call, so subcortex doesn't try. Per-TUI details, config paths and caveats:
[docs/tuis.md](docs/tuis.md).

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/pavlealeksic/subcortex/main/install.sh | sh
```

That installs the CLI into its own environment (uv, pipx or a private venv)
and starts **`subcortex setup`**, an interactive walk-through:

1. **Backend** — Laya (local; installs `laya-mlx`/`laya` into a dedicated
   environment and downloads the model) or Jev (hosted; your API key is kept in
   the environment or in a mode-600 file).
2. **Behaviors** — prompt hints, output trimming, compaction snapshots.
3. **TUIs** — a checklist of every supported TUI, with the ones found on this
   machine preselected.
4. **Review** — each file that will change, with its diff on request; every
   install self-tests its hook commands before writing.
5. **Daemon** — start it now, and optionally at login (launchd / systemd).

Arrow keys and space in a terminal; plain numbered prompts elsewhere. Run it
again any time to change your choices; `subcortex setup --yes --backend jev
--tuis detected` runs it unattended. Or install by hand:

```sh
pip install subcortex            # daemon + jev backend (zero dependencies)
pip install "subcortex[laya]"    # + local laya backend
```

## Wire it into a TUI (without the wizard)

```sh
subcortex install                         # checklist of TUIs (in a terminal)
subcortex tuis                            # supported TUIs, what's installed, what's on PATH
subcortex install claude-code --dry-run   # show exactly what would change
subcortex install claude-code             # shows the diff, asks, self-tests, writes
subcortex install codex gemini-cli --yes  # several at once, no prompt
subcortex install detected                # every supported TUI found on PATH
subcortex install cursor --mcp            # also register the on-demand MCP server
subcortex uninstall claude-code           # removes exactly what subcortex added
```

## How good are the decisions?

A probability is not an accuracy, so every rule was chosen on labeled
examples and then checked on examples it had never seen
([docs/calibration.md](docs/calibration.md)). The mistakes that matter are a
"simple" hint on complex work and a trim of output the request needs; there
were none:

| | hints: simple requests | hints: complex requests | trims: disposable output | trims: needed output |
|---|---|---|---|---|
| **jev** (calibration / held out) | 17/20 · 12/12 | **0/20 · 0/12** | 12/12 · 6/6 | **0/12 · 0/6** |
| **laya** (calibration / held out) | 5/20 · 5/12 | **0/20 · 0/12** | off by default¹ | — |

¹ Laya's output judgments trimmed 2 of 6 needed outputs on the held-out set, so
Laya only trims if you set `thresholds.output_needed_threshold` yourself.

```sh
subcortex eval                  # the same check against your backend and thresholds
```

It exits non-zero on any hinted complex request or trimmed needed output.

## Safety

A hook that misbehaves can take a TUI down: an earlier version of this project
exited 2 from a Claude Code hook, and exit 2 means "block this prompt". People
run this inside real work, so every one of these is enforced by tests:

- **Hooks always exit 0 and never stall a turn.** One hardened entry point
  catches everything; a watchdog ends it silently when its budget (4 s) is
  spent — including a C-level backstop for work that holds the interpreter
  lock; stray prints never reach stdout; nothing is written to stderr.
- **Blocking responses can't be emitted.** A guard drops `decision: block/deny`,
  `continue: false`, permission denials and the like — except where a TUI's
  spec uses one as a non-blocking replacement, for that event only.
- **Your environment can't break it.** Hooks run as
  `'<python>' -I -m subcortex.hook <tui> <event> 2>/dev/null || true`:
  isolated from `PYTHONPATH` and from the project directory (a repo's own
  `json.py` can't run inside a hook or the daemon), and a missing
  installation stays harmless.
- **Nothing is sent to a proxy.** Hooks and plugins reach the daemon over a
  direct socket to 127.0.0.1, whatever `HTTP_PROXY` says.
- **Many sessions at once are fine.** Per-session state is keyed by TUI and
  session, written atomically, kept private (0600/0700) and changed under a
  lock; a compaction snapshot is restored exactly once and deleted only after
  delivery. The daemon answers bursts (backlog 256), sheds load it can't serve
  in time (503, the hook passes through), and serializes the local model.
- **Only you can use the daemon.** Every request carries a per-user token from
  a 0600 file; requests from web pages (Origin, foreign Host, non-JSON) are
  refused.
- **Installs are self-tested.** Before writing anything, the installer runs the
  exact commands it's about to write — with the daemon down, against a stub
  daemon, and with the executable missing — and refuses to write if any run
  exits non-zero, writes to stderr, emits a blocking response, runs over
  budget, or stays silent where it must answer.
- **Config edits are conservative.** A diff is shown and confirmed first;
  writes are atomic with timestamped backups; a symlinked (dotfiles) config
  stays a symlink; a file the TUI changed during install is re-merged, not
  overwritten; files that aren't plain JSON / valid TOML are refused; only
  subcortex's own entries are ever removed.
- **Other TUIs' hooks are respected.** Cursor, Droid, Grok Build, Devin and
  Cortex Code also execute hooks from `~/.claude/settings.json`; the Claude
  Code adapter recognises foreign payloads and stays silent.

## Privacy

- **laya** keeps everything on your machine.
- **jev** receives, per decision, your latest request, the tool call and a
  ~1.5 KB head+tail excerpt of a large output — capped, and with anything that
  looks like a secret (API keys, tokens, passwords, private keys, credentials
  in URLs) masked first.
- Local state (remembered requests, compaction snapshots) is private to your
  user and pruned automatically; the ledger behind `subcortex stats` holds
  counts only, never content.

## Daemon & settings

```sh
subcortex doctor                 # check config, backend, daemon, installed hooks
subcortex config                 # show every setting (config edit: interactive)
subcortex config set thresholds.min_output_chars 8000
subcortex service install        # start the daemon at login (launchd / systemd --user)
subcortex serve                  # start the background daemon (127.0.0.1:7707)
subcortex stats                  # hints, trims (tokens saved), restores and jev cost, per TUI
subcortex eval                   # labeled examples through your backend and thresholds
subcortex serve --stop
subcortex decide --state "Refactor the parser to be iterative" \
    --questions '{"hard": {"type": "noul", "instructions": "Is `prompt` hard?"}}'
```

HTTP surface (all on `127.0.0.1:<port>`; every request but `/health` carries
the per-user token from `~/.local/share/subcortex/token`):

- `POST /decide` `{state, questions}` → `{success, answers, model?, usage?}`
- `POST /verdict/prompt` `{prompt}` → `{success, verdict: {label, confidence, signals}}`
- `POST /verdict/output` `{output, context, task}` → `{success, verdict: {needed, p_needed, signals}}`
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
  "thresholds": {"prompt_simple_confidence": null, "output_needed_threshold": null, "min_output_chars": 6000},
  "hooks": {"autostart_daemon": true, "budget_s": 4.0, "http_timeout_s": 3.0, "head_chars": 1000, "tail_chars": 500,
            "snapshot_messages": 5, "snapshot_chars": 500}
}
```

`null` thresholds use the rule calibrated for the active backend; a number
replaces that rule's primary threshold (check the effect with `subcortex eval`).
The daemon picks up config changes without a restart.

Hooks start the daemon themselves when they find it down (at most once a
minute; set `hooks.autostart_daemon` to `false` or `SUBCORTEX_AUTOSTART=0` to
turn that off) — the hook that notices still passes through untouched.
`subcortex doctor` also checks every installed integration's hook executable.

Runtime state (daemon log/PID/token, compaction snapshots, the stats ledger, `hooks.log`) lives in
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
SUBCORTEX_E2E=1 .venv/bin/python -m unittest discover -s tests -p "test_e2e*.py"   # real TUIs, mock LLM API
```

The unit suite includes concurrency proofs (racing threads and processes,
bursts against the daemon), watchdog and proxy-immunity checks, and runs every
TS plugin under Bun. The end-to-end suites drive real TUI binaries against a
local fake LLM API (`tests/e2e/`) — no network, no cost — and assert on what
each TUI actually sent to the model. They never touch your real TUI configs.

Releases publish to PyPI via Trusted Publishing (`.github/workflows/publish.yml`):
bump `version` in `pyproject.toml`, then `gh release create v<X.Y.Z> --generate-notes`.
