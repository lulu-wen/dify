# PR #2 Implementation Record — `feat/ai-sdk-gateway-pr2`

> Companion to `ai-review/specs/feat-ai-sdk-v2.md`. Read this if you want to
> understand **what shipped in PR #2, where it lives, and what each test is
> actually verifying** without spelunking the whole diff.

## 1. Scope shipped in PR #2

| Spec ID | Title | Status |
|---|---|---|
| **R1** | `POST /v1/embeddings` (OpenAI-compatible) | ✅ shipped |
| **R6** | OpenAI 2025 deprecation aliases (`max_completion_tokens`, `safety_identifier`) | ✅ shipped |
| — | `/v1/models` `owned_by` → publisher identity (tenant-leak fix) | ✅ shipped |
| — | Upstream 4xx passthrough (`UpstreamClientError`) — codex round-2 [P2] | ✅ shipped |
| — | Cross-list id collision validator — codex round-2 [P2] | ✅ shipped |
| **R2** | `/v1/datasets` (knowledge base CRUD) | ⏳ deferred to PR #3 |
| **R3** | `/v1/files` (RAG document upload) | ⏳ deferred |
| **R4** | `POST /v1/datasets/{id}/retrieve` | ⏳ deferred |
| **R5** | Embedding model lazy-provisioning | ⏳ deferred (registry-side already ready) |
| **R7** | Streaming `reasoning_content` (Qwen3 thinking) | ⏳ deferred |

PR #2 deliberately stays small: it locks down the **embeddings** surface and
the **alias schema** so the rest of PR #3 (RAG) can build on a stable
OpenAI-compatible footprint without continually breaking client SDK calls.

Diff against PR #1 base (`feat/ai-sdk-gateway-core`):

