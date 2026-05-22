# Review Response: feat-ai-sdk-v2 — Round 2

> Response to `reviews/feat-ai-sdk-v2/review-2.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 0 | — | — |
| [P2] | 2 | 2 | 0 |

Round 2 收斂結果：0 P1，2 個 P2 全部修完。沒有 deferral。

## Findings 處理紀錄

---

### Finding 1: [P2] Preserve embedding upstream 4xx responses

- **Severity**: [P2]
- **Codex 描述**:
  > When a request passes gateway validation but the embedding backend rejects
  > it as a client error — for example, a positive `dimensions` value on a
  > model that does not support truncation, or an input that exceeds the
  > backend's token limit — OpenAI-compatible upstreams return 4xx responses.
  > This branch converts every non-2xx response into `DifyUpstreamError`, so
  > those client mistakes are reported as 502 `dify_upstream_error` responses
  > instead of the upstream 4xx / invalid-request response. This breaks proxy
  > semantics for valid client inputs the gateway cannot fully validate itself.
- **影響檔案**: `gateway/src/gateway/embeddings/client.py:72-80`
- **動作**: ✅ Fixed

#### 驗證

Codex 是對的。Gateway 無法 pre-validate 的客戶輸入錯誤包括：

- `dimensions=999999`（超過模型支援的維度截斷上限）
- input 字數超過 `max_model_len`
- 模型不支援 `encoding_format="base64"`
- payload 超過 vLLM 的 max_request_size

這些都是上游 vLLM 用 4xx 回的，原本實作都被吞成 502 `dify_upstream_error`。
客戶看到 502 會以為要 retry、或以為 gateway 掛了，但其實是自己 input 不對。

#### 修復內容

1. `gateway/src/gateway/errors.py` — 新增 `UpstreamClientError`，
   `error_type="invalid_request_error"`、`code="upstream_invalid_request"`。
   `status_code` 用 instance attribute 覆寫，能保留上游原本的 HTTP 狀態碼
   （400 / 413 / 422 等都各自透傳，不一律壓成 400）。

2. `gateway/src/gateway/embeddings/client.py:72-94` — 把 `if not resp.is_success`
   分支拆成兩段：

   ```python
   if 400 <= resp.status_code < 500:
       raise UpstreamClientError(
           f"Embedding endpoint rejected request (HTTP {resp.status_code}): {body_preview}",
           upstream_status=resp.status_code,
       )
   # 5xx or other non-success: real upstream failure.
   raise DifyUpstreamError(...)
   ```

3. 上游錯誤訊息 (`body_preview`) 會被帶進 `error.message`，讓客戶能看到
   vLLM 原本的拒絕原因（例如 `dimensions out of range`），不用打開 gateway 的 log 才知道為何爆掉。

#### 測試

`gateway/tests/test_embeddings.py::test_embeddings_upstream_4xx_passes_through`，
parametrized 跑 `[400, 413, 422]`：

- 上游回 4xx → client 收到一樣的 4xx（不是 502）
- `error.type == "invalid_request_error"`
- `error.code == "upstream_invalid_request"`
- 上游的 message preview 出現在 `error.message`

原本的 `test_embeddings_upstream_5xx_returns_502` 保留 — 5xx + transport
error 路徑邏輯不變，docstring 加註「real server failure」做語義 disambiguation。

---

### Finding 2: [P2] Reject IDs shared by chat and embedding models

- **Severity**: [P2]
- **Codex 描述**:
  > When a customer configures an LLM and an embedding model with the same
  > customer-facing `id`, both per-list validators pass, even though
  > `/v1/models` now flattens the two lists into one response. That produces
  > duplicate OpenAI model IDs in the advertised list and violates this
  > module's existing invariant that model IDs are unique within a customer,
  > so registry validation should also reject cross-list collisions.
- **影響檔案**: `gateway/src/gateway/registry.py:127-135`
- **動作**: ✅ Fixed

#### 驗證

`registry.py` 之前各跑 per-list `_unique_model_ids` 跟 `_unique_embedding_model_ids`，
兩個 list 內部的 unique 都擋了，但**跨 list** 的 collision 沒擋。

考慮這個 registry：

```yaml
models:
  - id: "shared-name"
    provider: ...
embedding_models:
  - id: "shared-name"
    endpoint_url: ...
```

呼叫 `/v1/models` 會回兩個 id 相同的 entry — OpenAI spec 沒禁止但任何
client 的 `models[id]` 字典型存儲都會被覆蓋掉一個。更嚴重的是 `find_model`
跟 `find_embedding_model` 各自走自己的 list，客戶若 POST 到 `/v1/chat/completions`
跟 `/v1/embeddings` 都送 `model="shared-name"`，會走完全不同的 backend，
但客戶完全沒被告知 — 這是會炸 debugging 預算的 silent overload。

#### 修復內容

`gateway/src/gateway/registry.py:138-160` — 新增 `model_validator(mode="after")`
（pydantic v2 model-level validator，跑在所有 field validator 之後）：

```python
@model_validator(mode="after")
def _no_id_collisions_across_lists(self) -> "CustomerEntry":
    llm_ids = {m.id for m in self.models}
    emb_ids = {e.id for e in self.embedding_models}
    overlap = llm_ids & emb_ids
    if overlap:
        raise ValueError(
            f"model ids collide across LLM and embedding lists: {sorted(overlap)}"
        )
    return self
```

啟動載 YAML 時就會炸明確 message（不是 runtime 神祕行為），跟既有
`"model ids must be unique within a customer"` / `"embedding model ids
must be unique"` 一系列 validator 風格一致。

#### 測試

`gateway/tests/test_registry.py::TestCustomerEntryValidation`：

- `test_id_collision_across_llm_and_embedding_rejected` — 同 id 同時在
  `models` 跟 `embedding_models` 出現 → `ValueError("collide across LLM and embedding")`
- `test_disjoint_llm_and_embedding_ids_accepted` — 兩個 list id 不衝突時
  正常建立、`find_model` / `find_embedding_model` 都還能查得到（防止
  我寫的 validator 不小心擋到合法 config）

---

## 整體決策

- Round 2 後狀態：**準備開 PR**
- Round 2 收斂性：0 P1，2 P2 全修，無 deferral。
- 全測試 129 PASSED（包含新增 5 個測試：3 個 parametrize 4xx + 2 個 cross-list）
- 兩個 fix 是兩個獨立 commit，方便 reviewer 各別評估。

## 預備 Round 3 的觀察點

Round 2 兩個 finding 都是 codex round 1 沒看到、要實際 trace flow 才看得到的問題，
代表 round 1 偏重「明顯的 test fixture drift」，round 2 才開始 cover semantic correctness。
預期 codex round 3 可能會看：

1. `UpstreamClientError.status_code` 用 instance attribute 覆寫 — pydantic
   model dump 或 logging middleware 取值會不會踩到 class vs instance 差異？
2. Cross-list collision check 用 set intersection — 如果未來再加第三類
   model（例如 reranker），需要把 invariant 抽出來避免 N-list pairwise check。
3. 4xx body preview 帶進 `error.message` 可能 leak 上游的 vLLM 版本字串
   / 內部 path — 是否該 sanitize？（傾向不修，proxy 透明性 > 神秘化）
4. `UpstreamClientError` 的 `code` 是 `"upstream_invalid_request"` 不是
   OpenAI 標準的 `"invalid_request_error"` code — 跟 OpenAI 慣例是否要對齊？
