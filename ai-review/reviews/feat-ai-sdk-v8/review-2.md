# Codex Review #2 — feat/ai-sdk-gateway-pr8 (Phase 1b)

> Reviewer: OpenAI Codex CLI (user ran locally). Base: `main` (post PR #11 / 1a).
> Diff at review time: through `ac34b0196` (1b + self-review).

## Summary

Codex caught **two P2s**, both in the new cost-metering behaviour. Both
are real. The second exposed a design assumption I'd baked in wrongly:
that the client's `max_tokens` bounds generation — it doesn't, because
the gateway never forwards it to Dify.

| Severity | Count |
|---|---|
| [P1] | 0 |
| [P2] | 2 |
| [P3] | 0 |

## Findings — verbatim

```
[P2] Validate model before consuming TPM — routers/chat.py:194
  When TPM is enabled and selected_model is invalid, this call consumes the
  customer's token bucket before get_app_key() raises the 404. ... a typo or
  probing invalid chat models can still drain TPM and cause later valid chat
  requests to return 429; embeddings already resolve the model before
  metering. Move the TPM check after model validation or refund it on local
  validation failures.

[P2] Enforce the fallback output cap — routers/ratelimit_guard.py:44-48
  For chat requests that omit max_tokens, admission reserves only
  default_max_output_tokens, but ... this fallback is estimation-only and does
  not change what is sent upstream. In that common case the model can generate
  more than the reserved 1024 tokens, so concurrent unbounded requests can
  still exceed the node budget the new OOM guard is meant to protect. Either
  enforce/forward a cap when one is omitted or reject uncapped requests when
  admission is enabled.
```

## Analysis

**P2-1** — straightforward ordering bug. TPM (a token-bucket *consume*,
not refundable) ran before `get_app_key`'s model validation, so invalid
models drained the bucket. Embeddings already validated first —
inconsistent.

**P2-2** — the deeper one. I'd based the reservation on the client's
`max_tokens` (or the 1024 fallback). But the gateway is a thin OpenAI
shim over Dify *Apps*: it does NOT forward per-request `max_tokens` to
Dify — generation is bounded by the App's DSL `completion_params.max_tokens`,
which is built per-model from the registry. So:
1. The client's `max_tokens` is irrelevant to actual generation; basing
   the reservation on it was wrong from the start.
2. A model with no configured `max_tokens` generates unbounded (provider
   default), so reserving 1024 under-counts → the OOM guard leaks.

## Fixes (see review-2-response.md)

- **P2-1**: moved TPM + admission to AFTER model validation (`get_app_key`)
  AND body validation (`_last_user_message` etc.), right before generation.
  Invalid model / bad body now reject without touching TPM or holding a
  reservation. Aligns chat with embeddings.
- **P2-2** (two parts, so the reservation is a *true* upper bound):
  - Reservation now uses the model's configured cap
    (`completion_params.max_tokens`), falling back to
    `default_max_output_tokens` — the value that actually bounds the App's
    output, not the ignored client `max_tokens`.
  - `AppManager` injects `default_max_output_tokens` into the auto-built
    App's `completion_params` when the model omits a cap, so generation is
    genuinely bounded at the reserved amount. Bumped `DSL_VERSION`
    (`v3-dataset-enabled` → `v4-output-cap`) so existing Apps rebuild with
    the cap. Gated on `rate_limit_enabled` (a disabled limiter shouldn't
    silently cap generation).

## Behaviour change worth flagging

Models WITHOUT an explicit `completion_params.max_tokens` now have their
auto-built App capped at `default_max_output_tokens` (1024) when rate
limiting is enabled. Operators who want longer outputs set the model's
`max_tokens` in the registry. This is the "enforce a cap" option codex
endorsed, and it's sane edge hygiene (no unbounded generation on a shared
finite GPU) — but it does change output length for previously-uncapped
models on upgrade.

## Gate

**PASS after fix.** Suite 395 passing (+3 regression tests). mypy + ruff clean.
