# Feature: AI SDK Gateway PR #2 — Knowledge Base + OpenAI Aliases

## Feature ID

`ai-sdk-gateway-kb`

## Owner

luluwen

## Status

- [x] Draft
- [ ] Ready for implementation
- [ ] In review
- [ ] Approved
- [ ] Merged

## Related PR

- PR #1: `ai-review/specs/feat-ai-sdk-v1.md`（chat path 已 merge / merge 中）

---

## Goal

擴充 Gateway 的 API 表面到 **「OpenAI 完整使用體驗」**：
1. 加 `/v1/embeddings`（OpenAI 標準）
2. 加 `/v1/datasets`、`/v1/files` 給客戶上傳 RAG 文件（非 OpenAI 標準，但 Dify 風格）
3. 加 OpenAI 最新棄用欄位的 alias（`max_completion_tokens`、`safety_identifier`），新舊並存
4. Streaming 串流加上 reasoning content 支援（Qwen3 thinking mode 內容透傳給 client）

## Non-goals

- 不做 Real rate limiting（PR #3 範圍）
- 不做 bypass-Dify 直連 vLLM 模式（post-MVP）
- 不做 multi-region routing
- 不做 OpenAI Files API 完整相容（finetuning 用，跟我們無關；我們是知識庫用途）

## User Story

```
As 基站 SDK 客戶
I want 用 openai-python 上傳設備手冊（PDF / Word / Markdown）+ 向量化
So that 我用 chat completion 問問題時，Gateway 能自動帶上手冊內容做 RAG
```

```
As 基站 SDK 客戶
I want 用 openai-python 直接拿到文字向量（embedding）
So that 我能在自家系統做向量檢索（不一定要透過我們的 chat）
```

```
As OpenAI SDK 升級到最新版的客戶
I want 用 max_completion_tokens 寫程式碼
So that 不會踩到 deprecation warning，雖然 max_tokens 仍然有效
```

## Requirements

### 核心功能

- [ ] **R1：`/v1/embeddings`（OpenAI 標準）**
    - `POST /v1/embeddings`，OpenAI schema：`{model, input, encoding_format, dimensions, user}`
    - 路由到 Dify Workspace API 的 model invocation；或直連 vLLM embed endpoint
    - 回應符合 OpenAI Embeddings response schema
    
- [ ] **R2：`/v1/datasets`（自定義端點，Dify 風格）**
    - `POST /v1/datasets` — 建客戶專屬知識庫（lazy-build per customer）
    - `GET /v1/datasets` — 列客戶的知識庫
    - `GET /v1/datasets/{id}` — 取 metadata
    - `DELETE /v1/datasets/{id}` — 刪知識庫
    
- [ ] **R3：`/v1/files`（自定義端點，Dify 風格 upload）**
    - `POST /v1/files` — multipart 上傳檔案進知識庫
    - `GET /v1/files` — 列檔案
    - `DELETE /v1/files/{id}` — 刪檔案
    - Query param `dataset_id` 指定知識庫
    
- [ ] **R4：Retrieval（純檢索通道）**
    - `POST /v1/datasets/{id}/retrieve` — 純檢索回傳 top-k chunks
    - 包裝 Dify hit-testing API
    
- [ ] **R5：Embedding model lazy-provisioning**
    - 不同客戶可能想用不同 embedding model
    - 第一次建 dataset 時記錄 embedding_model；之後該 dataset 鎖死該 model
    - registry.yaml 加 embedding_models 清單

### OpenAI 規範更新

- [ ] **R6：欄位 alias 支援（新舊並存）**
    - `max_tokens`（舊，仍主流）+ `max_completion_tokens`（新）
    - `user`（舊，Dify/vLLM 認）+ `safety_identifier`（新）
    - Router 取值優先新欄位、舊欄位 fallback
    - 對下游 Dify/vLLM 一律送舊欄位（它們認的）

### Streaming 進化

- [ ] **R7：Reasoning content streaming（PR #1 lesson）**
    - Qwen3 / DeepSeek-R1 等 reasoning models 的 `<think>...</think>` 內容
    - 透過 `delta.reasoning_content` 欄位串流給 client（OpenAI o1 標準）
    - vLLM `--reasoning-parser qwen3` 解析 → Gateway 分流 → OpenAI chunk

## Acceptance Criteria

- [ ] 官方 OpenAI Python SDK `client.embeddings.create(model="bge-m3", input="text")` 能拿到 1024 維向量
- [ ] 用 SDK 上傳一份 .txt 文件後，chat 能命中 RAG 並在 `metadata.references` 帶內容
- [ ] 同客戶舊 code 寫 `max_tokens=100` 正常運作；新 code 寫 `max_completion_tokens=100` 也正常；同時寫兩個時 `max_completion_tokens` 優先
- [ ] Streaming chat 對 Qwen3 model 能拿到 `delta.reasoning_content` chunk（PR #1 是丟掉的）
- [ ] 多客戶上傳同一份檔案到各自 dataset，互不干擾

## Out of Bounds

- 不改 Dify 核心（Gateway 純 proxy）
- 不引入新 Python 套件除非必要
- 不破壞 PR #1 已有的 contract

## Technical Notes

- **Dataset 建立時間長**：DSL import → app 建立 → embed_model 設定，整個約 5-10 秒。Lazy provisioning + cache 跟 R3（LLM App）走同一套邏輯。
- **File upload**：Dify `/v1/datasets/{id}/document/create-by-file` 接 multipart，Gateway 直接 proxy（用 httpx multipart）。
- **Embeddings lazy-build**：embedding model 跟 LLM 不同——dataset 一旦建立 embedding 就鎖死（向量維度不能改）。
- **Backward compat**：`/v1/chat/completions` 行為 100% 不變。

## References

- OpenAI Embeddings: https://platform.openai.com/docs/api-reference/embeddings
- Dify Knowledge Base API: `api/controllers/service_api/dataset/`
- PR #1 spec / response：`ai-review/specs/feat-ai-sdk-v1.md`, `ai-review/reviews/feat-ai-sdk-v1/`

## Spec 變更歷史

- 2026-05-15：建立初稿；含 R6 alias support（從 PR #1 deprecation 討論延伸）
