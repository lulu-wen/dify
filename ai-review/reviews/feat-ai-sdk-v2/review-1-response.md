# Review Response: feat-ai-sdk-v2 — Round 1

> Response to `reviews/feat-ai-sdk-v2/review-1.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 1 | 1 | 0 |
| [P2] | 0 | — | — |

A single [P1] finding — a cross-file state drift bug in the test suite
caused by updating a shared fixture without updating one of the assertions
that depended on the old fixture shape.

## Findings 處理紀錄

---

### Finding 1: [P1] Exclude explicit embedding owners from default-owner assertion

- **Severity**: [P1]
- **Codex 描述**:
  > With the updated fixture, `make_customer()` now registers `emb1` by default
  > with `owner="TestPublisher"`, so `/v1/models` includes that embedding row
  > and this `all(...)` assertion fails on every run.
- **影響檔案**: `gateway/tests/test_models_endpoint.py:29`
- **動作**: ✅ Fixed

#### 驗證

This is a real, deterministic test failure. Walking through:

1. PR #1's `make_customer()` fixture only built LLM models (no embedding entry).
2. The original `test_models_endpoint_returns_customer_models` asserted
   `all(m["owned_by"] == "ai-sdk-gateway" for m in body["data"])` — true
   because every model was an LLM with default owner.
3. R1 added `EmbeddingModelEntry` to the fixture with `owner="TestPublisher"`
   (different from the default) to exercise the publisher-identity path.
4. The `/v1/models` endpoint now lists both LLM **and** embedding entries.
5. The `all(...)` assertion would fail on `emb1` (`"TestPublisher"` ≠
   `"ai-sdk-gateway"`).

I should have caught this by running `pytest` before pushing. I only ran
`ast.parse` (syntax check), which catches typos but not cross-file state drift.
Same lesson as PR #1's pytest discoveries — **AST validity ≠ test correctness**.

#### 修復內容

Refactored the assertion from a single `all(...)` predicate to a per-id
dictionary lookup that names the expected owner for each row:

```python
by_id = {m["id"]: m for m in body["data"]}
assert {"m1", "m2", "emb1"} <= by_id.keys()

# LLM rows fall back to the gateway default
assert by_id["m1"]["owned_by"] == "ai-sdk-gateway"
assert by_id["m2"]["owned_by"] == "ai-sdk-gateway"
# The embedding row carries the explicit publisher
assert by_id["emb1"]["owned_by"] == "TestPublisher"
```

This is **stricter** than the original — it pins each entry's owner
individually, rather than asserting a global property. Cross-file drift
(e.g., someone later changes the fixture's embedding owner) will produce a
specific, actionable error message rather than a `True != False` from `all()`.

Also expanded the docstring to call out the fixture-state assumption
explicitly, so future readers don't have to reverse-engineer the conftest.

---

## 整體決策

- Round 1 後狀態：**進 round 2 確認沒新問題**
- Round 1 收斂性：1 個 P1，純測試 bug、修法明確且不影響 production 程式碼。
- Process lesson:
  - PR #1 pytest 找到 4 個 bug 之後我把流程改成「commit 前 pytest 必跑」
  - 這次回到 AST-only 是退步；R1 規模較大時更容易踩到 cross-file drift
  - 修正：codex review 之前一律先 pytest

## 預備 Round 2 的觀察點

預期 codex review-2 可能會看：
1. `_resolve_model` 用 `UnknownModelError` (404) — 跟 OpenAI 對 unknown
   model 慣例（400 `invalid_request_error`）一致嗎？
2. `invoke_embeddings` 每次 build 新 `AsyncClient` — 高 RPS 場景是否該 pool？
3. embedding response 的 `model` 欄位從上游覆寫成客戶 id — 上游 usage 統計
   仍是上游格式，是否該重塑？
4. R6 alias precedence 在 EmbeddingsRequest 跟 ChatCompletionRequest 用同
   一套 pattern — 是否該抽出 mixin 避免重複？
