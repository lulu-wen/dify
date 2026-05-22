# Codex Review #4 — feat-ai-sdk-v4

> Codex CLI v0.130.0, `model_reasoning_effort=high`.
> Base: `feat/ai-sdk-gateway-pr3` (PR #3 head).
> Diff: 10 commits (5 fix rounds + spec + 4 doc commits).
> Raw output: `review-4.raw.md`.

## Summary

One P2 — a **backward-compat regression** from review-1's fix. I added a
slug pattern to `CustomerEntry.customer_id` to defend shared mode's
prefix logic. The pattern applies at the Pydantic Field level, so it
runs against EVERY customer regardless of mode. Existing PR #1-#3
dedicated-mode registries with IDs like `Customer_A` or `acme_prod`
(uppercase / underscores) would fail to load after the PR #4 upgrade —
even though those IDs are perfectly safe in dedicated mode where the
prefix logic doesn't apply.

Shared mode is opt-in. Dedicated mode should remain backward compatible.

No P1 outstanding → **GATE: PASS**.

| Severity | Count |
|---|---|
| [P1] | 0 |
| [P2] | 1 |

## Full review comment

- [P2] **Keep dedicated customer IDs backward compatible** —
  `gateway/src/gateway/registry.py:210-213`
  This `customer_id` validation now applies to every registry entry,
  including the default `dedicated` mode. A PR #1-#3 deployment with
  an existing ID like `Customer_A` or `acme_prod` was previously valid
  and is not subject to shared-mode prefix parsing, but it will now
  fail registry validation at startup. Move the slug restriction into
  the shared-mode validator (or otherwise only enforce it when
  `dify.mode == "shared"`) to preserve dedicated-mode compatibility.

## Gate

**PASS** (no [P1] findings). The single P2 fix lands in this round.
