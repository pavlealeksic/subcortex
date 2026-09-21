# Aider (aider.chat): subcortex adapter spec

Verified 2026-09-21 from PyPI, the GitHub repo (source at `main`), and aider.chat docs.

**Verdict:** the latest Aider has no hooks, no plugins, and no MCP. None of subcortex's 4 behaviors can be implemented through a supported seam. There are only indirect seams (command wrappers, startup flags, log files), and they cover part of behavior 2 plus rough approximations of 3 and 4. **Not worth a first-class adapter.** At most, document an optional recipe, "wrap your test/lint command". If Aider users matter, target the **cecli** fork (formerly aider-ce), which does have hooks and MCP (see §8).

| Behavior | Aider seam | Status |
|---|---|---|
| 1. Prompt submit: hint injection | none | **Impossible.** No per-prompt hook. The closest is static `--read` files loaded at launch |
| 2. Replace large tool output | `--test-cmd` / `--lint-cmd` wrapper | **Partial.** Only for the test/lint commands Aider runs. Shell commands suggested by the LLM and `/run` output cannot be intercepted |
| 3. Pre-compaction snapshot | `--chat-history-file` / `--llm-history-file` (passive tail) | **Observe-only workaround.** No event. Aider's history summarization runs in-process with no hook |
| 4. Re-inject after compaction | `--read <snapshot.md>` / `--load <cmds>` at launch | **Launch-time only.** Nothing fires after summarization |

---

## 1. Sources and latest version

| Item | Value | Source |
|---|---|---|
| Latest release | **aider-chat 0.86.2**, PyPI upload 2026-02-12. Previous: 0.86.1 (2025-08-13) | https://pypi.org/pypi/aider-chat/json |
| Latest GitHub release object | v0.86.0 (2025-08-09). Tags go up to `v0.86.2` and `v0.86.3.dev` | `gh api repos/Aider-AI/aider/releases`, `/tags` |
| Repo activity | `main` last pushed 2026-05-22 (model-list updates, bash tree-sitter tags). Not archived. About 49k stars. Development has slowed to maintenance | `gh api repos/Aider-AI/aider` |
| CLI options (complete list, from `aider/args.py` on `main`) | Contains no `hook`, `plugin` or `mcp` option. The only "hooks" string is `--git-commit-verify` (git pre-commit hooks) | https://github.com/Aider-AI/aider/blob/main/aider/args.py |
| Source tree | No `mcp`, `hook` or `plugin` files anywhere in `aider/` | `gh api repos/Aider-AI/aider/git/trees/main?recursive=1` |
| MCP status | Issue #3314 "MCP SUPPORT" is **open** (updated 2026-09-10). Issue #2525 is open. MCP PRs #3672 and #3937 were **closed unmerged** (2026-07-09). PR #5539 ("opencode-compatible .mcp.json") is open and unmerged. Issue #4506 is open | github.com/Aider-AI/aider/issues/3314 , /pull/3937 , /pull/5539 |
| Hooks status | Issue #5712 "Add a Hook System for Extensible Automation" is open (2026-09-14). No implementation exists | github.com/Aider-AI/aider/issues/5712 |
| Docs used | https://aider.chat/docs/usage/lint-test.html , https://aider.chat/docs/config/aider_conf.html , https://aider.chat/docs/usage/watch.html | |

---

## 2. Config paths and environment variables

- `.aider.conf.yml` is loaded from the **home dir, then the git root, then the cwd**. "Files loaded last will take priority." `--config <file>` selects an explicit file.
- Every option has an env var: `AIDER_<OPTION>`, for example `AIDER_TEST_CMD`, `AIDER_LINT_CMD`, `AIDER_AUTO_TEST`, `AIDER_READ`. `.env` files are loaded from the home dir, git root, cwd, or `--env-file`. The docs say API keys other than OpenAI and Anthropic must live in `.env`, not the YAML.
- Relevant options, from `args.py` and the aider_conf docs:
  - `test-cmd`: "Specify command to run tests". `auto-test` defaults to False.
  - `lint-cmd`: "Specify lint commands to run for different languages, eg: 'python: flake8 --select=...'" (repeatable, `lang: cmd`). `auto-lint` defaults to True.
  - `read`: read-only files added to context at launch (repeatable).
  - `load`: "Load and execute /commands from a file on launch".
  - `chat-history-file` defaults to `.aider.chat.history.md`, `restore-chat-history` to False, `llm-history-file` to off.
  - `max-chat-history-tokens`: "Soft limit on tokens for chat history, after which summarization begins".
  - `notifications-command`: "a command to run for notifications instead of the terminal bell".
  - `suggest-shell-commands` defaults to True.
  - `watch-files`: AI-comment trigger mode (`AI!`/`AI?` comments in source files). This is an input channel, not an extension seam.
