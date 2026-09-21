# Design notes: what subcortex takes from Jev's guidance, and what it doesn't

Two sources shaped 0.3.0: TypeSafe's write-up on splitting decisions from
generation with Jev (the Python SDK, question primitives, Auto Mode and Model
Router middleware), and *Jev Engineering for Coding Agents*, a synthesis of
where a coding harness spends tokens and where small decisions could save them.
subcortex sits outside the harness — hooks, plugins, MCP — so the question for
each idea was whether it can be done safely from there.

## Adopted

| Idea | In subcortex |
|---|---|
| Split decisions from generation: a fast typed decision instead of asking the big model | the whole premise: hints, trims and restores are decided by Jev/Laya in ~10–250 ms |
| Question ids are invisible to the model; write the whole question in `instructions` | every question names the state fields it reads (`prompt`, `task`, `tool_call`, `output`) |
| Put evidence in the state, separate from the question | the user's request is remembered per session and sent with every output judgment; without it nothing is trimmed |
| One condition per question, stated positively; combine in code | compound 0.2.0 questions replaced by atomic ones combined as primary + veto ([calibration.md](calibration.md)) |
| Confidence is not accuracy; calibrate against labeled examples | labeled calibration and held-out sets; `subcortex eval` |
| Batch questions over the same state | the questions of one decision go in one request |
| Pin the model version once thresholds are tuned | documented; the answering model is recorded (`subcortex stats`) |
| Track the bill: input tokens × $0.042/M | usage and cost metered per decision; `subcortex stats` |
| Query-aware context: keep what the current request needs (the "2,400-line grep → 12 hits" example) | trims keep lines of the removed middle that mention the request's terms |
| Retrieval and command output dominate token spend | trimming targets large shell output; failures are never trimmed |
| Instant compaction / restore relevant old context after a reset | compaction snapshot + exactly-once restore wherever a TUI's hooks or plugin API allow it |

## Deliberately not done (yet)

| Idea | Why not |
|---|---|
| Model routing (a cheaper model for simple requests) | the notes show per-token routing loses money once context is rebuilt per model, and a plugin layer can't switch the TUI's model safely; the hint gets the saving without switching |
| Permission gate (allow / ask / deny, "Auto Mode") | a pre-tool hook that can deny is exactly the kind of hook that can block a user's turn; if added, it will only ever *ask*, and only as an explicit opt-in |
| Per-chunk visibility ladder scored by the model | measured on Laya, per-block relevance was near chance (AUC 0.58–0.61); the deterministic keep-lines-that-mention-the-request rule gives the same protection without a guess |
| Tiered tool disclosure, KV-cache reuse, conditional AGENTS.md sections | need control of the harness's own prompt assembly, which hooks and plugins don't have |
| Replacing the TUI's compaction with a Jev-selected history | only Claude Code's experimental function hooks allow it; independent replays of the public plugin found its shipped wording dropped nearly every tool call |
