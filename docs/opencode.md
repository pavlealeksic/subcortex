# OpenCode adapter

`adapters/opencode/subcortex.ts` is an [OpenCode](https://opencode.ai/docs/plugins)
plugin that routes prompts and tool outputs through the local subcortex daemon
and mutates hook outputs based on its verdicts.

## What it does, per hook

- **`chat.message`** — fires before the user message is persisted and the LLM
  loop starts. The plugin extracts the text parts of the message, classifies
  them via `POST /verdict/prompt`, and — when the verdict is `simple` with
  confidence ≥ 0.8 — appends a synthetic text part
  (`synthetic: true`) telling the model to prefer the most direct path.
  Mutations to `output.parts` are persisted and seen by the model; that is the
  mechanism.

- **`tool.execute.after`** — fires after a tool runs; `output.output` is exactly
  what the LLM sees next. String outputs of ≥ 6000 chars that do not look like
  an error (no `error` / `traceback` / `failed` in the first 2000 chars,
  case-insensitive) are judged via `POST /verdict/output`, with the tool name
  plus the first 200 chars of the args JSON as context. If the output is
  disposable (`p_needed < 0.3`) it is replaced with a 1000-char head, a
  truncation note, and a 500-char tail:
  `[subcortex: truncated N chars of low-value output; re-run the tool if you need the rest]`.
  Errors are always left verbatim — they are needed for debugging.

- **`experimental.session.compacting`** — appends one line to the compaction
  context pointing at the daemon stats endpoint (`GET /stats`, or
  `subcortex stats`).

## Install

Once the CLI wiring lands:

```
subcortex install --tui opencode
```

which runs `subcortex.installers.opencode.install()`. Manual install is just a
file copy — OpenCode loads any `*.ts` in its global plugin directory:

```
mkdir -p ~/.config/opencode/plugins
cp adapters/opencode/subcortex.ts ~/.config/opencode/plugins/subcortex.ts
```

The installer also supports `uninstall()` (removes only
`~/.config/opencode/plugins/subcortex.ts`) and `status()` (reports whether the
file is present and identical to the bundled copy). `install()` backs up a
pre-existing, differing `subcortex.ts` to `subcortex.ts.bak` before overwriting.

## Configuration

- `SUBCORTEX_URL` — daemon base URL, default `http://127.0.0.1:7707`.
- Thresholds (`SIMPLE_CONFIDENCE` 0.8, `MIN_OUTPUT_CHARS` 6000,
  `P_NEEDED_THRESHOLD` 0.3, `REQUEST_TIMEOUT_MS` 3000) are constants at the top
  of the TS file, mirroring the `thresholds.*` defaults in
  `~/.config/subcortex/config.json`. Edit the file to change them; OpenCode
  reloads plugins on restart.

## Runtime notes

OpenCode plugins run on **Bun**, so plain global `fetch` is available and there
is no Node/npm install step. Hooks are **awaited sequentially** — the LLM loop
does not continue until `chat.message` and `tool.execute.after` resolve. That
is why every daemon request carries a hard 3-second `AbortController` timeout:
a slow or missing daemon must never stall a turn.

## Fail-open behavior

The plugin never throws. Daemon unreachable, timeout, non-200, malformed JSON,
or an explicit `{"success": false}` all result in the hook returning with the
output untouched. The daemon wraps verdicts in
`{"success": true, "verdict": {...}}`; the plugin unwraps `.verdict` when
present and also accepts a flat body, so it keeps working if the envelope
changes. In the worst case OpenCode behaves exactly as if the plugin were not
installed.
