# Codex Review #3 — feat-ai-sdk-v4

> Codex CLI v0.130.0, `model_reasoning_effort=high`.
> Base: `feat/ai-sdk-gateway-pr3` (PR #3 head).
> Diff: 8 commits (round-1 fixes + round-2 fixes + docs).
> Raw output: `review-3.raw.md`.

## Summary

Three more P2 findings — codex zoomed in on **what review-2's «P2 #3 move
ownership before file.read()» actually accomplished**, plus two related
edge cases in registry validation. No P1, but each P2 still represents a
real gap.

- Review-2's reordered ownership check **doesn't actually defend** before
  multipart parse — FastAPI's Form/UploadFile binding spools the body
  BEFORE the handler body runs.
- Cross-customer base_url consistency grouping uses raw strings, so
  `http://dify` and `http://dify/` slip into different groups.
- Shared-mode customer_id length budget is unchecked — a 38+ char
  customer_id loads fine but every dataset create fails.

No P1 outstanding → **GATE: PASS**. All three fixed in this round.

| Severity | Count |
|---|---|
| [P1] | 0 |
| [P2] | 3 |

## Full review comments

- [P2] **Don't rely on handler order to avoid multipart spooling** —
  `gateway/src/gateway/routers/files.py:79`
  For shared-mode `/v1/files` uploads to a foreign or missing dataset,
  this check still runs only after FastAPI has parsed the multipart
  form and materialized `UploadFile`/`dataset_id`; a large body can
  therefore still be spooled to memory/disk before the 404. If the goal
  is to reject before upload I/O, the dataset id needs to be available
  outside the multipart body (for example query/header) or enforced by
  a pre-parse size/ownership layer.

- [P2] **Normalize base URLs before consistency grouping** —
  `gateway/src/gateway/registry.py:325`
  When two registry entries point at the same Dify deployment but one
  uses a trailing slash, e.g. `http://dify` vs `http://dify/`, this
  raw-string grouping splits them into different groups even though
  `DifyClient` normalizes both to the same upstream. That bypasses the
  new mixed-mode/shared-model validation and can allow the inconsistent
  shared/dedicated configuration this check is meant to reject.

- [P2] **Reject shared customer IDs that leave no dataset-name budget** —
  `gateway/src/gateway/registry.py:212`
  In shared mode the stored dataset name is `{customer_id}__{name}` and
  Dify caps it at 40 chars, but this allows customer IDs up to 64
  chars. A valid shared registry with `len(customer_id) >= 38` can
  start successfully while every `POST /v1/datasets` fails because
  even a one-character name exceeds the Dify limit; validate this at
  registry load for shared customers or lower the shared-mode prefix
  length.

## Gate

**PASS** (no [P1] findings). All three P2 fixes will land in this round.