- Isolated e2e: `HOME=$T` plus a temp git repo, `--config $T/aider.yml --no-check-update --no-analytics --yes-always --message "..."`. `--message` runs one turn and exits.

---

## 3. "Registration" snippet (the closest workable seam)

Aider exposes no event contract, so nothing gets registered. The only integration is to wrap the commands Aider already runs:

```yaml
# ~/.aider.conf.yml (or <repo>/.aider.conf.yml)
test-cmd: subcortex wrap --tui aider --kind test -- pytest -q
auto-test: true
lint-cmd:
  - "python: subcortex wrap --tui aider --kind lint -- ruff check"
# Optional launch-time re-injection of the last snapshot (behavior 4 approximation):
read:
  - .subcortex/aider-snapshot.md
```

`subcortex wrap` does not exist yet. If it is added, it must:
- run the child, then **exit with the child's exit code unchanged**, and
- trim only the captured stdout/stderr.

Aider's contract, from the lint-test docs: "If there are ... errors, aider expects the command to print them on stdout/stderr and return a non-zero exit code."

---

## 4. Per-event payloads and responses (source-verified)

### 4.1 Prompt submit (behavior 1): impossible
There is no pre-prompt hook, no stdin/stdout protocol, and no plugin loader. Workarounds:
- `read:` files: static context at launch.
- `--load cmds.txt`: runs slash commands at launch only.
- Scripting API (`from aider.coders import Coder; coder.run(msg)`): you would have to write a replacement front-end. Unsupported, and it breaks the TUI.

### 4.2 After tool (behavior 2): partial, test and lint output only
Aider runs three kinds of external commands. Their output reaches the LLM as follows (`aider/coders/base_coder.py`, `aider/commands.py`, `aider/linter.py` on `main`):

| Command source | How output enters the chat | Can subcortex trim it? |
|---|---|---|
| `test-cmd` (auto-test after edits, or `/test`) | `cmd_test` calls `cmd_run(args, add_on_nonzero_exit=True)`. On **non-zero exit** the full output is formatted with `prompts.run_output` and added to `cur_messages`, then "Attempt to fix test errors?" sets `reflected_message`. On exit 0 nothing is added | **Yes, via wrapper.** The wrapper sees and trims the output. Preserve the exit code |
| `lint-cmd` (auto-lint after edits) | `Linter.run_cmd` appends the **relative filename** to the cmd, captures stdout+stderr, and returns nothing on exit 0. Otherwise it returns `"## Running: {cmd}\n\n" + errors`. `find_filenames_and_linenums` parses `file:line` refs from that text, then "Attempt to fix lint errors?" follows | **Yes, via wrapper.** Keep `file:line` lines in the head/tail so Aider's line-context extraction still works |
| LLM-suggested shell commands (`suggest-shell-commands`) | `handle_shell_commands` asks "Run shell command?" (explicit yes), runs via `run_cmd`, then asks "Add command output to the chat?". If yes, the raw output is appended as a user message | **No.** The commands are arbitrary strings written by the LLM, with no interception point |
| `/run` or `!` by the user | `cmd_run` asks "Add {k}k tokens of command output to the chat?" | **No** (the user can decline) |

### 4.3 Before compaction (behavior 3): no event; observe-only workaround
- Aider's "compaction" is `ChatSummary` (`aider/history.py`). After each exchange, `summarize_start()` checks `too_big(done_messages)` against `max_chat_history_tokens`, then summarizes in a **background thread** (`summarize_worker`). `summarize_end()` swaps in `summarized_done_messages`. No callback or hook exists.
- Workaround: have the daemon tail `.aider.chat.history.md` (markdown transcript, appended live, per repo) or `--llm-history-file` (exact LLM messages) and keep a rolling snapshot. Both are passive, and the timing of summarization is unknown to subcortex.

