# MCP-only agents — `subcortex mcp` registration specs (as of 2026-09-21)

Scope: agents where subcortex can only (or should only) register its stdio MCP server (`subcortex mcp`, tools `subcortex_decide`, `subcortex_classify_prompt`, `subcortex_judge_output`). Each section: sources + version, user-level file path (macOS + Linux), format and JSONC risk, exact entry for a server named `subcortex`, approval/trust, non-interactive CLI, and unverified points. `/abs/subcortex` = absolute path to the installed binary.

**Summary**

| Agent | User-level file | Format | Commonly commented? | Non-interactive CLI | Approval gate |
|---|---|---|---|---|---|
| Warp app (agent) | `~/.warp/.mcp.json` | strict JSON | no | none (edit file; `/agent-add-mcp` is agent-driven) | global file auto-spawns, no prompt |
| `warp` CLI (TUI) | macOS `~/.warp_cli/.mcp.json`; Linux `~/.config/warp-terminal/cli/.mcp.json` | strict JSON | no | none (edit file) | none; starts once logged in |
| Oz (`oz agent run`) | per run `--mcp <json\|file\|uuid>` | JSON | – | the flag itself | – |
| Zed agent | `~/.config/zed/settings.json` (+ `global_settings.json`) | **JSONC** (comments + trailing commas by default) | **yes** | none | tool calls `confirm` by default |
| Continue `cn` | `~/.continue/config.yaml` | YAML | often | none | TUI asks per MCP tool |
| Auggie | `~/.augment/settings.json` | JSON (parser tolerates comments) | rarely | `auggie mcp add-json … --replace` | UNVERIFIED |
| Rovo Dev CLI | `~/.rovodev/mcp.json` (see §5 — path conflict) | JSON | unknown | none (`acli rovodev mcp` opens editor) | **first-run trust prompt per server** |
| Kiro CLI | `~/.kiro/settings/mcp.json` | JSON | no | `kiro-cli mcp add … --scope global` | per-tool prompt unless `autoApprove` |
| Cline CLI | `~/.cline/data/settings/cline_mcp_settings.json` | strict JSON | no | `cline mcp install subcortex --yes -- /abs/subcortex mcp` | UNVERIFIED |
| Amazon Q CLI | `~/.aws/amazonq/mcp.json` | JSON | no | `q mcp add … --scope global --force` | skip (maintenance-only) |

---

## 1. Warp — app agent, `warp` CLI (TUI), Oz

**Sources/version:** docs repo `warpdotdev/docs` @ `c17eb30` (2026-09-21): `src/content/docs/agents/capabilities/mcp.mdx`, `agents/cli/configuration.mdx`, `terminal/settings/file-locations.mdx`, `reference/cli/mcp-servers.mdx`, `changelog/2026.mdx`. Client source (open, AGPL, `warpdotdev/Warp` HEAD `0efd26e`, 2026-09-21) via GitHub API: `crates/warp_core/src/paths.rs`, `app/src/ai/mcp/{mod.rs,parsing.rs,file_based_manager.rs,file_mcp_watcher.rs}`, `resources/bundled/skills/add-mcp-server/SKILL.md`. Latest stable app: **v0.2026.09.16.08.27** (changelog 2026-09-16). No agent lifecycle hooks exist (docs/code search) → MCP + `AGENTS.md`/`WARP.md` rules only.

