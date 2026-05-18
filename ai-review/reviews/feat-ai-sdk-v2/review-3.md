# Codex Review #3 — feat-ai-sdk-v2

> Codex CLI v0.130.0, `model_reasoning_effort=high`.
> Base: `feat/ai-sdk-gateway-core` (PR #1).
> Diff: 9 commits, ~1300 LOC (R6 aliases + owned_by semantics + R1 embeddings +
> review-1 P1 fix + review-2 P2 #1 + review-2 P2 #2 + docs).
> Raw output: `review-3.raw.md`.

## Summary

Both findings are **second-order bugs introduced by the review-2 fixes** —
the 4xx passthrough is too aggressive (catches non-shape 4xx that are
actually upstream-side failures), and the success path doesn't validate
that the upstream body is JSON-shaped. Same pattern as PR #1 review-2:
fixing one class of error revealed neighbouring assumptions.

No P1 findings — round 3 converges with no critical regressions.

| Severity | Count |
|---|---|
| [P1] | 0 |
| [P2] | 2 |

> Note: codex's initial "Summary" section repeated the old review-1 P1
> wording (`test_models_endpoint.py:29` `all(...)` assertion). That was
> already fixed in commit `56de4890f` (the file now uses a per-id dict
> assertion). The two findings below are the real round-3 output.

## Full review comments

- [P2] **Do not classify every upstream 4xx as invalid input** —
  `gateway/src/gateway/embeddings/client.py:82-86`
  When the registered embedding service returns 401/403 because
  `model_entry.api_key` is wrong or expired (or 429 for upstream
  throttling), those statuses come from the gateway's upstream
  credential/service rather than from the SDK caller. This blanket branch
  wraps them as `invalid_request_error` / `upstream_invalid_request` and
  returns them to a caller with a valid SDK key, so auth/config failures
  and rate limits look like bad client input instead of upstream failures;
  only request-shape statuses such as 400/413/422 should take this path.

- [P2] **Convert malformed successful upstream bodies into upstream errors** —
  `gateway/src/gateway/embeddings/client.py:92`
  When a registered embedding backend responds with HTTP 2xx but non-JSON
  content (for example an HTML error page from a proxy), `resp.json()`
  raises here; similarly, a non-object JSON body later fails at
  `upstream_response["model"]`. Those exceptions are not `GatewayError`s,
  so this new endpoint returns an internal 500 instead of the gateway's
  upstream-error envelope for an upstream failure; parse/validate the
  successful response and translate malformed bodies into an upstream
  error before returning them.

## Gate

**PASS** (no [P1] findings).
