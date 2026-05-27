# Codex Review #3 — feat/ai-sdk-gateway-pr6

> Reviewer: OpenAI Codex CLI (user ran locally).
> Base: `main`.
> Diff at review time: 5 commits — through `7c66f0f46`.

## Summary

Codex caught **one P2 + one P3** — both still in the same "failure
after side-effect" family, but at points my round-2 fix didn't
generalise to.

| Severity | Count |
|---|---|
| [P1] | 0 |
| [P2] | 1 |
| [P3] | 1 |

## Findings — verbatim

```
[P2] Handle registry write failures after provisioning —
gateway/src/gateway/admin/cli.py:431-431

When the registry path cannot be written (for example an unwritable
directory, a parent path that is a file, or disk full),
`write_registry_atomic` raises after `_provision_dataset_api_key`
has already created a real Dify dataset key. Because this block only
catches `RegistryMergeError`, the operator gets an unhandled
traceback and the newly-created key is left orphaned with no
registry entry; add a writable preflight before provisioning and/or
catch filesystem errors here with a clean failure path.

[P3] Reject non-mapping customer entries cleanly —
gateway/src/gateway/admin/registry_merge.py:197-198

If a hand-edited registry has `customers` as a list but one item is
not a mapping, such as `customers: [null]` or `- bad`, this calls
`.get` on a non-dict before the validation wrapper runs. That
escapes as an `AttributeError` instead of the intended
`RegistryMergeError`, so `add-customer` shows a traceback for a
schema error that should be reported cleanly before any network call.
```

## Why round 2 didn't catch this

Round 2's P2 fix moved local validation BEFORE the network call. I
thought that closed the orphan-key class.

Codex saw it didn't: my fix was complete for **CustomerEntry +
registry merge** failures. But there are TWO more places things can
fail:

1. **Filesystem-level write failures** (P2 round 3) — preflight
   `merge_customer` succeeds, network call succeeds, but
   `write_registry_atomic` raises `OSError` because the dir doesn't
   exist / is unwritable / disk full. The dry-run merge can't see
   filesystem state.

2. **Parser-level entry-shape failures** (P3 round 3) —
   `load_existing_registry` already validated that `customers` is a
   list, but never validated each item is a dict. `_find_customer_index`
   then crashes with AttributeError when iterating non-dict items
   (`customers: [null]` etc.).

Both share the round-2 pattern (side effect before validation) but
in code paths I generalised away from too fast. Round 2's regression
tests asserted `mock_provision_dataset_key.call_count == 0` for
**registry-content** failures — but didn't exercise filesystem
failures or parser-edge entries.

## Gate

**PASS after fix** — see `review-3-response.md`. Fix landed as commit
`87155adc5`.

## Cumulative review history

| Round | Reviewer | P1 | P2 | P3 | Cumulative |
|---|---|---|---|---|---|
| 1 | Claude self | 0 | 2 | 4 | 6 |
| 2 | Codex | 0 | 1 | 2 | 9 |
| 3 | Codex | 0 | 1 | 1 | 11 |

Three rounds, 5 P2 + 5 P3 total, 4 P2 fixed, 1 P2 documented (yaml
comment loss), all P3 either fixed or deferred with rationale, no P1.

Codex has now caught two distinct sub-families of "side effects
before validation":

- **Round 2**: CustomerEntry / registry merge validation moved too
  late
- **Round 3**: filesystem write + parser-edge validation also too late

Both belong to the same parent concept: "what can fail AFTER the
network call?". The round-2 fix asked "what registry-content
failures can happen after?" — round 3 asks "what OTHER failures can
happen after, regardless of registry content?".

Worth round 4? Less obvious now — the remaining surface (atomic
write internals, OS-specific `os.replace` edge cases, signal handling
during write) is harder to enumerate at static-review time and easier
to discover via real Jetson E2E. I'd run one more round to confirm
or stop.
