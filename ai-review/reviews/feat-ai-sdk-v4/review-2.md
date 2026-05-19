# Codex Review #2 — feat-ai-sdk-v4

> Codex CLI v0.130.0, `model_reasoning_effort=high`.
> Base: `feat/ai-sdk-gateway-pr3` (PR #3 head).
> Diff: 6 commits (spec + R1 + R3/R4/R5 + 2 round-1 fix commits + docs).
> Raw output: `review-2.raw.md`.

## Summary

Three P2 findings — codex narrowed in on **wiring + DoS surface**:

- ``IsolationStrategy.app_name`` is declared but never called (the actual
  App builder still uses a hardcoded string).
- Shared-mode dataset name prefix can overflow Dify's 40-char cap when
  the customer_id is long; gateway accepts it, Dify rejects later.
- Shared-mode upload reads the file body BEFORE the ownership check, so
  an attacker who knows a foreign UUID can force the gateway to spool
  large uploads only to 404 afterward.

No P1 outstanding → **GATE: PASS**.

| Severity | Count |
|---|---|
| [P1] | 0 |
| [P2] | 3 |

## Full review comments

- [P2] **Wire app naming strategy into AppManager** —
  `gateway/src/gateway/mode.py:112-113`
  In shared-mode chat requests this new `app_name()` strategy is never
  used: `AppManager._build_app()` still calls `build_chat_app_dsl(
  name=f"auto:{customer.customer_id}:{model.id}", ...)` regardless of
  `dify.mode`. That means the PR's App naming contract is only tested
  in isolation and actual Dify Apps keep the legacy name shape.

- [P2] **Validate the prefixed dataset name length** —
  `gateway/src/gateway/routers/datasets.py:245`
  In shared mode this sends `{customer_id}__{body.name}` to Dify, but
  the gateway still allows `customer_id` up to 64 chars and
  `DatasetCreateRequest.name` up to 40 chars. A request that is valid
  at the gateway, such as a long shared customer id plus `name='kb'`,
  can exceed Dify's dataset name limit and be rejected upstream.

- [P2] **Check dataset ownership before reading uploads** —
  `gateway/src/gateway/routers/files.py:81`
  For shared-mode uploads to a missing or foreign dataset, this
  ownership check runs only after `await file.read()` has loaded the
  whole upload into a bytes object. A request that will ultimately
  return `dataset_not_found` can still force the gateway to read a
  large file into memory first; move the dataset ownership verification
  ahead of the file read.

## Gate

**PASS** (no [P1] findings). All three P2 fixes will land in this round.
