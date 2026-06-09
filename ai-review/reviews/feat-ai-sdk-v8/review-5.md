# Codex Review #5 — feat/ai-sdk-gateway-pr8 (Phase 1b)

> Reviewer: OpenAI Codex CLI. Base: `main`. Diff: through `24ab190d4` (R4 fixes).

## Summary

| Severity | P1 | P2 | P3 |
|---|---|---|---|
| count | 0 | 1 | 0 |

## Finding — verbatim

```
[P2] chat.py:213 under-reserves token cost for RAG-enabled customers.
  The admission estimate counts OpenAI messages plus max output, but Dify may
  inject retrieved KB chunks into the real LLM prompt. A short RAG query over
  a large KB can therefore reserve too little and still exceed
  node_token_budget. Codex recommends adding a bounded retrieval-context
  allowance, or capping Dify retrieval and including that cap in the
  reservation whenever KBs are attached.
```

## Why it's real

What vLLM actually sees on a RAG request =
`client messages` + `retrieved KB chunks (Dify-injected)` + `generation`.

We were reserving for the first and third only. For a customer with a fat
KB, Dify's retrieval can inject several thousand tokens into the prompt
before vLLM tokenises it — under-reserving by exactly that amount.

## Fix — both layers, one shared knob (R3 pattern)

R3 taught the durable shape: when "the cap" and "the reservation" can
disagree, make both derive from a single value. Same here:

**Dify side (the real cap)**
- `dsl.py`: `build_chat_app_dsl(..., retrieval_top_k=N)` emits
  `dataset_configs.top_k = N` when KBs are attached. Dify uses this in
  multiple-retrieval mode (verified by reading
  `api/core/app/app_config/easy_ui_based_app/dataset/manager.py:117`).
- `AppManager`: takes `retrieval_top_k` ctor param; passes to dsl when
  building. main.py wires `settings.default_kb_top_k` (gated on
  `rate_limit_enabled`, same gate as `default_max_output_tokens` injection).
- `DSL_VERSION` bump `v4-output-cap → v5-rag-top-k-cap` so existing Apps
  rebuild with the cap.

**Reservation side (matches the cap)**
- `estimate_cost(rag_allowance_tokens=...)` adds it into `token_cost`.
- `estimate_request_cost(has_knowledge_bases=...)` computes
  `default_kb_top_k * default_kb_chunk_tokens` when True, else 0.
- chat router passes `has_knowledge_bases=bool(customer.knowledge_bases)`.

**Two settings, decoupled meaning:**
- `default_kb_top_k = 3` — Dify's actual retrieval count (also the
  reservation's chunk-count term).
- `default_kb_chunk_tokens = 1000` — conservative per-chunk upper bound
  (the reservation's chunk-size term; Dify chunk size is set at indexing
  time, not at retrieval, so this is an estimation knob).

Default RAG allowance = 3 × 1000 = 3000 tokens. Operators tune per their
Dify `indexing_technique` chunk sizing.

## Tests (+4)

- `test_rag_customer_reservation_includes_kb_allowance`: registry has TWO
  customers, one with KBs and one without. `node_token_budget=2000` admits
  the non-RAG cost (~1024) but rejects the RAG cost (~4024). Asserts 200
  vs 503 — the only way for that to happen is if the allowance is in the
  reservation.
- `test_autobuilt_app_caps_dify_retrieval_top_k`: with KBs attached and
  `default_kb_top_k=2`, the auto-built App DSL's
  `dataset_configs.top_k == 2`.
- `test_no_kb_omits_top_k_in_dsl`: customer without KBs → no `top_k`
  emitted (don't add noise to Apps that don't retrieve) AND reservation
  unchanged.
- `test_estimate_cost_adds_rag_allowance`: unit-level confirmation that
  `rag_allowance_tokens` flows into `token_cost`, negatives clamped.

## Gate

**PASS.** Suite 405 passing, mypy + ruff clean.

## Pattern

R3 (effective_max_tokens) → R5 (kb_top_k): same shape, different field.
When the reservation's accuracy depends on a cap the gateway controls AND
something downstream can ignore the cap, derive both from a single source
of truth — preferably one that's enforced (DSL), not estimated. The
estimation-only knob (`default_kb_chunk_tokens` here) is honest about
being a knob, not pretending to be a bound.
