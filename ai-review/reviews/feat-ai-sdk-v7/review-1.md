# Self-Review #1 — feat/ai-sdk-gateway-pr7 (Phase 1a)

> Reviewer: Claude (self-review before codex). Base: `main` (post PR #6 / GitHub #10).
> Scope: Phase 1a only — per-tenant RPM token bucket in middleware.
> Commit under review: `f270be783` + this round's fixes.

## Summary

| Severity | Found | Fixed | Deferred |
|---|---|---|---|
| [P1] | 0 | — | — |
| [P2] | 1 | 0 | 1 (doc-only) |
| [P3] | 5 | 2 | 3 |

Phase 1a is small and the algorithm is standard, so most findings are
forward-looking (1b footguns) or documented limitations rather than
bugs. No correctness defect in the 1a happy path.

## Findings

### [P2] Multi-worker effective limit multiplies — doc-only, deferred
`InMemoryTokenBucketLimiter` state is per-process. Under N uvicorn
workers (or N replicas) each process keeps its own bucket, so the
*effective* per-customer limit is `N x rpm`. For the single-Jetson /
single-worker target this is correct; for any scaled-out deploy it
silently over-admits.

**Decision: deferred, documented.** Called out in the limiter docstring
and the design doc. The migration path is a Redis-backed `RateLimiter`
(same Protocol, no caller change) — that's explicitly Phase 4
(distributed). Filing as P2 because it's a real operational gotcha an
operator must know before scaling horizontally, not because 1a is wrong.

### [P3] `units_per_min <= 0` would divide by zero — FIXED
The limiter is a reusable component; callers enforce `>= 1`
(config `ge=1`, `rpm_limit gt=0`), but a future caller passing 0 would
hit `deficit / refill_per_s` with `refill_per_s == 0`.
**Fixed:** guard `refill_per_s` and return `retry_after_s=None` ("never
refills") for the degenerate case instead of crashing the request.

### [P3] `cost > burst` is never satisfiable but returns a finite retry — deferred (1b)
For 1a `cost=1` and `burst >= 1`, so unreachable. In 1b, TPM cost is
the token count, which can exceed `burst`; the bucket caps at `burst`,
so even a full bucket can't admit it, yet `retry_after_s` would suggest
a finite wait that never helps.
**Decision: deferred to 1b** — when TPM lands, either clamp/validate
`burst >= max single request cost` at config time, or have the limiter
signal "unsatisfiable" distinctly. Noted so 1b doesn't reintroduce it.

### [P3] Router-raised `RateLimitError` (1b) won't carry jitter — deferred (1b)
Jitter is added in `RateLimitMiddleware`. When 1b raises
`RateLimitError` / `OverloadError` from a router (TPM / admission),
those go through the global exception handler, which ceils
`retry_after_s` but adds no jitter — so router-level 429/503s could
re-synchronize a retry storm.
**Decision: deferred to 1b** — move jitter into a shared helper both the
middleware and the routers use, or apply it in the exception handler.

### [P3] `X-RateLimit-Reset` not emitted — deferred
Spec listed Limit/Remaining/Reset; I shipped Limit + Remaining. Reset
(seconds until the bucket is full again) is a nice client signal but
needs a small extra computation. Low value for 1a; revisit if clients
ask. Deferred.

### [P3] Idle buckets are never evicted — accepted, no fix
`_buckets` keeps one entry per `customer_id:rpm` key forever. Bounded by
registry size (≈100 customers) and cleared on restart, so unbounded
growth isn't a practical risk. A removed customer leaves a stale bucket
until restart — harmless. **Accepted as-is**; revisit only if the
registry ever becomes unbounded/dynamic.

## What I checked and found clean
- Middleware ordering: Logging → Auth → RateLimit → route; RateLimit
  reads `request.state.customer` that Auth sets (verified via the
  `add_middleware` add-order = innermost-first rule).
- Renders 429 directly (runs outside the exception handler, like Auth) —
  no accidental 500.
- Monotonic clock (no wall-clock); rejection doesn't consume; new key
  starts full; refill caps at burst — all unit-tested with a fake clock.
- `to_openai_envelope` stays OpenAI-shaped (action only when set);
  existing error tests unaffected (full suite green).
- `rate_limit_enabled=false` and exempt paths are true pass-throughs
  (no headers, no metering) — tested.
- Per-customer `rpm_limit` override beats the default — tested via a
  recording limiter.

## Gate
Self-review **PASS** with 1 fix applied (units guard). Ready for codex.
Expected codex axes: streaming-response header mutation under
BaseHTTPMiddleware, fail-open vs fail-closed when a future limiter
raises, and whether RPM-only (no TPM) is a meaningful gate on an
edge node where the real bottleneck is tokens/KV-cache (the design
doc's answer: RPM is the cheap first gate; TPM/admission is 1b).
