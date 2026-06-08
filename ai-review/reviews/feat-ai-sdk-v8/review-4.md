# Codex Review #4 — feat/ai-sdk-gateway-pr8 (Phase 1b)

> Reviewer: OpenAI Codex CLI. Base: `main`. Diff: through `51ba680c0` (R3 fixes).

## Summary

| Severity | P1 | P2 | P3 |
|---|---|---|---|
| count | 0 | 1 | 1 |

**GATE: PASS, no [P1] findings.** Both findings actionable; both fixed.

## Findings — verbatim

```
[P2] Release stream reservations only after upstream close — chat.py:264-268
  On client disconnects, this frees the node budget before stream_cm.__aexit__()
  has actually closed the Dify/vLLM stream. During that close window the
  upstream generation may still be holding KV cache, so another request can be
  admitted and exceed the budget the guard is meant to enforce. Move
  settle(...) after the upstream stream is closed, ideally in a nested
  finally so it still runs if close fails.

[P3] Admit before lazily building rejected chat apps — chat.py:176-180
  When the app cache is cold and the node budget is already full, this awaits
  get_app_key() and may log in/import/create a Dify App before the request is
  rejected by admission. That makes overload rejection depend on Dify console
  ...
```

## Fixes

### P2 — Streaming finally: close BEFORE settle, nested for safety
```python
finally:
    try:
        try:
            await stream_cm.__aexit__(None, None, None)
        except Exception:
            logger.exception("chat.stream_close_failed")
    finally:
        settle(request, grant, actual_output_tokens=0)
```
Now: upstream close happens first; settle runs in the outer `finally` so it
still releases the budget even if `__aexit__` raises. Closes the
read-this-window-and-double-admit race codex flagged.

### P3 — Reorder: cheap validation → admit → TPM → heavy lazy-build
```python
# 1. sync model validation (no network)
model_entry = customer.find_model(selected_model)
if model_entry is None: raise UnknownModelError(...)

# 2. sync body validation
query = _last_user_message(...); inputs = ...; user = ...

# 3. cost + admit (sync, may 503)
cost = estimate_request_cost(...)
grant = admit(request, customer, cost)

# 4. TPM (after admit, settle on rejection)
try: enforce_tpm(...)
except: settle+raise

# 5. HEAVY get_app_key (may lazy-build) — only after both gates pass
try: app_key = await app_manager.get_app_key(...)
except: settle+raise
```
Invalid model still 404s synchronously (R2 P2-1 preserved). Over-budget
now 503s without doing any Dify console_login / DSL import / api-key call.
Wrapped get_app_key in try/except settle so a build failure after admit
doesn't leak the reservation either.

## Tests (+2)
- `test_streaming_settles_after_upstream_close`: wraps `open_chat_stream`
  with a context-exit spy + monkeypatches `settle` to record call order;
  asserts `events == ["close", "settle"]` after a streamed request.
- `test_overload_503_does_not_call_get_app_key`: spies on
  `app_manager.get_app_key`; sends a request with `node_token_budget=1`
  → 503; asserts `call_count == 0`. Also verifies invalid model still
  404s (sync find_model preserved).

## Gate
**PASS.** Suite 401 passing, mypy + ruff clean.

## Pattern note

R4 P3 was a tension between two earlier fixes: R2 P2-1 said "validate model
before TPM" (we put `get_app_key` first); R4 says "don't do heavy lazy-build
before admit" (we put `get_app_key` last). Both are right — the resolution
was splitting validation from build. `customer.find_model` is the
sync validator; `get_app_key` is the heavy builder. Use the validator at
the validation site; defer the builder until after the gates. Two
different responsibilities had been collapsed into one call.
