# Codex Review #1 — feat-ai-sdk-v3

> Codex CLI v0.130.0, `model_reasoning_effort=high`.
> Base: `feat/ai-sdk-gateway-pr2` (PR #2 head, commit `4eac47bc7`).
> Diff: 6 commits, ~2500 LOC (R7 streaming + R2/R5 datasets + R4 retrieve + R3 files + CI).
> Raw output: `review-1.raw.md`.

## Summary

Three real bugs touching the three biggest new surfaces. Pattern matches
prior rounds: each fix from a previous PR (4xx classification, response
shape handling) revealed a neighbouring code path that didn't apply the
same lesson.

| Severity | Count |
|---|---|
| [P1] | 1 |
| [P2] | 2 |

## Full review comments

- [P1] **Unwrap Dify upload responses before returning the file** —
  `gateway/src/gateway/routers/files.py:86`

  When Dify's create-by-file endpoint returns its normal v1.x shape
  `{"document": {...}, "batch": ...}`, this passes the outer envelope into
  `_to_file`, so `id` and `name` fall back to empty strings even though
  the document was created. Clients then cannot reliably poll or delete
  the uploaded file from the response; unwrap `dify_resp["document"]`
  here, similar to `_doc_id_from_response`.

- [P2] **Preserve Dify client errors for dataset APIs** —
  `gateway/src/gateway/dify/client.py:275`

  For new dataset/file operations, Dify can return expected 4xx responses
  such as duplicate dataset name, invalid API key, forbidden dataset, or
  dataset/document not found, but this helper always turns every non-2xx
  into `DifyUpstreamError` / 502. That makes client mistakes look like
  gateway outages and violates the intended 4xx surface for these
  endpoints; handle 4xx with the client-error path before the generic
  upstream-error mapping.

- [P2] **Emit only new agent thought text as streaming deltas** —
  `gateway/src/gateway/streaming/converter.py:116-119`

  Dify's `agent_thought` stream payload contains the persisted full
  `MessageAgentThought.thought` for that thought id, and subsequent
  events for the same id are cumulative after appends / tool updates.
  OpenAI clients concatenate `delta.reasoning_content`, so forwarding
  the whole `thought` each time duplicates prefixes (for example `"foo"`
  then `"foobar"` renders as `"foofoobar"`); track the previous thought
  per event id and emit only the suffix.

## Gate

**FAIL** (1 × [P1]). All three findings will be fixed before review #2.
