# Codex Review #8 — feat-ai-sdk-v4

> Codex CLI v0.130.0, `model_reasoning_effort=high`.
> Base: `feat/ai-sdk-gateway-pr3` (PR #3 head).
> Diff: 18 commits (8 fix rounds + spec + docs).
> Raw output: `review-8.raw.md`.

## Summary

Two P2 — both classic «scope of a new constraint or behaviour» issues
that the previous rounds didn't surface:

- Shared-mode dataset list forwards ``keyword`` to Dify, but Dify stores
  names with the customer prefix. A caller searching ``keyword=tenant-a``
  would match every owned dataset (each starts with ``tenant-a__``).
- The new ``max_length=64`` on ``customer_id`` Field applies to all
  modes, breaking dedicated-mode PR #1-#3 deployments that may have
  longer ids (same pattern as review-4's slug check).

No P1 outstanding → **GATE: PASS**.

| Severity | Count |
|---|---|
| [P1] | 0 |
| [P2] | 2 |

## Full review comments

- [P2] **Filter shared dataset keywords on public names** —
  `gateway/src/gateway/routers/datasets.py:395`
  In shared mode the customer never sees the `{customer_id}__` prefix,
  but this forwards `keyword` to Dify before stripping that prefix.
  For example, `GET /v1/datasets?keyword=tenant-a` for customer
  `tenant-a` will match every stored `tenant-a__...` dataset and the
  ownership filter will keep them all, even when none of the
  customer-facing names contain `tenant-a`.

- [P2] **Keep dedicated customer IDs backward-compatible** —
  `gateway/src/gateway/registry.py:207`
  The new `max_length=64` applies to dedicated-mode customers too, so
  an existing PR #1-#3 registry with a longer `customer_id` now fails
  to load even though dedicated mode does not use the shared
  dataset-name prefix budget. Since the rest of this change
  intentionally keeps shared-mode `customer_id` restrictions out of
  dedicated mode, move this length check into the shared-mode
  validator or remove it for dedicated entries.

## Gate

**PASS** (no [P1] findings).
