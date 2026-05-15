# Codex Review #1 — feat-ai-sdk-v2

> Codex CLI v0.130.0, `model_reasoning_effort=high`.
> Base: `feat/ai-sdk-gateway-core` (PR #1).
> Diff: 5 commits, ~1100 LOC (R6 aliases + owned_by semantics + R1 embeddings).

## Summary

The updated default test fixture now includes an embedding model with a
non-default owner, but the modified models endpoint test still asserts that
every returned model uses the default owner. This makes the test suite fail
deterministically.

## Full review comments

- [P1] Exclude explicit embedding owners from the default-owner assertion —
  `gateway/tests/test_models_endpoint.py:29`
  With the updated fixture, `make_customer()` now registers `emb1` by default
  with `owner="TestPublisher"`, so `/v1/models` includes that embedding row
  and this `all(...)` assertion fails on every run. Restrict the assertion
  to the LLM rows or update the expectation to account for the explicitly
  owned embedding model.
