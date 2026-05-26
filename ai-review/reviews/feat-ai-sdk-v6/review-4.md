# Codex Review #4 — feat/ai-sdk-gateway-pr6

> Reviewer: OpenAI Codex CLI (user ran locally).
> Base: `main`.
> Diff at review time: 7 commits — through `4d492c45c`.

## Summary

Codex caught **one P2 security finding** that none of the previous 3
rounds (1 Claude self + 3 Codex) touched: **filesystem mode bits on
the secret-bearing registry file**. This is a different axis from the
"failure ordering" family rounds 1-3 stomped on — this is "side
effects on persistent state are different from in-process state".

| Severity | Count |
|---|---|
| [P1] | 0 |
| [P2] | 1 |
| [P3] | 0 |

## Finding — verbatim

```
[P2] Preserve secret registry file permissions —
gateway/src/gateway/admin/registry_merge.py:236

When `registry.yaml` already has restrictive permissions, or when this
creates it on a system with a default umask like `022`, the temporary
file is opened with default permissions and then replaces the original.
Since the registry contains `console_password`, SDK keys, and dataset
API keys, this can turn a previously private file into a world-readable
one; create the temp file with restrictive mode such as `0600` and/or
copy the existing file mode before `os.replace`.
```

## Why all earlier rounds missed it

Rounds 1-3 were thinking about **what can go wrong DURING the write**:
- Round 1: --mode case validation too late
- Round 2: registry-content validation after Dify call
- Round 3: filesystem writability + parser edge cases

All of those were "does the write succeed or fail correctly?" Codex
round 4 asked a different question: **even when the write succeeds,
what does the resulting file LOOK LIKE in the filesystem?** That's
not on the same axis as ordering — it's about post-condition state.

The leak is silent and persistent: every `add-customer` invocation
on a Linux box with default umask widens `registry.yaml` perms from
whatever-it-was to `0644`. No error, no log, no test catches it.
Operators who set up perms once and forgot, then ran the CLI six
months later, would have credentials world-readable.

## What Claude (and I) missed

- I never thought to ask "what perms does `tmp.open('w')` give?"
- The 326+ tests covered behaviour and content, never permissions
- `mypy strict` doesn't have a notion of file-system invariants
- Pre-PR code never had to write a secret-bearing file via tmp+rename,
  so this was new ground PR #6 introduced and I didn't audit

## Gate

**PASS after fix** — see `review-4-response.md`. Fix landed as commit
`37521c47d`.

## Cumulative review history

| Round | Reviewer | P1 | P2 | P3 | Cumulative | Family |
|---|---|---|---|---|---|---|
| 1 | Claude self | 0 | 2 | 4 | 6 | self-spotted maintainability |
| 2 | Codex | 0 | 1 | 2 | 9 | merge / parser validation timing |
| 3 | Codex | 0 | 1 | 1 | 11 | filesystem write + parser edge timing |
| 4 | Codex | 0 | 1 | 0 | 12 | **file mode security** ← new axis |

Four rounds, 12 findings total, 5 P2 + 5 P3 + 0 P1. **All P2 fixed**
(4 hard fixes + 1 doc-only for the ruamel ergonomics tradeoff).
P3s 3 fixed, 3 deferred with rationale.

Codex's round 4 added a **new dimension** — security / file-system
post-conditions — to its previous three rounds of "side effects
before validation". The pattern is "look at what's left in the world
after the code runs, not just whether the code raised correctly."

Worth a round 5? Probably yes — this round just opened a new axis
(persistent-state attributes), so there could be more in the same
family (e.g., umask for parent dir creation, temp file lifetime if
process is killed, log file perms). If round 5 returns 0, PR #6
has the same "clean baseline" status PR #5 achieved at round 4.