### 4.4 Session start after compaction (behavior 4): no event
- Nothing fires after summarization, and summarization happens in-process within the same session.
- The only injection points are launch-time `read:`/`--load`, or `--restore-chat-history`, which restores Aider's own log.

### 4.5 Other observable signals
- `--notifications-command <cmd>` runs via `subprocess.run(cmd, shell=True, capture_output=True)` when Aider is waiting for input after a response (`io.ring_bell`). It gets no payload, and its output is ignored except for a warning on failure. It can serve as a "turn ended" tick for snapshotting the history file. It only fires when notifications are enabled (`--notifications`).

---

## 5. Exit codes and blocking behavior to avoid

- **Wrapper exit code is load-bearing.** Returning 0 when the real test/lint failed hides the errors from the LLM, because Aider only adds output on non-zero exit. Returning non-zero when the child succeeded triggers a bogus "fix errors" loop. Always propagate the child's exit code, and on any wrapper error `exec` the child untouched.
- If the lint wrapper cannot run (OSError), Aider prints "Unable to execute lint command" and skips linting. That fails open, but loses lint.
- Keep wrappers fast. They run synchronously in Aider's main loop.
- `notifications-command` failures only warn.

---

## 6. MCP registration

**None.** Aider has no MCP client (issue #3314 open, PRs closed unmerged), so `subcortex mcp` cannot be used with mainline Aider.

---

## 7. Is Aider worth supporting?

No first-class adapter. Aider has lost momentum: last release in February 2026, last commit in May 2026, and the MCP and hooks requests have gone unmerged for over a year. Its tool loop is not agentic in a way subcortex can hook.

If anything, ship docs only: the `subcortex wrap` recipe for `test-cmd`/`lint-cmd`, plus an optional `read:` snapshot file. Reconsider only if issue #5712 (hooks) lands.

---

## 8. Closest real seam: the cecli fork (formerly aider-ce)

- Repo `cecli-dev/cecli`, latest **v1.6.0 (2026-09-19)**. Actively developed Aider fork. Has hooks and MCP.
- Config file: `.cecli.conf.yml`. Hook types: `start`, `on_message`, `end_message`, `pre_tool`, `post_tool`, `end`.
- **Command hooks** get metadata via `{placeholder}` substitution into the command string: shell-quoted `{timestamp} {coder_type} {message} {message_length} {tool_name} {arg_string} {output}`. There is **no stdin JSON**, and stdout is only printed.
  - A non-zero exit, a timeout, or an exception returns 1, and **"a pre_tool hook ... non-zero exit code ... the tool execution will be aborted"**.
  - Fail-open therefore requires the command to always exit 0.
- **Python hooks** (`file: path.py`, class extends `BaseHook`, `async execute(self, coder, metadata)`):
  - Returning False aborts (pre_tool/post_tool).
  - `HookHelpers.append_message(coder, {"role":"user","content":...})` injects context. That would cover behavior 1 via `on_message`, and possibly 4 via `start`.
  - post_tool gets `output`, but the return value is bool, so **output replacement is not supported** through the hook API (UNVERIFIED whether mutating coder messages works).
- MCP: `mcp-servers: { mcpServers: { name: { transport: stdio, command, args } } }` in the conf.
- There is no compaction hook.
- Not Claude-Code-compatible.
- Sources: https://github.com/cecli-dev/cecli/blob/main/cecli/website/docs/config/hooks.md , `cecli/hooks/{types,base,integration}.py` , `cecli/website/docs/config/mcp.md`.

AiderDesk (`hotovo/aider-desk`, Electron GUI, not a TUI) is also active and has its own hooks and MCP. It is out of scope for a terminal adapter.

---

## 9. Unverified claims
1. `AIDER_LINT_CMD`, `AIDER_TEST_CMD`, `AIDER_READ`, `AIDER_LOAD`, `AIDER_MESSAGE` and `AIDER_NOTIFICATIONS_COMMAND` are documented (options.html, dotenv.html). The exact multi-value syntax for a list option passed through a single env var is untested.
2. Whether 0.86.2 (PyPI, Feb 2026) differs from `main` in any code path cited. Line references are from `main`. No hook or MCP code exists in either.
3. cecli: whether a Python `post_tool` hook can mutate the tool result in place. Untested, and the docs do not claim it.
