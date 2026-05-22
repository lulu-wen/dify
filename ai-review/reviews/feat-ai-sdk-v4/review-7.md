# Codex Review #7 — feat-ai-sdk-v4

> Codex CLI v0.130.0, `model_reasoning_effort=high`.
> Base: `feat/ai-sdk-gateway-pr3` (PR #3 head).
> Diff: 16 commits (7 fix rounds + spec + docs).
> Raw output: `review-7.raw.md`.

## Summary

Round 7 catches a follow-up from review-6's customer_id uniqueness fix.
Review-6 scoped the check to "duplicates within the same base_url
group", which was the wrong invariant — gateway cache keys throughout
the codebase use `customer_id` alone, so duplicates ANYWHERE collide.

No P1 outstanding → **GATE: PASS**.

| Severity | Count |
|---|---|
| [P1] | 0 |
| [P2] | 1 |

## Full review comment

- [P2] **Use globally unique customer IDs or app-cache keys** —
  `gateway/src/gateway/registry.py:427-430`
  When two shared customers on different `base_url`s reuse the same
  `customer_id`, this check lets the registry load because duplicates
  are only detected within each base-url group. That allowed
  configuration breaks chat/App lifecycle paths: `AppManager` caches
  apps by `(customer_id, model_id)`, sessions by `customer_id`, and GC
  resolves by `find_by_customer_id`, so the second deployment can reuse
  the first deployment's app key/session or delete the wrong app during
  GC. Either reject duplicate `customer_id`s globally or include the
  deployment/sdk key in the AppManager cache/session keys before
  allowing this.

## Gate

**PASS** (no [P1] findings). Single P2 fix lands in this round.
