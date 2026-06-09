# Self-Review #1 — feat/ai-sdk-gateway-pr8 (Phase 1b)

> Reviewer: Claude (self-review before codex). Base: `main` (post PR #11 / 1a).
> Scope: Phase 1b — cost-based node admission + TPM metering + pre-charge/refund.
> Builds on the 1a seams (RateLimiter Protocol, token bucket, middleware, errors action).

## Summary

| Severity | Found | Fixed | Deferred |
|---|---|---|---|
| [P1] | 0 | — | — |
| [P2] | 2 | 1 | 1 (doc-only) |
| [P3] | 4 | 0 | 4 |

The riskiest surface is the chat router hot path (admission reservation +
settle across streaming / disconnect / error). Self-review found one real
leak bug there and fixed it.

## Findings

### [P2] Reservation leak: admit ran before a can-raise validation — FIXED
`admit()` was placed right after `get_app_key`, but `_last_user_message()`
(which raises `InvalidRequestError` for a body with no user message) ran
*after* it. A no-user-message request would reserve node budget and then
400 without ever settling → the reservation leaks until process restart,
permanently shrinking the node's effective budget.
**Fixed:** moved `admit()` below all can-raise local prep (query / inputs /
user), so the only failure window between admit and settle is the
generation call itself (stream pre-flight + blocking — both settle on
error). Added regression test
`test_local_rejection_after_admit_window_does_not_leak` (no-user-message →
400, `in_flight == 0`).

### [P2] Multi-worker node budget multiplies — doc-only, deferred
`InMemoryQuotaStore` is per-process, same as the 1a token bucket. Under N
uvicorn workers the effective node budget is `N × node_token_budget`, so
the OOM guard is looser than configured when scaled out. Correct for the
single-Jetson / single-worker target; Redis-backed `QuotaStore` (same
Protocol) is the Phase 4 migration. Documented in `quota.py` + design doc.

### [P3] Streaming settle passes actual_output_tokens=0 — deferred
The streaming path doesn't extract Dify usage, so settle logs 0 actual
tokens. **Budget correctness is unaffected** — settle releases the full
reservation regardless (the request is done); actual is telemetry only.
Precise streaming token telemetry would mean threading usage out of the
SSE converter. Deferred; noted in the chat router.

### [P3] X-RateLimit-Reset still not emitted — deferred (carried from 1a)
1b adds TPM/admission errors but still doesn't emit `X-RateLimit-Reset`.
Low value; deferred again.

### [P3] cost estimate under-counts CJK — accepted
chars/4 under-counts CJK input tokens (CJK is ~1-2 chars/token). Makes the
estimate less conservative on CJK-heavy prompts; `max_output_tokens` (the
larger term for short prompts) is exact. Documented in `cost.py`. Accepted
for MVP; revisit if CJK RAG contexts dominate.

### [P3] TPM over-charges unused max_output — accepted (by decision)
TPM pre-charges `input + max_output` and does not refund the unused
portion (decision: keep conservative + simple). A request that asks for
4096 max but emits 100 is metered at the worst case. Accepted; the
optional refund-into-bucket path is noted in the design doc B.4.

## What I checked and found clean
- **Settle covers every admit path**: streaming normal completion + client
  disconnect (GeneratorExit reaches the event_source finally) + stream
  pre-flight failure (explicit settle+raise) + blocking success + blocking
  error (try/except settle+raise). All e2e-tested incl. disconnect.
- **settle idempotency**: pre-flight-failure and the finally can both fire;
  `InMemoryQuotaStore.settle` pops-or-noop, unit-tested for double-settle.
- **Order**: TPM (cheap, local, no reservation) before `get_app_key`
  (network/model-validation) before admit (reserve). Invalid model 404s
  without reserving.
- **Disabled bypass**: `rate_limit_enabled=false` → `admit` returns a
  no-charge grant (charge_id None), `settle` is a no-op — callers don't
  branch. e2e-tested.
- **Embeddings**: TPM only (no admission) — documented decision (not a
  KV-cache OOM risk); `max_output=0` so cost is input-only (no fallback).
- **errors**: OverloadError (503) distinct from RateLimitError (429);
  envelope stays OpenAI-shaped (action only when set); existing error tests
  unaffected.
- **No regressions**: full suite 392 pass (+22 1b), mypy strict + ruff clean.

## Gate
Self-review **PASS** with 1 fix applied (admit-ordering leak). Ready for codex.
Expected codex axes: the streaming settle/cancellation story (we release
budget on disconnect but don't yet cancel the upstream Dify/vLLM generation
— design doc flags vLLM cancellation as the real fix, Phase 2+);
admit/settle race correctness on the event loop; and whether TPM
pre-charge-no-refund interacts surprisingly with the RPM bucket.
