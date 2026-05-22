# Feature: AI SDK Gateway PR #3 — Knowledge Base + Reasoning Streaming

## Feature ID

`ai-sdk-gateway-kb-impl`

## Owner

luluwen

## Status

- [x] Draft
- [ ] Ready for implementation
- [ ] In review
- [ ] Approved
- [ ] Merged

## Related PRs

- PR #1: `ai-review/specs/feat-ai-sdk-v1.md`（chat path，已 merge）
- PR #2: `ai-review/specs/feat-ai-sdk-v2.md`（embeddings + OpenAI aliases，已 ready）

---

## Goal

把 PR #2 規劃但 defer 掉的 5 個需求做完，讓客戶能用單一 SDK 完整管理
**知識庫 (KB) 全流程**：建 dataset、上傳文件、檢索、embedding lazy-provisioning，
並補上 **Qwen3 reasoning 模型的 thinking 串流透傳**。

完成後 Gateway 就具備產品 GA 的完整 OpenAI 介面 + Dify-style 知識庫管理面。

## Non-goals

- 不做 Real rate limiting（PR #4 範圍）
- 不做 multi-region routing
- 不做 OpenAI Files API 完整相容（finetuning 用，跟我們無關）
- 不引入新 Python 套件（用既有 httpx multipart 處理 R3）

## User Story

```
As 基站 SDK 客戶
I want 用 openai-python（+ httpx）上傳 RSRP 故障診斷手冊（PDF / Markdown）
So that 我用 chat completion 問問題時，Gateway 能自動帶上手冊內容做 RAG
```

```
As 基站 SDK 客戶
I want 直接 hit-test 我的知識庫
So that 我能在自家系統做檢索評估（不必每次都生成 LLM 回答）
```

```
As 基站 SDK 客戶
I want stream 模式下能拿到 Qwen3 的 <think> 內容
So that 我能展示「AI 正在分析」的 thinking UI（OpenAI o1 標準的 reasoning_content）
```

## Requirements

### 核心知識庫 (R2-R5)

- [ ] **R2：`/v1/datasets` CRUD**
    - `POST /v1/datasets` — 建客戶專屬知識庫
    - `GET  /v1/datasets` — 列客戶的知識庫（支援 page / limit / keyword）
    - `GET  /v1/datasets/{id}` — 取 metadata
    - `DELETE /v1/datasets/{id}` — 刪知識庫
    - Auth：客戶用同一個 SDK key；Gateway 內部用客戶的 `dataset_api_key`
      呼叫 Dify Service API
    - 不接受 PATCH（避免客戶不小心改掉 embedding model 破壞向量；
      要改 metadata 直接 delete + recreate）

- [ ] **R3：`/v1/files` multipart 上傳**
    - `POST /v1/files` (multipart/form-data) — 上傳文件進指定 dataset
        - form field：`file` (binary) + `dataset_id` (string)
        - 可選 form field：`indexing_technique`（"high_quality" / "economy"）
    - `GET  /v1/files?dataset_id=...` — 列 dataset 下的所有檔案
    - `DELETE /v1/files/{id}?dataset_id=...` — 刪一份檔案
    - 包裝 Dify `POST /datasets/{dataset_uuid}/document/create-by-file`
      （Dify 用 Flask + multipart，Gateway 直接 stream proxy）

- [ ] **R4：`/v1/datasets/{id}/retrieve` 純檢索**
    - `POST /v1/datasets/{id}/retrieve { query, top_k?, score_threshold? }`
    - 包裝 Dify `POST /datasets/{uuid}/retrieve` （hit-testing API）
    - 回應 schema：`{ records: [{ segment, score, document }, ...] }`
    - 用途：客戶要在自家系統做 RAG ranking 評估、做 search-only UI

- [ ] **R5：Embedding model lazy-provisioning**
    - `POST /v1/datasets` 時：
        - 客戶在 body 帶 `embedding_model="bge-m3"` → 驗 registry，把
          `embedding_model` + `embedding_model_provider` 一起送 Dify
        - 客戶**沒帶** → fallback 用 customer 的第一個 `embedding_models`
          entry；都沒有 → 400 `invalid_request_error`
    - Dataset 建立後 embedding 模型**鎖死**（Dify 本身就會擋；Gateway 在
      `PATCH /datasets` 不開放，所以這層也擋了）
    - Registry 端不需要改（PR #2 R1 的 `embedding_models` 欄位已經夠用）