| Commit | Subject |
|---|---|
| `bdc4424f2` | chore: add PR #2 spec (knowledge base + OpenAI aliases) |
| `49e95be2c` | feat(gateway): accept OpenAI 2025 deprecation aliases (R6) |
| `1a88a929e` | fix(gateway): `/v1/models` `owned_by` is publisher identity, not tenant id |
| `60a75b731` | fix(gateway): `/v1/models` `owned_by` uses upstream publisher identity |
| `2430000a2` | feat(gateway): `/v1/embeddings` endpoint + registry support (R1) |
| `56de4890f` | fix(tests): pin per-id owner in `/v1/models` test (review-1 P1) + docs |
| (next) | fix(gateway): pass upstream embedding 4xx through as client error (review-2 P2 #1) |
| (next) | fix(registry): reject id collisions across LLM and embedding lists (review-2 P2 #2) |

Total post-review-2: 8 commits, 21 files. PR #2 has gone through 2 codex
review rounds with zero P1 findings outstanding and all P2 findings fixed.

---

## 2. Feature 1 — `/v1/embeddings` (R1)

### 2.1 Why we built it

Customers want to use the **same SDK key, same base URL** for both chat and
vectorisation, so the AI SDK is one unit. The OpenAI Python SDK already
exposes `client.embeddings.create(...)`; the gateway just has to honour the
OpenAI surface and proxy the bytes.

### 2.2 Architectural choice — Dify is bypassed

Embeddings go **straight to the OpenAI-compatible upstream** (typically
vLLM running with `--task embed`). Dify is not in the path:

```
client → Gateway (auth + routing) → vllm-embed (OpenAI-compat) → vectors
```

Reasoning (codified in `routers/embeddings.py:1-29` and `embeddings/client.py:1-12`):

- Embeddings have no prompt, no RAG, no agent loop, no conversation state.
- Dify would only add a hop of latency and an extra failure surface.
- Per-customer routing + auth + logging is still done at the Gateway choke point.

### 2.3 Files added / changed

| File | Role |
|---|---|
| `gateway/src/gateway/embeddings/__init__.py` | New package marker. |
| `gateway/src/gateway/embeddings/client.py` | Async `httpx` caller (`invoke_embeddings`). Translates upstream errors → `DifyTimeoutError` / `DifyUpstreamError` so the existing `GatewayError` handler produces a consistent OpenAI envelope. |
| `gateway/src/gateway/routers/embeddings.py` | The `POST /v1/embeddings` route. Resolves customer's embedding model from registry, builds upstream body, echoes customer-facing `model` id back. |
| `gateway/src/gateway/registry.py` | Adds `EmbeddingModelEntry` + `CustomerEntry.embedding_models` + `find_embedding_model()`. |
| `gateway/src/gateway/schemas.py` | Adds `EmbeddingsRequest`, `EmbeddingData`, `EmbeddingsUsage`, `EmbeddingsResponse`. |
| `gateway/src/gateway/main.py` | Wires the new router into the FastAPI app. |
| `gateway/src/gateway/routers/models.py` | `/v1/models` now lists LLM **and** embedding entries in one flat array (OpenAI is type-agnostic). |
| `gateway/registry.example.yaml` | Documents the `embedding_models:` block + `owner:` field. |

### 2.4 Request flow (line-by-line)

`routers/embeddings.py:49-90`:

1. `body: EmbeddingsRequest` — Pydantic validates the OpenAI shape.
2. `customer = request.state.customer` — populated by the auth middleware (PR #1).
3. `_resolve_model(customer, body.model)` — returns the `EmbeddingModelEntry`
   or raises `UnknownModelError` (→ HTTP 404 `model_not_found`, same envelope
   chat uses for unknown models).
4. Build `upstream_body`:
   - `model` is rewritten to `model_entry.name` (upstream's served name) —
     the customer-facing id and the upstream-served name can differ.
   - `input`, `encoding_format`, `dimensions` forward as-is.
   - `user` falls through R6 precedence (`safety_identifier > user`) and
     defaults to `<customer_id>:<request_id>` so the upstream always sees a
     stable identifier (matches the chat router's behaviour).
5. `invoke_embeddings(...)` posts to `<endpoint_url>/embeddings`.
6. Response's `model` is rewritten back to the customer-facing id before
   returning — clients see what they sent.

### 2.5 Registry surface

`EmbeddingModelEntry` (`registry.py:74-104`):

```yaml
embedding_models:
  - id: "bge-m3"                                  # customer-facing
    name: "bge-m3"                                # upstream served name
    owner: "BAAI"                                 # /v1/models owned_by
    endpoint_url: "http://localhost:9997/v1"      # OpenAI-compat base URL
    api_key: "EMPTY"                              # vLLM-friendly default
    dimensions: 1024                              # informational
```

Frozen Pydantic model with `extra="forbid"` — typos like `endpiont_url` fail
loud at startup, not silently at request time.

---

## 3. Feature 2 — OpenAI 2025 alias support (R6)

### 3.1 Why

OpenAI deprecated:

| Old | New (2025+) |
|---|---|
| `max_tokens` | `max_completion_tokens` |
| `user` | `safety_identifier` |

Older SDKs still send the old names; newer ones send the new ones; some
emit both during migration. We accept **both**, with the new name winning
when both are sent (matches OpenAI's migration direction).

### 3.2 Implementation

`schemas.py:38-91`:

```python
class ChatCompletionRequest(BaseModel):
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    user: str | None = Field(default=None)
    safety_identifier: str | None = Field(default=None)

    @property
    def effective_max_tokens(self) -> int | None:
        return self.max_completion_tokens if self.max_completion_tokens is not None else self.max_tokens

    @property
    def effective_user(self) -> str | None:
        return self.safety_identifier if self.safety_identifier is not None else self.user
```

The router (`routers/chat.py:79-91`) calls `req.effective_user` instead of
`req.user`. The same precedence is mirrored on `EmbeddingsRequest`
(`schemas.py:219-253`) so the embeddings router can share the rule.

Downstream Dify / vLLM only know the old field name (`user`), so the router
forwards `effective_user` **as `user`** — clients see the new contract,
servers see what they understand.

---

## 4. Feature 3 — `/v1/models` `owned_by` fix

### 4.1 The bug we found

PR #1 wrote `owned_by = customer.customer_id`. This is a **cross-tenant
information leak**: a customer can call `GET /v1/models` with their SDK
key and learn the **customer_id** the gateway uses for them internally.

OpenAI's `owned_by` means **who published the underlying model**:

```
gpt-4               → "openai"
Qwen3.6-35B         → "Qwen"
bge-m3              → "BAAI"
customer fine-tune  → "org-<customer>"   (only if scoped)
```

### 4.2 Fix

`registry.py:59` — `ModelEntry.owner: str = Field(default="ai-sdk-gateway", min_length=1)`
`registry.py:101` — same on `EmbeddingModelEntry`.
`routers/models.py:38-44`:

```python
entries = [ModelInfo(id=m.id, owned_by=m.owner) for m in customer.models]
entries.extend(ModelInfo(id=e.id, owned_by=e.owner) for e in customer.embedding_models)
```

Default falls back to `"ai-sdk-gateway"` (not to a tenant id) when the
registry doesn't declare an `owner`. There is no code path left that puts
`customer_id` into a `owned_by` field.

---

## 5. Test inventory — what each test verifies

The full test suite (`pytest gateway/tests/`) has 60 tests. Below is the
PR #2-relevant subset, grouped by the feature each test gates.

### 5.1 `gateway/tests/test_embeddings.py` — R1 surface (11 tests)

| Test | What it asserts about R1 |
|---|---|
| `test_embeddings_single_string_input` | OpenAI shape works for the single-string `input` case. Also verifies wire-level details: `Authorization: Bearer EMPTY` sent upstream, upstream sees the upstream-served `model` name (`upstream-emb1`), response echoes the customer-facing id (`emb1`), `usage.prompt_tokens` surfaces from upstream. |
| `test_embeddings_list_input` | OpenAI shape works for list `input`. Response `data` length matches input list length and `index` field is preserved per element. |
| `test_embeddings_forwards_optional_params` | `encoding_format` and `dimensions` are not eaten by the gateway — they pass through to the upstream body. |
| `test_embeddings_safety_identifier_preferred` | R6 alias precedence on the embeddings endpoint: when both `user` and `safety_identifier` are sent, the upstream receives `safety_identifier` as `user`. |
| `test_embeddings_user_fallback_to_customer_request` | When neither `user` nor `safety_identifier` is supplied, the gateway synthesises `<customer_id>:<request_id>` and forwards it. Guarantees the upstream always sees a stable id (Dify-style). |
| `test_embeddings_unknown_model_returns_404` | Unknown embedding model id → HTTP 404 with `error.code = "model_not_found"`. Same envelope as chat — clients can write one error handler. |
| `test_embeddings_missing_auth_returns_401` | No `Authorization` header → 401. Auth middleware applies to the new route. |
| `test_embeddings_chat_model_id_not_treated_as_embedding` | A model id registered as an LLM (`m1`) must NOT be valid on `/v1/embeddings` — the two namespaces are distinct. Prevents accidental cross-resolution. |
| `test_embeddings_upstream_5xx_returns_502` | Upstream returns 503 → gateway returns 502 with `error.code = "dify_upstream_error"`. (See codex review-2 [P2] — this is the behaviour codex flagged as too coarse for 4xx upstream errors; PR #3 will narrow it.) |
| `test_embeddings_upstream_timeout_returns_504` | Upstream `httpx.TimeoutException` → 504 `dify_timeout`. |
| `test_models_endpoint_includes_embedding_models` | The `/v1/models` list flattens LLM + embedding entries; embedding row's `owned_by` reflects the registry-declared publisher (`BAAI` in the fixture). |

Mocking strategy: `respx` intercepts the upstream HTTP call. Tests exercise
the real `httpx` request construction — closer to a true integration test
than mocking `invoke_embeddings`.

### 5.2 `gateway/tests/test_schemas.py` — R6 alias logic (8 tests)

Pure unit tests on the Pydantic model — no HTTP.

| Test | What it asserts about R6 |
|---|---|
| `TestEffectiveMaxTokens::test_only_old_field_returns_old` | `max_tokens=512` alone → `effective_max_tokens == 512` |
| `TestEffectiveMaxTokens::test_only_new_field_returns_new` | `max_completion_tokens=256` alone → 256 |
| `TestEffectiveMaxTokens::test_both_set_new_wins` | Both set → new wins (`max_completion_tokens`) |
| `TestEffectiveMaxTokens::test_neither_set_returns_none` | Neither set → `None` |
| `TestEffectiveUser::test_only_user_returns_user` | `user="alice"` alone → `"alice"` |
| `TestEffectiveUser::test_only_safety_identifier_returns_safety_identifier` | `safety_identifier="bob"` alone → `"bob"` |
| `TestEffectiveUser::test_both_set_safety_identifier_wins` | Both set → new wins (`safety_identifier`) |
| `TestEffectiveUser::test_neither_set_returns_none` | Neither set → `None` |

### 5.3 `gateway/tests/test_chat_blocking.py` — R6 wiring through chat (new tests added in PR #2)

The chat router was already tested in PR #1; PR #2 added the alias-specific
tests below. They verify the alias property reaches Dify correctly.

| Test | What it asserts about R6 on the chat path |
|---|---|
| `test_safety_identifier_preferred_over_user` | When both fields are in the request body, the Dify call receives `user = "new-style-user-id"`. End-to-end precedence proof. |
| `test_user_field_alone_still_accepted` | Backwards compat: old SDK clients sending only `user` still work. Dify call sees that value. |
| `test_max_completion_tokens_accepted` | New field name does not trip Pydantic validation (i.e. it's not an `extra_forbidden` reject; it's a declared field). Returns 200. |
| `test_both_max_tokens_fields_accepted_together` | Both fields can be sent on the same request without 400. |

### 5.4 `gateway/tests/test_registry.py` — `EmbeddingModelEntry` validation (5 new tests under `TestCustomerEntryValidation` + 4 under `TestEmbeddingModelEntry`)

| Test | What it asserts about R1's registry surface |
|---|---|
| `test_embedding_models_default_to_empty_list` | Customer without `embedding_models:` block in YAML still loads — PR #1 registries are backwards-compatible. `find_embedding_model("anything") is None`. |
| `test_embedding_model_lookup` | `find_embedding_model("bge-m3")` returns the entry; unknown id returns `None`. |
| `test_duplicate_embedding_model_ids_rejected` | Two entries with `id="dup"` in `embedding_models` → `ValueError("embedding model ids must be unique")` at registry load. (Codex review-2 [P2] flags that cross-list collisions — same id in `models` and `embedding_models` — are not yet rejected. PR #3.) |
| `TestEmbeddingModelEntry::test_defaults` | Unset `owner` → `"ai-sdk-gateway"`; unset `api_key` → `"EMPTY"`; unset `dimensions` → `None`. |
| `TestEmbeddingModelEntry::test_owner_can_be_publisher` | `owner="BAAI"` round-trips. |
| `TestEmbeddingModelEntry::test_extra_fields_forbidden` | Typo `typo_field="oops"` → `ValueError`. Guards against config typos becoming silent failures. |
| `TestEmbeddingModelEntry::test_dimensions_must_be_positive` | `dimensions=0` → `ValueError`. Pydantic's `gt=0`. |

### 5.5 `gateway/tests/test_models_endpoint.py` — `owned_by` semantics

| Test | What it asserts |
|---|---|
| `test_models_endpoint_returns_customer_models` | Per-id owner pins: `m1`, `m2` (LLM, no `owner` set) → `"ai-sdk-gateway"`. `emb1` (embedding with explicit `owner="TestPublisher"`) → `"TestPublisher"`. This is the **strictened** assertion from review-1 P1 — was `all(... == "ai-sdk-gateway")` which broke when the fixture added a non-default-owner embedding. |
| `test_models_endpoint_owned_by_does_not_leak_customer_id` | Regression test for the tenant-leak: no `owned_by` value equals `"test-a"` (the customer id used in the fixture). Will catch any future code path that re-introduces the leak. |
| `test_model_entry_owner_defaults_to_gateway` | `ModelEntry` unit: `owner` defaults to `"ai-sdk-gateway"` when unspecified. |
| `test_model_entry_owner_can_be_overridden` | `ModelEntry(owner="Qwen")` round-trips. |
| `test_models_endpoint_requires_auth` | (unchanged from PR #1) 401 without auth. |
| `test_health_endpoint_does_not_require_auth` | (unchanged) `/health` is public. |

### 5.6 `gateway/tests/conftest.py` — fixture changes

`make_customer()` now seeds:
- LLMs: `m1`, `m2` (no `owner` — default fallback).
- Embedding: `emb1` with `owner="TestPublisher"` and an endpoint targeting
  the `respx` mock base (`http://embed.test/v1`).

This is what made the original `all(... == "ai-sdk-gateway")` assertion in
`test_models_endpoint.py` fail in codex review #1 — the fix was to assert
per-id rather than globally.

---

## 6. Codex review history

PR #2 went through 2 rounds of independent codex review.

| Round | Findings | Action |
|---|---|---|
| **Round 1** | 1 × [P1] — test fixture drift in `test_models_endpoint.py` (the `all(...)` assertion broke after the fixture added an embedding with non-default `owner`). | Fixed in commit `56de4890f`. See `review-1-response.md`. |
| **Round 2** | 0 × [P1], 2 × [P2] — (1) embedding upstream 4xx coerced to 502, (2) cross-list id collision between `models` and `embedding_models` not rejected. | Both fixed in 2 separate follow-up commits. New tests added: 3 parametrized 4xx-passthrough + 2 cross-list collision tests. `pytest gateway/tests/`: **129 PASSED**. See `review-2.md` + `review-2-response.md`. **GATE: PASS**. |

**Process lesson** (already saved as a feedback memory after round 1):
> AST validity ≠ test correctness. Run `pytest` before pushing for codex review;
> AST-only verification catches typos but not cross-file state drift.

---

## 7. End-to-end verification on Jetson (2026-05-16)

Run on production-shape hardware (NVIDIA Jetson AGX Thor, Gemma 3n E4B as
LLM, bge-m3 as embedding) before opening PR:

| Endpoint | Status | Notes |
|---|---|---|
| `GET /health` | ✅ `{"status":"ok"}` | Gateway + auth middleware up. |
| `GET /v1/models` | ✅ | Lists `gemma-3n-e4b` (`owned_by: "google"`) + `bge-m3` (`owned_by: "BAAI"`). Confirms PR #2 owner field + embedding entry surfacing. |
| `POST /v1/embeddings` | ✅ | Single-string input → `dim=1024` vector, `model=bge-m3` echoed, `usage.prompt_tokens > 0`. Gateway → vllm-embed direct path verified. |
| `POST /v1/chat/completions` | ✅ | Non-streaming: 26 prompt tokens → 817 completion tokens of Gemma output. Full chain Gateway → Dify → vLLM works on Gemma after the LLM swap from Qwen3.6. |

Streaming chat + OpenAI Python SDK end-to-end test are next (todo item
in checkpoint).

---

## 8. How to read the PR (suggested order)

1. **`ai-review/specs/feat-ai-sdk-v2.md`** — what we set out to do.
2. **This file (`IMPLEMENTATION.md`)** — what shipped and what each test verifies.
3. **`gateway/src/gateway/registry.py`** — `EmbeddingModelEntry` + `owner` field. Smallest delta, biggest surface area for the rest.
4. **`gateway/src/gateway/schemas.py`** — `EmbeddingsRequest/Response` + R6 `effective_*` properties.
5. **`gateway/src/gateway/routers/embeddings.py`** — the new route, 50 LOC.
6. **`gateway/src/gateway/embeddings/client.py`** — the upstream caller.
7. **`gateway/src/gateway/routers/models.py`** — `owned_by` fix.
8. **`gateway/tests/test_embeddings.py`** — 11 tests cover everything above.
9. **`ai-review/reviews/feat-ai-sdk-v2/review-1.md` + `review-1-response.md`** — what codex caught, how we responded.
10. **`ai-review/reviews/feat-ai-sdk-v2/review-2.md`** — round-2 findings deferred to PR #3.

---

## 9. Known follow-ups (PR #3)

- **[R2-R5]** `/v1/datasets`, `/v1/files`, retrieval, embedding lazy-provisioning.
- **[R7]** Streaming `reasoning_content` chunk surface.
