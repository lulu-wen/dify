# Codex Review #3 — feat-ai-sdk-v3

> Codex CLI v0.130.0, `model_reasoning_effort=high`.
> Base: `feat/ai-sdk-gateway-pr2` (PR #2 head).
> Diff: 13 commits including PR #3 features + 2 review rounds of fixes.
> Raw output: `review-3.raw.md`.

## Summary

Round-3 narrows down to a single remaining P2 — codex grep'd Dify's
Service API source and found two specific 4xx that the round-2
``_DATASET_CLIENT_STATUSES`` set doesn't cover but should:

- **415** `UnsupportedFileTypeError` (Dify rejects .exe / non-allowed MIME
  during create-by-file) — genuine client mistake
- **403** disabled-dataset / per-tenant quota refusals — client-actionable
  (caller can switch datasets or request a quota bump)

No P1 outstanding → **GATE: PASS**. After this commit the gateway has had
three full rounds of independent AI review with all findings resolved.

| Severity | Count |
|---|---|
| [P1] | 0 |
| [P2] | 1 |

## Full review comment

- [P2] **Pass through Dify file/client 4xx statuses** —
  `gateway/src/gateway/dify/client.py:53`

  Dify's Service API can return 415 for `UnsupportedFileTypeError`
  during `create-by-file` and 403 for disabled dataset API / quota
  checks in the dataset wrappers; because this set omits those
  statuses, `_raise_for_dify_status(..., pass_client_errors=True)`
  turns those client-actionable `/v1/files` / dataset failures into a
  502 `dify_upstream_error`. Include these statuses, or otherwise map
  them to client errors, so unsupported uploads and forbidden dataset
  access surface as the original 4xx.

## Gate

**PASS** (no [P1] findings). Fix applied in same round; PR #3 now
considered converged.