### Streaming 進化 (R7)

- [ ] **R7：Reasoning content streaming**
    - Dify 對 reasoning 模型會吐 `event: "agent_thought"` SSE，內含 `thought`
      欄位（這是 Qwen3 `<think>...</think>` 內容 / DeepSeek-R1 思考過程）
    - Gateway 把這個 event 轉成 OpenAI 串流 chunk：
      `{ choices: [{ delta: { reasoning_content: "..." } }] }`
      （OpenAI o1 streaming 標準的 `delta.reasoning_content`）
    - 客戶端用標準 openai-python 解析 chunk 就能拿到 `chunk.choices[0].delta.reasoning_content`
    - 必須跟既有的 `message` event（content delta）**並行不衝突** —
      推理階段 yield reasoning_content chunks，回答階段 yield content chunks

## Acceptance Criteria

- [ ] `client.datasets.create(name="rsrp-manuals", embedding_model="bge-m3")` 成功，
      Gateway 內部對 Dify 用客戶的 `dataset_api_key` 呼叫，回傳 `id` 等 metadata
- [ ] 用 httpx multipart 上傳 `.txt` / `.pdf` → `POST /v1/files` → Dify
      建立 document → 後續 chat 命中時 `metadata.references` 帶回該段內容
- [ ] `POST /v1/datasets/{id}/retrieve` 回傳 top-k 段落 + score
- [ ] 客戶**沒帶** embedding_model 但 registry 有 default → dataset 建立成功
- [ ] 客戶**沒帶** embedding_model 且 registry 也沒 default → 400 並提示
      具體錯誤訊息
- [ ] Streaming chat 對啟用 reasoning 的 Dify App → 串流先收到一批
      `reasoning_content` chunks，再收到 `content` chunks，最後 `[DONE]`
- [ ] 多客戶上傳同名檔案到各自 dataset，互不干擾（dataset uuid 隔離）
- [ ] `/v1/datasets`、`/v1/files`、`/v1/datasets/{id}/retrieve` 都走同一套
      auth middleware（沒帶 Bearer → 401；錯 key → 401；錯 dataset_id → 404）

## Out of Bounds

- 不改 Dify 核心源碼（Gateway 純 proxy）
- 不引入新 Python 套件（multipart 用既有 httpx）
- 不破壞 PR #1 / PR #2 的 API contract
- 不做 dataset 的 PATCH（避免客戶誤改 embedding model 破壞向量）
- 不做 `POST /v1/datasets/{id}/document/create-by-text`（inline text），
  客戶要 inline 就自己用 file upload

## Technical Notes

### R2-R5 — Dify Service API mapping

| Gateway 端點 | HTTP | Dify Service API 端點 | Auth |
|---|---|---|---|
| `POST /v1/datasets` | POST | `POST /v1/datasets` | `Bearer <dataset_api_key>` |
| `GET /v1/datasets` | GET | `GET /v1/datasets?page=&limit=&keyword=` | `Bearer <dataset_api_key>` |
| `GET /v1/datasets/{id}` | GET | `GET /v1/datasets/{uuid}` | `Bearer <dataset_api_key>` |
| `DELETE /v1/datasets/{id}` | DELETE | `DELETE /v1/datasets/{uuid}` | `Bearer <dataset_api_key>` |
| `POST /v1/files` | POST multipart | `POST /v1/datasets/{uuid}/document/create-by-file` | `Bearer <dataset_api_key>` |
| `GET /v1/files?dataset_id=...` | GET | `GET /v1/datasets/{uuid}/documents` | `Bearer <dataset_api_key>` |
| `DELETE /v1/files/{id}?dataset_id=...` | DELETE | `DELETE /v1/datasets/{uuid}/documents/{document_id}` | `Bearer <dataset_api_key>` |
| `POST /v1/datasets/{id}/retrieve` | POST | `POST /v1/datasets/{uuid}/retrieve` | `Bearer <dataset_api_key>` |