**Warp app (GUI agent)**
- Global file: `warp_home_mcp_config_file_path()` = `~/.warp/.mcp.json` on **macOS, Linux and Windows** (`%USERPROFILE%\.warp\.mcp.json`). Stable and Preview share `~/.warp`; Dev/OSS/Local channels use `.warp-dev`, `.warp-oss`, `.warp-local` (+ `-<profile>` for dev data profiles).
- Project file: `{repo_root}/.warp/.mcp.json` (never auto-spawns; user must toggle each session).
- Format: **strict JSON** (`serde_json::from_str`; no comments), then `${VAR}` placeholders are substituted from the environment (a missing var → servers in that file don't start). Wrapper keys accepted by `find_server_map` (`app/src/ai/mcp/mod.rs`): `/mcpServers` (preferred), `/servers`, `/mcp/servers`, or a flat name→server map (the bundled skill also lists `mcp_servers`; not seen in that function). Always write `mcpServers`.
- Entry:
  ```json
  {
    "mcpServers": {
      "subcortex": { "command": "/abs/subcortex", "args": ["mcp"] }
    }
  }
  ```
  Optional `env` (object), `working_directory` (default for global files = home dir). Docs mark `args` as required — always include it.
- Approval/trust: **global Warp servers always auto-spawn** (`file_based_manager.rs`: "Global Warp servers always auto-spawn"); the file watcher picks up edits live. "Config edits require approval" applies to edits made *by the Warp agent*, not external writers. Individual MCP tool calls follow the agent's permission profile (MCP allow/deny list, confirmations) — defaults UNVERIFIED.
- Also read (only when the user enables **Settings → Agents → MCP servers → "Auto-spawn servers from third-party agents"**): `~/.claude.json`, `~/.codex/config.toml`, `~/.agents/.mcp.json`. If subcortex is already registered for Claude Code/Codex, Warp may list a second `subcortex` from those files — dedupe risk (UNVERIFIED how Warp names collisions).
- Non-interactive CLI: none. (The bundled `/agent-add-mcp` skill makes the Warp agent edit the file.)

**`warp` CLI / TUI** (the new `warp` binary; `oz` is deprecated, supported through end of September 2026)
- File (`tui_mcp_config_file_path()` = `tui_config_local_dir()/.mcp.json`):
  - macOS: `~/.warp_cli/.mcp.json` (Preview: `~/.warp_cli-preview/.mcp.json`)
  - Linux: `${XDG_CONFIG_HOME:-~/.config}/warp-terminal/cli/.mcp.json` (derived from `config_local_dir().join("cli")`; docs confirm `settings.toml` lives in that directory)
  - Windows: `%LOCALAPPDATA%\warp\Warp\config\cli\.mcp.json`
- Same `mcpServers` JSON shape as above. **Global file only** (project `.mcp.json` files are not read). Changes are picked up automatically; configured servers start once logged in. `/mcp` shows the path being read. `/tui-migrate-setup` copies global servers from the app file.

**Oz** (`oz agent run` / `oz agent run-cloud`): per-run `--mcp` accepts a Warp-Drive UUID, a managed id (`linear|slack|jira`), inline JSON `'{"subcortex":{"command":"/abs/subcortex","args":["mcp"]}}'`, or a file path. Cloud runs execute in Warp's sandbox and cannot reach a locally installed `subcortex` binary — skip cloud.

## 2. Zed (agent panel)

**Sources/version:** Zed stable **v1.20.2** (2026-09-17; v1.21.0-pre 2026-09-16). Source at tag `v1.20.2`: `crates/paths/src/paths.rs`, `crates/settings/src/settings_store.rs`, `crates/settings_content/src/{project.rs,merge_from.rs}`, `assets/settings/initial_user_settings.json`. Docs (`zed-industries/zed` @ `a434bb7`, 2026-09-21): `docs/src/ai/mcp.md`, `ai/tool-permissions.md`, `worktree-trust.md`, `ai/terminal-threads.md`. No agent hooks; Terminal Threads run other CLIs with their own config.

- User settings file (`paths::settings_file()` = `config_dir()/settings.json`):
  - macOS: `~/.config/zed/settings.json`
  - Linux/FreeBSD: `$XDG_CONFIG_HOME/zed/settings.json` (default `~/.config/zed/`); Flatpak: `$FLATPAK_XDG_CONFIG_HOME/zed/settings.json`
  - `zed --user-data-dir <d>`: `<d>/config/settings.json`
- Format: **JSONC**. Zed's generated `initial_user_settings.json` starts with `//` comment lines and uses trailing commas, so essentially every user's file is JSONC → **subcortex must not rewrite it** (per policy).
- **Safer target (source-verified, undocumented): `config_dir()/global_settings.json`** (same directory). Zed watches and parses it (`parse_json_with_comments`), never writes it, and merges it **below** user settings (`default → extensions → global → user → server`). Maps merge per key (`MergeFrom for HashMap`), so `context_servers.subcortex` there coexists with the user's own `context_servers`. If the file doesn't exist we own it as plain JSON; if it exists and has comments, refuse.
- Entry (`ContextServerSettingsContent::Stdio`, untagged enum; `command` is the executable path):
  ```json
  {
    "context_servers": {
      "subcortex": { "command": "/abs/subcortex", "args": ["mcp"], "env": {} }
    }
  }
  ```
  Optional: `"enabled": true` (default true), `"remote": false`, `"timeout": <seconds>` (default `context_server_timeout` = 60). Do not use the old `{"source":"custom","command":{"path":…}}` shape.
- Trust/approval: global (user-level) MCP servers start regardless of worktree Restricted Mode (only `.zed/settings.json` servers are gated). Tool calls prompt by default: `agent.tool_permissions.default = "confirm"`; per-tool override key `mcp:subcortex:<tool>` (e.g. `"agent":{"tool_permissions":{"tools":{"mcp:subcortex:subcortex_decide":{"default":"allow"}}}}` — that lives in the user's JSONC file, so leave it to the user). Custom profiles with `"enable_all_context_servers": false` hide the server unless listed.
- Non-interactive CLI: none (Settings → AI → MCP Servers → Add Server, or edit the file). MCP servers configured in Zed are forwarded to external ACP agents.
- UNVERIFIED: if the user toggles the server off in the UI, Zed writes `context_servers.subcortex = {"enabled": false}` into **settings.json**; that partial object deserializes as the `Extension` variant and may not merge cleanly with our `Stdio` entry from `global_settings.json`.

## 3. Continue CLI (`cn`)

**Sources/version:** npm `@continuedev/cli` **1.5.47** (2026-06-18, still latest); repo `continuedev/continue` `main` @ `5522c6f` (2026-07-20, no later commits). Source: `extensions/cli/src/{configLoader.ts,env.ts,shared-options.ts,permissions/defaultPolicies.ts,services/index.ts,hooks/*}`, `packages/config-yaml/src/schemas/{index.ts,mcp/index.ts}`.

- File: `$CONTINUE_GLOBAL_DIR/config.yaml`, default `~/.continue/config.yaml` (macOS and Linux). YAML; users often keep comments → edit with a comment-preserving YAML round-tripper or refuse.
- **Only used when it already exists and no `--config` is passed** (`determineConfigSource`: `--config` → local `config.yaml` if present → remote `continuedev/default-cli-config`). **Do not create the file**: creating it switches `cn` off the remote default config (models etc.). Users running `cn --config <path|hub-slug>` never load it.
- Top-level required keys: `name`, `version` (`schema: v1` optional). Entry (list, zod `stdioMcpServerSchema`):
  ```yaml
  mcpServers:
    - name: subcortex
      command: /abs/subcortex
      args: ["mcp"]
      # optional: type: stdio, env: {}, cwd: …, connectionTimeout: <ms>
  ```
  Tag = `name: subcortex`. The Continue IDE extension reads the same file.
- Approval: default policies (`defaultPolicies.ts`) — interactive TUI: `{"tool":"*","permission":"ask"}` (every MCP call prompts); headless (`-p`): `*` = allow. `--allow <tool>` or `~/.continue/permissions.yaml` can pre-allow (exact MCP tool naming UNVERIFIED).
- Non-interactive CLI: none. `--mcp <owner/package>` only injects **hub** MCP blocks.
- Hooks: `HookService` is constructed (`services/index.ts`) but nothing fires events in 1.5.47 / `main` — still MCP-only. If ever wired it would merge `~/.claude/settings.json` hooks (see sweep §0.1).

## 4. Augment Auggie CLI

**Sources/version:** npm `@augmentcode/auggie` **0.36.0** (`latest`, 2026-08-21); newer tags: `prerelease` 0.37.0-prerelease.202609212000, `preview`/`daemon` **1.2.0-prerelease.202609211216** (a new major line, not examined). Docs https://docs.augmentcode.com/cli/integrations.md (fetched 2026-09-21); 0.36.0 bundle `augment.mjs` read statically.

- User file: `$HOME/.augment/settings.json` (home = `process.env.HOME || USERPROFILE || os.homedir()`), same on macOS/Linux. Also workspace `<ws>/.augment/settings.json`, `<ws>/.augment/settings.local.json`, and managed `/etc/augment/settings.json` (`%ProgramData%\augment\settings.json` on Windows).
- Format: JSON. The main settings loader uses a jsonc-parser `parse(text, errors, {allowTrailingComma:true})`, so comments are tolerated on read; but other code paths (plugin settings) read the same file with `JSON.parse` and write with `JSON.stringify`. Auggie itself writes plain JSON → commonly plain JSON; if a user added comments, refuse.
- Entry:
  ```json
  { "mcpServers": { "subcortex": { "command": "/abs/subcortex", "args": ["mcp"] } } }
  ```
  (stdio needs no `type`; `env` optional; `${workspaceFolder}` expands in `command`/`args`.)
- Non-interactive CLI (writes `~/.augment/settings.json`):
  - `auggie mcp add-json subcortex '{"command":"/abs/subcortex","args":["mcp"]}' --replace`
  - or `auggie mcp add subcortex --command /abs/subcortex --args "mcp" --replace` (`--args` is one string)
  - remove: `auggie mcp remove subcortex`; list: `auggie mcp list --json`. `--project`/`--local` target workspace files. Per-run override: `--mcp-config`.
- Approval: tool-permission rules exist (`docs /cli/permissions`); default for MCP tools UNVERIFIED.
- Hooks: see sweep §2.8 (PromptSubmit fire-and-forget in 0.36.0; append-only PostToolUse; SessionStart startup/resume only). Not re-verified here; 0.37/1.2 may differ.
- Auggie also loads plugins (`.mcp.json` inside a plugin root, parsed with JSON5) — alternative packaging, not needed.

## 5. Atlassian Rovo Dev CLI (`acli rovodev`)

**Sources/version:** Atlassian Support "Connect to an MCP server in Rovo Dev CLI" and "Manage Rovo Dev CLI settings" (fetched 2026-09-21); Bitbucket "Rovo Dev: Advanced agentic configuration"; official VS Code extension source `atlassian/atlascode` `src/rovo-dev/rovoDevUtils.ts`; community decompile `ghuntley/atlassian-rovo-source-code-z80-dump` (June 2025) `rovodev/common/{agent.py,config_versions/v1.py}`. Closed source; version scheme is date-based (e.g. `202604.11.1`); TUI is the default since April 2026 (needs ACLI ≥ v0.13.61; `acli rovodev legacy` for the old UI). Current exact version UNVERIFIED.

- Config: `~/.rovodev/config.yml` (YAML; `acli rovodev config` opens it; `acli rovodev run --config-file <f>` overrides). Same path on macOS/Linux.
- MCP file: **conflict in Atlassian's own docs** — MCP page and atlascode use **`~/.rovodev/mcp.json`** (atlascode creates it as `{"mcpServers": {}}`); the settings page and Bitbucket doc give the `mcp.mcpConfigPath` default as `~/.rovodev/mcp_config.json`. **Resolve at install time:** read `mcp.mcpConfigPath` from `config.yml` if set; else use `~/.rovodev/mcp.json` (current official tooling). `acli rovodev mcp` opens whichever file the CLI uses.
- Format: JSON (`load_mcp_servers_from_json`); comments unsupported/unknown → treat as strict JSON.
- Entry:
  ```json
  { "mcpServers": { "subcortex": { "command": "/abs/subcortex", "args": ["mcp"], "env": {}, "transport": "stdio" } } }
  ```
- **Trust step:** on startup each new server not in the allowlist triggers an interactive "allow this MCP server?" prompt; declined servers are skipped (decompiled `agent.py check_mcp_server_allowed`; id = `command + " " + args`, stored in `toolPermissions.allowedMcpServers` in 2025). Current docs describe `mcp.allowedMcpServers` with selectors `stdio:<command>:<args space-joined>` → e.g. `stdio:/abs/subcortex:mcp`. Pre-seeding requires rewriting user YAML and the exact key/format is version-dependent (UNVERIFIED) → prefer letting the user approve on first launch. `mcp.disabledMcpServers: [name]` disables.
- Non-interactive CLI: none.
- Hooks: `eventHooks` are notification-only (no I/O contract; sweep summary) → MCP + `AGENTS.md` only.

## 6. Kiro CLI (MCP only)

**Sources/version:** `kiro-cli` **2.22.0** (kiro.dev/changelog/cli, 2026-09-16); docs https://kiro.dev/docs/mcp/configuration.md (fetched 2026-09-21, identical to earlier copy), `docs/custom-agents/configuration-reference.md`, `docs/cli/v3.md` (CLI 3.0 early access via `kiro-cli --v3`, runs alongside 2.x, same `.kiro` config). Closed source (successor of Amazon Q CLI).

- User file: `~/.kiro/settings/mcp.json` (macOS/Linux). Workspace: `<cwd>/.kiro/settings/mcp.json`. Agent-level: `mcpServers` inside `~/.kiro/agents/<name>.json`. Priority agent > workspace > global; same name = override, different names = additive.
- Format: JSON (serde heritage; no comments).
- Entry:
  ```json
  { "mcpServers": { "subcortex": { "command": "/abs/subcortex", "args": ["mcp"], "env": {}, "disabled": false, "autoApprove": ["subcortex_decide", "subcortex_classify_prompt", "subcortex_judge_output"] } } }
  ```
  `autoApprove: ["*"]` approves all its tools; omit to keep prompts. `disabledTools` also supported. Remote entries use `url`/`headers`.
- Custom agents only see `mcp.json` servers when `"includeMcpJson": true`. The built-in default agent inherited `use_legacy_mcp_json: true` from Q CLI (verified in Q source), assumed the same in Kiro (UNVERIFIED).
- Hot-reload: a watcher on `mcp.json`/agents reconciles running servers without restarting the session.
- Non-interactive CLI: `kiro-cli mcp add --name subcortex --scope global --command /abs/subcortex --args mcp` (docs). `--force` to overwrite exists in Q CLI; for Kiro UNVERIFIED. Per agent: `--agent <name>`.
- Hooks: see sweep §2.7 (B1 only). Not needed for MCP.

## 7. Cline CLI (MCP only)

**Sources/version:** npm `cline` **3.0.62** (`latest`; git tag `cli-v3.0.62`); repo `cline/cline` HEAD `1085135` (2026-09-21, `apps/cli` 3.0.63 unreleased; no MCP-path changes since 3.0.62). Source: `sdk/packages/shared/src/storage/paths.ts`, `sdk/packages/core/src/extensions/mcp/config-loader.ts`, `sdk/packages/core/src/services/mcp-install.ts`, `apps/cli/src/main.ts` (`mcp install|add`, `uninstall|remove|rm`), `sdk/packages/core/src/extensions/agent-plugin/loader.ts`.

- File (`resolveMcpSettingsPath()`): `$CLINE_MCP_SETTINGS_PATH` → else `$CLINE_DATA_DIR/settings/cline_mcp_settings.json` → else `$CLINE_DIR/data/settings/…` → default **`~/.cline/data/settings/cline_mcp_settings.json`** (macOS/Linux; home from `$HOME`).
- Format: strict JSON (`JSON.parse`); a present-but-malformed file **throws**. Shared by the CLI, VS Code extension and JetBrains; Cline writes it atomically (temp + rename).
- Entry — either shape is accepted:
  - legacy/flat (what the VS Code extension writes; safest for cross-client use):
    ```json
    { "mcpServers": { "subcortex": { "type": "stdio", "command": "/abs/subcortex", "args": ["mcp"] } } }
    ```
  - native (what `cline mcp install` writes): `{"mcpServers":{"subcortex":{"transport":{"type":"stdio","command":"/abs/subcortex","args":["mcp"]}}}}`
  Optional: `disabled`, `timeout` (seconds), `cwd`, `env`. Unknown keys (e.g. extension-era `autoApprove`) are stripped by the zod schema.
- Non-interactive CLI (in 3.0.62): `cline mcp install subcortex --yes -- /abs/subcortex mcp` (alias `add`; without `--yes` it opens a TTY wizard); overwrites an existing same-name entry. Uninstall: `cline mcp uninstall subcortex` (aliases `remove`, `rm`; errors if absent).
- Alternative (3.0.62+): Agent Plugins under `~/.agents/plugins/<name>/` (`plugin.json` + `mcp.json` with exact fields `$schema` = `https://agent-plugins.org/schemas/1.0.0/mcp.schema.json` and `mcpServers{name:{type:"stdio",command,args,…}}`) start MCP servers "without touching `cline_mcp_settings.json`"; workspace `.agents/plugins` is not scanned. Manifest required fields and default enablement UNVERIFIED.
- Approval of MCP tool calls in the CLI: UNVERIFIED (auto-approve settings).
- Hooks/plugins for B1–B4: sweep §2.15 (TS plugin `afterTool` replaces results).

## 8. Amazon Q Developer CLI (`q`) — skip

- Status: repo README says "no longer being actively maintained and will only receive critical security fixes … now available as Kiro CLI". Last release **v1.19.7 (2025-11-17)**; not archived (last push 2026-08-24).
- For completeness (source `crates/chat-cli/src/util/paths.rs`, `cli/mcp.rs`, `cli/agent/mod.rs`): global `~/.aws/amazonq/mcp.json`, workspace `.amazonq/mcp.json`, agents `~/.aws/amazonq/cli-agents/*.json` (`mcpServers`); default agent has `use_legacy_mcp_json: true`. JSON (serde). `q mcp add --name subcortex --command /abs/subcortex --args mcp --scope global --force`.

## Unverified / flagged (all sections)

- Warp: MCP tool-call confirmation defaults; duplicate handling when the same server name comes from `~/.warp/.mcp.json` and third-party files.
- Zed: merge behaviour between a UI-written `{"enabled":false}` in `settings.json` and our `global_settings.json` entry; `global_settings.json` is undocumented (source-only).
- Continue: permission tool naming for MCP tools in `permissions.yaml`/`--allow`.
- Auggie: MCP approval defaults; 0.37 / 1.2 preview changes.
- Rovo Dev: exact default MCP file name in the current build; `allowedMcpServers` location/format in current builds; current version number.
- Kiro: `--force` flag; default-agent inclusion of `mcp.json`.
- Cline: MCP approval behaviour; agent-plugin manifest schema/enablement.
