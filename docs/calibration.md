# Calibration: how subcortex's decisions were chosen and checked

A typed-decision model returns probabilities, not accuracies. A threshold only
means something against labeled data, for one backend and one question
wording. This page records how the questions and thresholds in
`src/subcortex/verdicts.py` were chosen, what they do, and how to check them on
your own setup (`subcortex eval`).

## The data

`src/subcortex/evalset.py` holds two labeled sets:

- **Calibration**: 40 requests (20 simple, 20 complex) and 24 tool outputs
  (12 disposable for their request, 12 needed). Questions and thresholds were
  chosen on this set.
- **Held out**: 24 requests and 12 outputs, written afterwards and never used
  to choose anything. It deliberately includes requests that sound small but
  aren't ("fix the bug", "make it faster", "just make the tests pass") and
  needed output that looks like noise (`npm audit`, `git log --stat`,
  `lsof -i`).

Failing commands are not in the output sets: subcortex never trims output that
looks like a failure (compiler errors, tracebacks, test failures, non-zero
exits, segfaults, panics), whatever a model says.

## What goes wrong, and what must never go wrong

| Mistake | Effect | Tolerance |
|---|---|---|
| "simple" hint on complex work | the main model may under-deliver | **none** |
| trim of output the request needs | the main model loses information it needed | **none** |
| no hint on a simple request | a missed saving | acceptable |
| no trim of disposable output | a missed saving | acceptable |

So every rule is tuned for zero mistakes of the first two kinds first, and only
then for coverage.

## What was measured

The questions `subcortex 0.2.0` asked turned out to be the problem:

| 0.2.0 question | Laya AUC | Effect |
|---|---|---|
| "Is `prompt` a simple request: a lookup, one-liner, or short answer with no multi-step reasoning, no code changes across files, and no specialist knowledge?" | **0.27–0.36** (inverted) | "simple" hints on 4 of 20 complex requests at the 0.8 threshold |
| "Is the tool output in `output` still needed to continue the task in `context`…" (no request known) | **0.47** | its only confident "drop" was output the request needed |

Following TypeSafe's question-writing guidance — one condition per question,
stated positively, naming the state fields it reads (Jev never sees question
ids), with the evidence (the user's request) in the state — candidate questions
were scored on both backends. The best discriminators:

| Question (id) | Jev AUC | Laya AUC |
|---|---|---|
| `quick`: the request can be handled with one fact, one command, or an edit of a few lines | 1.00 | 0.42 |
| `multi_step`: the request needs several dependent steps of work or reasoning (inverted) | 1.00 | 0.75 |
| `multi_file`: the request asks for code changes in more than one file (inverted) | 1.00 | 0.70 |
| `routine`: `output` is routine progress or log output with nothing specific to `task` (inverted) | 1.00 | 0.16 |
| `needed`: the output is still needed to accomplish `task` | 1.00 | 0.85 |
| `depends`: `task` depends on information that appears in `output` | 0.99 | 0.93 |

The two models put probabilities on very different scales (Jev rates truly
needed outputs as low as 0.13–0.22 on `needed`; Laya rates almost everything
above 0.6), so each backend gets its own rule: a primary signal plus a veto,
and every condition must hold.

## The rules and their results

| Backend | Hint when | Trim when |
|---|---|---|
| jev | `quick` ≥ 0.8 and `multi_step` ≤ 0.5 | `routine` ≥ 0.6 and `needed` < 0.3 |
| laya | `multi_step` ≤ 0.2 and `multi_file` ≤ 0.2 | off by default (opt-in: `depends` < 0.9 and `needed` < 0.75) |

| | hints: simple | hints: complex | trims: disposable | trims: needed |
|---|---|---|---|---|
| jev, calibration | 17/20 | **0/20** | 12/12 | **0/12** |
| jev, held out | 12/12 | **0/12** | 6/6 | **0/6** |
| laya, calibration | 5/20 | **0/20** | (3/12 opt-in) | **(0/12)** |
| laya, held out | 5/12 | **0/12** | (3/6 opt-in) | **(2/6) ✗** |

Laya's output rule looked safe on the calibration set and trimmed two needed
outputs on the held-out set — which is exactly why it is off by default.
Measured with jev-1.13.0 (`jev-latest`, 2026-09-22) and
`laya-multilingual-mlx`.

## Safety nets that don't depend on the model

- No request known → no trim (there is nothing to judge against).
- Anything that looks like a failure is kept whole.
- A trim keeps the head, the tail, and every line in between that mentions the
  request's distinctive terms or warns about something.
- A trim must save at least 20% or it isn't made.

## Checking it yourself

```sh
subcortex eval                  # your configured backend and thresholds
subcortex eval --backend jev    # or a specific backend
```

It prints both tables' rows for your setup and exits 1 on any hinted complex
request or trimmed needed output. Jev aliases move between releases: after
tuning thresholds against a version, pin it (`subcortex config set jev.model
jev-1.13.0`) and re-run `subcortex eval` when you upgrade.