關鍵設計：
- **`dataset_id` 是 Dify uuid，gateway 不做 slug 對應**（PR #2 的 model_id
  是 slug 因為要對客戶友善；dataset 是客戶建出來的，uuid 就好；客戶記不住
  uuid 是他們的問題，就像他們記不住 OpenAI 的 `file-abc123`）
- **Multipart 用 httpx stream upload**：不要把整個檔案讀進記憶體，用
  `httpx.AsyncClient.stream` 邊收 client multipart 邊推到 Dify（防止 OOM
  on 大 PDF）
- **Per-customer DifyClient 重用**：PR #1 已經有 `dify_clients` cache by
  `base_url`，新增 dataset 方法直接掛在 `DifyClient` 上就行
- **錯誤映射**：Dify Service API 4xx → 走 `UpstreamClientError` (透傳，
  PR #2 review-3 的 pattern)；5xx → `DifyUpstreamError`。Dify 4xx 常見：
  - 400 dataset_name_duplicate / embedding_model_not_found
  - 401 sdk_dataset_api_key 過期
  - 404 dataset_not_found / document_not_found

### R5 — Embedding model lazy-provisioning 邏輯

```python
# routers/datasets.py POST handler 偽碼
def resolve_embedding_model(customer, body) -> EmbeddingModelEntry:
    requested = body.embedding_model
    if requested:
        entry = customer.find_embedding_model(requested)
        if entry is None:
            raise UnknownModelError(f"embedding model '{requested}' is not enabled for this customer")
        return entry
    # No request → use customer default (first embedding model)
    if not customer.embedding_models:
        raise InvalidRequestError(
            "no embedding model configured for this customer; pass `embedding_model` explicitly",
            param="embedding_model",
        )
    return customer.embedding_models[0]
```

送給 Dify 的 payload 帶上 `embedding_model` + `embedding_model_provider`，
Dify 就會把它 bake 進 dataset config，之後加文件都會用同一個 embedding。

### R7 — Streaming reasoning_content

Dify 串流事件型態：

```jsonc
// Reasoning model (Qwen3, DeepSeek-R1) thinking 階段
data: {"event":"agent_thought","thought":"用戶問 RSRP=-115 ...","conversation_id":"..."}
// 多個 agent_thought event...

// 進入回答階段
data: {"event":"message","answer":"基站收到 RSRP=-115 的告警..."}
// 多個 message event...

data: {"event":"message_end","metadata":{...}}
```

Gateway 串流轉換器要：

```jsonc
// 對應每個 agent_thought event
data: {"id":"chatcmpl-...","choices":[{"delta":{"reasoning_content":"用戶問..."}, "index":0,"finish_reason":null}],...}

// 對應每個 message event（既有邏輯不變）
data: {"id":"chatcmpl-...","choices":[{"delta":{"role":"assistant","content":"基站收到..."},"index":0,"finish_reason":null}],...}

// 最終 chunk + [DONE]
```

Schema 改動：`DeltaMessage` 加 `reasoning_content: str | None`。
Converter 改動：`event_type == "agent_thought"` 分支 emit reasoning chunk。
測試：`test_sse_converter` 加 `test_agent_thought_events_translated_to_reasoning_content_chunks`。

## 實作順序

1. **R7 先做**（最獨立、最小、~200 LOC）— 給 PR #3 的 review 流程暖身
2. **DifyClient 加 dataset 方法**（內部 helper，無對外端點）
3. **R2 + R5 一起做**（datasets CRUD + embedding lazy-provision，互相綁定）
4. **R3 multipart upload**（需要 R2 dataset 存在才有意義）
5. **R4 retrieve**（最後做，依賴 R2 dataset 結構）

每階段都跑 pytest，每個獨立 feature 完成後考慮跑 codex review。

## References

- Dify Service API for datasets: `api/controllers/service_api/dataset/`
- Dify hit-testing: `api/controllers/service_api/dataset/hit_testing.py`
- Dify agent_thought stream event: `api/core/app/task_pipeline/easy_ui_based_generate_task_pipeline.py:503`
- OpenAI o1 streaming `reasoning_content`: https://platform.openai.com/docs/guides/reasoning
- PR #2 review history (process lessons): `ai-review/reviews/feat-ai-sdk-v2/review-{1,2,3}.md`

## Spec 變更歷史

- 2026-05-18：建立初稿；scope 是 PR #2 defer 的 R2-R5 + R7
