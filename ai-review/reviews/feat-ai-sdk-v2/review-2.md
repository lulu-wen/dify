# Codex Review #2 — feat-ai-sdk-v2

> Codex CLI v0.130.0, `model_reasoning_effort=high`.
> Base: `feat/ai-sdk-gateway-core` (PR #1).
> Diff: 6 commits, ~1240 LOC (R6 aliases + owned_by semantics + R1 embeddings + R1
> P1 review fix).
> Raw output: `review-2.raw.md`.

## Summary

Two correctness issues that affect real customer configurations and real upstream
behaviour, but neither is a blocking bug. No P1 findings — round 2 converges with
no critical regressions on top of round 1's test-fixture fix.

| Severity | Count |
|---|---|
| [P1] | 0 |
| [P2] | 2 |

## Full review comments

- [P2] **Preserve embedding upstream 4xx responses** —
  `gateway/src/gateway/embeddings/client.py:72-80`
  When a request passes gateway validation but the embedding backend rejects it as
  a client error — for example, a positive `dimensions` value on a model that does
  not support truncation, or an input that exceeds the backend's token limit —
  OpenAI-compatible upstreams return 4xx responses. This branch converts every
  non-2xx response into `DifyUpstreamError`, so those client mistakes are reported
  as 502 `dify_upstream_error` responses instead of the upstream 4xx /
  invalid-request response. This breaks proxy semantics for valid client inputs
  the gateway cannot fully validate itself.

- [P2] **Reject IDs shared by chat and embedding models** —
  `gateway/src/gateway/registry.py:127-135`
  When a customer configures an LLM and an embedding model with the same
  customer-facing `id`, both per-list validators pass, even though `/v1/models`
  now flattens the two lists into one response. That produces duplicate OpenAI
  model IDs in the advertised list and violates this module's existing invariant
  that model IDs are unique within a customer, so registry validation should also
  reject cross-list collisions.

## Gate

**PASS** (no [P1] findings).
