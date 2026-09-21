# Adding a TUI

subcortex separates three concerns, and a new TUI only touches the last two:

```
src/subcortex/
  policy.py        the four behaviors (hint / trim / snapshot / restore) — TUI-agnostic
  hook.py          the process contract every command hook runs under
  adapters/        per-TUI translation: payload in → policy → response out
  installers/      per-TUI config edits, self-tested before anything is written
  plugins/<tui>/   JS/TS plugins for TUIs without command hooks
```

## 1. Learn the contract first

Before writing code, pin down from the TUI's docs **and source** (docs are
often stale):

- which events exist for prompt-submit, after-tool, pre-compaction and
  post-compaction/session-start, and their exact stdin fields;
- the response fields that inject context and that *replace* tool output —
  and whether "replace" is really a replacement or an error/blocked result;
- **every exit code and field value that blocks, denies or stops the agent**
  (exit 2 is common; some TUIs block on any status ≥ 2, some on `continue: false`);
- the config file, its format and validation (does one bad entry disable the
  whole file?), env vars that relocate it, and any trust/approval step.

If a behavior would require a blocking or error-shaped response, don't do it.

## 2. The adapter

Subclass `HookAdapter` (or `ClaudeStyleAdapter` for Claude-shaped TUIs) in
`src/subcortex/adapters/`:

```python
class MyTuiAdapter(HookAdapter):
    name = "my-tui"
    display_name = "My TUI"
    events = {"PromptSubmitted": PROMPT, "BeforeCompact": PRE_COMPACT}
    restore_on_prompt = True          # no post-compaction hook that can inject

    def parse(self, name, kind, payload):   # only if fields differ from Claude's
        event = super().parse(name, kind, payload)
        event.session_id = str(payload.get("conversationId") or "")
        return event

    def render_prompt(self, event, text):
        return {"additionalContext": text}
```

- `parse` returns a `HookEvent`; return `None` to ignore a payload.
- `render_*` returns a dict (JSON), a string (plain stdout) or `None` (no output).
- Every response passes `guard()`, which drops blocking values. If the TUI's
  *non-blocking* replacement reuses a blocking-looking field (Codex's
  `continue: false` on PostToolUse), allow exactly that with
  `allowed_blocking = ((TOOL_OUTPUT, "continue", False),)`.
- Register it in `adapters/__init__.py` and aliases in `tuis.py`.

The process side (exit 0 always, watchdog, captured stdout/stderr, logging)
is `hook.py`'s job — adapters must not print, exit or catch-and-log themselves.

## 3. The installer

Subclass `Installer` (or `ClaudeStyleInstaller`) in `src/subcortex/installers/`
and return `Target`s: `json_target` (merge into plain JSON),
`toml_block_target` (a marked block), `plugin_file_target` (a file we own),
`mcp_json_target`. Provide `hook_events()`, a realistic `sample_payload()`
per event (`"{transcript}"` / `"{big_output}"` placeholders are filled in),
and `expects_output()` so the self-test fails if the pipeline goes silent.
Set `min_version` when hooks appeared in a known release.

## 4. Plugins

Plugins call the daemon's policy endpoints instead of re-implementing policy:

```
POST /v1/prompt-hint  {prompt, session_id, tui}                       -> {hint: str|null}
POST /v1/tool-output  {output, tool?, input?, failed?, session_id, tui} -> {replacement: str|null}
POST /v1/snapshot     {session_id, messages, tui}                     -> {saved}
POST /v1/restore      {session_id, tui}                               -> {context: str|null}
```

- Send `session_id` and `tui` everywhere: state is keyed by both, and the
  prompt-hint call is what records the request that tool output is judged by.
- Every request carries the `X-Subcortex-Token` header, read from the token file
  the installer templates into the plugin (`__SUBCORTEX_TOKEN_FILE__`), and
  `X-Subcortex-Timeout-Ms` with the plugin's own timeout.
- Copy the transport block between the `>>> subcortex transport` markers from an
  existing plugin verbatim (a test keeps them identical): it talks raw TCP,
  because Bun sends even loopback `fetch` through `HTTP_PROXY`.

`messages` may be the TUI's raw message objects; they're normalized. Every
hook must catch everything and put a hard timeout on every request.

## 5. Tests

- `tests/test_adapters.py`: the TUI's recorded payloads → exact responses; the
  generic tests already check every registered adapter for blocking output and
  garbage input.
- `tests/test_installers.py` round-trips every registered installer and runs
  every self-test automatically.
- Plugins run under Bun in `tests/test_plugins.py`.

## The MCP fallback

`subcortex mcp` is a stdio MCP server exposing `subcortex_decide`,
`subcortex_classify_prompt` and `subcortex_judge_output`. It suits TUIs whose
hooks can't inject anything; prefer hooks or plugins where they exist, since
MCP tools cost the model tokens to call.
