# Codex Review #1 — feat-ai-sdk-v4

> Codex CLI v0.130.0, `model_reasoning_effort=high`.
> Base: `feat/ai-sdk-gateway-pr3` (PR #3 head, commit `cb516983f`).
> Diff: 4 commits (spec + merge + R1 foundation + R3/R4/R5 routers), ~1100 LOC + 25 tests.
> Raw output: `review-1.raw.md`.

## Summary

Codex found four real **isolation correctness** gaps in shared-mode:

- ambiguous `customer_id` prefixes can bypass ownership checks
- missing-vs-foreign datasets produce distinguishable error envelopes
- pagination math leaks workspace-wide state to a single tenant
- embedding resolver gates on field presence, not the `mode` flag

All four are concrete issues in the shared-mode trust boundary I designed.
None of them break dedicated mode (PR #1-#3 customers unaffected).

| Severity | Count |
|---|---|
| [P1] | 2 |
| [P2] | 2 |

## Full review comments

- [P1] **Validate customer_id before prefix ownership checks** —
  `gateway/src/gateway/mode.py:127`
  In shared mode, ownership is decided with a plain
  `startswith("{customer_id}__")`, but `CustomerEntry.customer_id` only
  has `min_length=1` validation. If one customer is `acme` and another
  is `acme__beta`, datasets for `acme__beta` are named `acme__beta__...`
  and will also pass the `acme__` check, allowing `acme` to list / get /
  delete the other customer's datasets. Enforce the documented slug rule
  or escape the prefix before relying on this check.

- [P1] **Normalize missing dataset errors in ownership checks** —
  `gateway/src/gateway/routers/datasets.py:177-180`
  When this `get_dataset` call returns Dify's 404, it propagates as
  `UpstreamClientError` with code `upstream_invalid_request`, while an
  existing-but-foreign dataset raises `UnknownDatasetError` with code
  `dataset_not_found`. In shared mode, a caller who can try dataset
  UUIDs can therefore distinguish "does not exist" from "exists but not
  yours"; catch upstream 404s here and rethrow the same
  `UnknownDatasetError` envelope. The copied ownership helper in
  `files.py` needs the same treatment.

- [P2] **Page shared dataset lists after filtering** —
  `gateway/src/gateway/routers/datasets.py:284-287`
  In shared mode this still returns Dify's workspace-wide `has_more`
  while `total` is only the count of owned items on the current upstream
  page. If page 1 is filled with other customers' datasets, this
  customer can get `data=[]`, `total=0`, `has_more=true` even when their
  own datasets are on later pages, and the raw `has_more` also leaks
  that other tenants have data. Fetch/filter the shared workspace
  results before applying page/limit and computing `has_more`/`total`.

- [P2] **Gate shared embedding behavior on mode** —
  `gateway/src/gateway/routers/datasets.py:97-98`
  `shared_embedding_model` is documented as ignored in dedicated mode,
  but this branch treats any dedicated config that happens to include
  it as shared-mode resolution. In that scenario normal dedicated
  dataset creates can be rejected for using a registered embedding id,
  or silently bind to the wrong workspace-global model. Check
  `customer.dify.mode == "shared"` rather than the optional field's
  presence.

## Gate

**FAIL** (2 × [P1]). All four findings will be fixed before review #2.
