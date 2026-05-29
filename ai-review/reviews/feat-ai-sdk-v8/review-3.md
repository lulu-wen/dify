# Codex Review #3 — feat/ai-sdk-gateway-pr8 (Phase 1b)

> Reviewer: OpenAI Codex CLI. Base: `main`. Diff: through `856af0c72` (review-2 fixes).

## Summary

Two more P2s, both second-order consequences of the review-2 fixes.

| Severity | P1 | P2 | P3 |
|---|---|---|---|
| count | 0 | 2 | 0 |

## Findings — verbatim

```
[P2] Avoid charging TPM before admission succeeds — routers/chat.py:211-212
  When TPM is enabled and the node budget is already full, enforce_tpm()
  consumes from the tenant's token bucket before admit() returns a 503. The
  rejected request did not run ... but the tenant can still be throttled on
  the next retry because there is no TPM refund path. ... consider admitting
  first with a guaranteed settle on later TPM rejection, or adding a
  refundable/peek path for TPM.

[P2] Treat invalid max_tokens values as uncapped — dify/app_manager.py:251-252
  If a registry entry contains completion_params: {max_tokens: null} or
  another non-positive/non-int value, this branch skips injecting the default
  cap because the key exists, while admission later falls back to
  default_max_output_tokens because the value is not an int. In that
  configuration the App can remain effectively unbounded even though the
  reservation assumes a bounded output ... validate/normalize max_tokens or
  inject the default unless it is a positive integer.
```

## Analysis + fixes

**P2-1 — TPM debited on admission failure.** TPM is a non-refundable
bucket consume. With `enforce_tpm` before `admit`, a node-full request
that passed TPM got its TPM debited then 503'd — throttling the tenant
for a request that never ran. **Fix:** admit-first, then TPM, and settle
the reservation if TPM rejects:
```
grant = admit(...)            # 503 here → TPM never touched
try:
    enforce_tpm(...)          # 429 here → release the reservation
except BaseException:
    settle(grant); raise
```
Net: TPM is debited only for requests that actually proceed to generation
(token-bucket rejections don't consume, so a TPM 429 doesn't debit either).

**P2-2 — invalid `max_tokens` defeats the cap.** My review-2 injection
used a bare `"max_tokens" not in params` check, but the reservation used
`isinstance(int)`. So `max_tokens: null` (or 0 / "1024" / true) → injection
skipped (key present) → App unbounded, while reservation fell back to the
default → under-reserve → OOM hole reopened. **Fix:** a single shared
`effective_max_tokens(completion_params) -> int | None` (positive int only,
bool excluded) used by BOTH the App-build injection AND the reservation, so
they can't disagree. Invalid/null/non-positive → both treat as uncapped →
both apply the default.

## Pattern note

Both findings are "the two halves of a decision used different predicates."
P2-2 especially: review-2 fixed the under-reservation but left the
*injection* and the *reservation* keyed on different validity checks. The
durable fix was one shared predicate (`effective_max_tokens`) — a single
source of truth for "what counts as a configured cap," so the App's actual
bound and the reserved bound are derived identically.

## Tests (+4)
- `test_node_full_503_does_not_debit_tpm`: spy limiter asserts TPM
  `.check` never fired when admission 503s.
- `test_tpm_rejection_after_admit_releases_reservation`: admit passes,
  TPM rejects → `in_flight == 0`.
- `test_null_max_tokens_injects_default_cap`: `max_tokens: null` → App DSL
  gets the injected default (not left unbounded).
- `test_effective_max_tokens_normalizes_invalid_values`: unit table
  (2048→2048; {}/null/0/-5/"1024"/True → None).

## Gate
**PASS after fix.** Suite 399 passing (+4). mypy + ruff clean.
