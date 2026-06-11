# Self-review #1 — feat/ai-sdk-gateway-pr10 (Phase 2b)

> Reviewer: self. Base: `feat/ai-sdk-gateway-pr8` (PR #8 + R5 fix). Diff: PR #10 only.

## What PR #10 does

Best-effort cancel of in-flight Dify/vLLM generation when the receiver is
gone. Once the client disconnects (or the upstream errors mid-stream),
every further generated token is pure waste — independent of node KV
pressure, so decoupled from PR #9's headroom-driven admission (per user
feedback). Fires a single fire-and-forget POST to Dify's
`/v1/chat-messages/{task_id}/stop`, which propagates the cancel down to
vLLM and frees KV cache immediately.

## Implementation

### `DifyClient.chat_messages_stop(app_key, task_id, user)`
- Standard Bearer-auth POST; body `{"user": ...}`.
- Errors are SWALLOWED (logged at info/warning, not raised). Callers can
  fire-and-forget without wrapping; 404 on already-finalized tasks is the
  common case and is expected, not an error.

### `dify_to_openai_chunks(..., cancel_sink=...)`
Side-channel mutable dict. Two fields:
- `cancel_sink["task_id"]` — first non-empty Dify `task_id` seen
  (write-once; ping events also carry it so we typically capture early).
- `cancel_sink["dify_finalized"]` — set True when `message_end` OR `error`
  event arrives (Dify-side termination signals). Critical because the
  outer `async for chunk in dify_to_openai_chunks(...)` loop exits CLEANLY
  on `GeneratorExit` (Python `async for` aclose's the inner generator
  rather than propagating the exit), so the router can NOT use
  "loop completed" as a proxy for "Dify finished."

### `chat.py` streaming finally
Existing order preserved (R4 P2: close upstream → settle), with a new
PR #10 step *before* the close:

```python
finally:
    tid = cancel_sink["task_id"]
    if tid and not cancel_sink["dify_finalized"]:
        _fire_and_forget(dify_client.chat_messages_stop(...))
    try:
        try: await stream_cm.__aexit__(None, None, None)
        except Exception: logger.exception(...)
    finally:
        settle(request, grant, actual_output_tokens=0)
```

`_fire_and_forget` keeps a module-level set reference so the GC can't
drop the task; the task self-removes via `done_callback`.

### Bonus: graceful upstream error mid-stream
Pre-existing latent bug surfaced by the PR #10 tests: a
`DifyUpstreamError` raised mid-stream after headers were flushed gave a
"Caught handled exception, but response already started" crash at the
Starlette layer. event_source now catches it, logs, and emits
`data: [DONE]\n\n` so the client gets a clean stream end. cancel_sink
state at that moment reflects exactly how far we got — task_id captured
+ not finalized → cancel fires for the abandoned upstream task.

## Tests (+8)

Integration via httpx ASGITransport — note that ASGITransport buffers the
response and does NOT propagate `http.disconnect` even when the client
breaks out of `aiter_lines`. We exercise the same router code path via
upstream-error simulation (FakeDifyClient.streaming_raise_after_n_lines +
streaming_raise_exception). Production client disconnect hits the
identical finally — Starlette's real receive task signals
`http.disconnect`, raises `GeneratorExit` in body_iterator, our finally
runs.

- `test_natural_completion_does_not_cancel` — `message_end` seen → no stop call.
- `test_dify_error_event_does_not_cancel` — `error` event also marks
  finalized → no stop call.
- `test_upstream_drop_mid_stream_fires_cancel` — drop after first event
  with task_id → one stop call carrying the captured task_id + user.
- `test_upstream_drop_before_task_id_skips_cancel` — drop before any
  event-with-task_id → `if tid:` guard prevents an empty-task_id POST.
- `test_cancel_does_not_block_settle` — stop endpoint sleeps 500ms;
  asserts settle releases reservation immediately (< 100ms after
  finally), then the stop event still fires eventually (proves
  fire-and-forget shape works).
- Three converter unit tests pinning the `cancel_sink` writes — first
  event captures task_id, `message_end`/`error` set `dify_finalized`,
  ping-only stays None/False.

## Self-review findings

### What codex might flag

- **`except Exception` too broad**: would swallow a TypeError in our
  own converter code. Tradeoff — mid-stream re-raise gives "response
  already started" + crashes the request. Keeping the catch but logging
  at `warning` (not info) so real bugs are visible.
- **`_bg_cancel_tasks` is module-level mutable state**: testing across
  multiple `app` fixtures shares the set. Each task self-removes on
  done, so it's bounded by concurrency, not accumulated. Fine.
- **What if `user` differs between chat-messages-streaming and
  chat-messages-stop?** Same `_user_id(body, customer, request_id)`
  call — closure captures the same value. No drift.
- **Multi-worker**: process-local _bg_cancel_tasks is fine (no shared
  state needed). Each worker's disconnects fire its own cancels.

### Not in scope (deferred)

- Live RuntimeMetrics-driven dynamic budget — PR #9 (Phase 2a).
- Bounded priority wait-queue — Phase 3 (needs policy engine).
- Reserved control-plane class — Phase 3 (same dependency).

## Gate

**PASS.** 413 passing, mypy strict clean, ruff clean.

## Pattern

R4 (close-before-settle) → R10-style fire-and-forget: when teardown has
multiple steps with different blocking properties, decouple them via
fire-and-forget so the cheap synchronous-from-our-side step (settle,
release reservation) doesn't wait on the I/O-bound step (cancel POST).
The `cancel_sink` mutable side-channel mirrors the `task_id_sink`
pattern from earlier — keeping the async iterator's shape unchanged for
non-cancelling callers (tests, blocking path) while plumbing cancel-only
state through.
