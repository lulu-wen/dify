# Codex Review #6 — feat-ai-sdk-v4

> Codex CLI v0.130.0, `model_reasoning_effort=high`.
> Base: `feat/ai-sdk-gateway-pr3` (PR #3 head).
> Diff: 14 commits (6 fix rounds + spec + docs).
> Raw output: `review-6.raw.md`.

## Summary

One P1 + two P2 — codex caught a **PR #3 backward-compat break** from
review-3 plus two design gaps that the earlier rounds missed:

- **P1**: review-3 moved `/v1/files` `dataset_id` to query-only,
  breaking existing PR #3 clients that send it via multipart form.
- **P2**: shared-mode embedding resolver compares `requested_id` to
  `shared.name` (Dify served name) directly, ignoring that
  `requested_id` is documented as a *customer-facing* id.
- **P2**: registry consistency check verifies mode + shared_embedding
  agreement within a base_url group, but doesn't prevent two shared
  customers from using the same `customer_id` — collapsing the
  `{customer_id}__` prefix isolation.

| Severity | Count |
|---|---|
| [P1] | 1 |
| [P2] | 2 |

## Full review comments

- [P1] **Preserve multipart upload dataset_id** —
  `gateway/src/gateway/routers/files.py:71-74`
  Existing `/v1/files` clients send `dataset_id` as a multipart form
  field because the previous FastAPI signature declared it with
  `Form(...)`; this query-only lookup now returns 400 before parsing
  the body for those clients. This breaks the PR #3 upload contract /
  OpenAI SDK `extra_body` flows, so prefer the query param for
  shared-mode preflight but keep a form-field fallback after parsing
  (or at least for dedicated mode).

- [P2] **Resolve shared embedding by public model ID** —
  `gateway/src/gateway/routers/datasets.py:109`
  When a shared-mode caller explicitly passes the customer-facing
  `embedding_model` ID advertised by `/v1/models`, this compares that
  ID to the Dify served model name from `shared_embedding_model` and
  rejects it even if the registry entry maps to the same workspace
  model. The request schema documents this field as a customer-facing
  ID, so resolve the ID first and verify its name/provider against the
  shared model instead of comparing directly to `shared.name`.

- [P2] **Reject duplicate shared customer IDs** —
  `gateway/src/gateway/registry.py:366`
  The new shared-mode isolation prefix is derived only from
  `customer_id`, but the registry consistency check still allows two
  shared entries on the same Dify base URL to use the same
  `customer_id` with different SDK keys. In that configuration both
  keys generate the same `{customer_id}__` dataset prefix and can
  list/get/delete each other's datasets, so the load-time check should
  reject duplicate shared customer IDs within a Dify group unless they
  are explicitly treated as the same tenant.

## Gate

**FAIL** (1 × [P1]). All three findings fixed in this round.
