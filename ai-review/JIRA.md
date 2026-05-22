# AI SDK Gateway — Implementation Record (PR #1 + PR #2)

> 給 Jira / Confluence / Notion 使用的完整實作紀錄。一份文件講完：
> 1. **Gateway 在系統中扮演什麼角色** — 客戶從 OpenAI SDK 進來，Gateway 怎麼把它送到對的後端
> 2. **目前實作了什麼** — PR #1（chat 全鏈路）跟 PR #2（embeddings + alias）所有功能逐項列出
> 3. **每一個測試在驗什麼、為什麼一定要這個測試** — 沒寫這個測試會放掉哪個 bug
> 4. **Codex review 歷史** — 兩個 PR 共 5 輪獨立 AI review 找到並修掉的問題
>
> 文件結構讓不熟系統的人也能逐節讀懂。

---

## 1. 為什麼要做 Gateway？

### 1.1 真實場景

基站客戶用 Python 開發告警分析、設備診斷功能，已經會用 OpenAI 的 SDK 寫
程式碼了。我們希望他們：

```python
# 客戶端 — 不需要學新的 API
from openai import OpenAI

client = OpenAI(
    base_url="https://aisdk.example.com/v1",      # ← 我們的 Gateway
    api_key="bsa_dev_xxxxx",                       # ← 客戶專屬 SDK key
)

# 用標準 OpenAI 介面就能跑 chat
client.chat.completions.create(
    model="gemma-3n-e4b",
    messages=[{"role": "user", "content": "RSRP=-115 告警怎麼分析？"}],
)

# 也能直接拿向量
client.embeddings.create(model="bge-m3", input="基站告警文本")
```

不能要求客戶學 Dify 的 API，更不能讓他們看到 Dify 的 console。

### 1.2 後端的真實限制

每個客戶要一個獨立的 Dify 部署（**Dify license 限制**：開源版單部署只能
一個 workspace），所以後端長這樣：

```
                ┌──── 客戶 A 專屬 Dify ────┐
                │  + per-app + 知識庫       │
Gateway ────────┼──── 客戶 B 專屬 Dify ────┤────── vLLM (chat / embed)
                │  + per-app + 知識庫       │       Gemma 3n / bge-m3
                └──── 客戶 C 專屬 Dify ────┘
```

Gateway 就是這層**統一入口 + 路由器**：
- 識別客戶（看 SDK key）
- 找對應的後端（查 registry）
- 翻譯協議（OpenAI ↔ Dify ↔ vLLM）
- 統一錯誤格式 / logging / quota

### 1.3 系統定位圖

```
┌────────────────────────────────────────────────────────────────────────┐
│                          客戶 Python 程式碼                              │
│        使用標準 openai-python SDK，看不到底層任何 Dify 細節             │
└─────────────────────────┬──────────────────────────────────────────────┘
                          │ HTTPS, Authorization: Bearer <sdk_key>
                          ▼
┌────────────────────────────────────────────────────────────────────────┐
│            AI SDK Gateway (FastAPI + Python 3.11+, async)              │
│  ────────────────────────────────────────────────────────────          │
│   /v1/chat/completions  (blocking + streaming) ← PR #1                 │
│   /v1/embeddings        (OpenAI-相容)          ← PR #2                 │
│   /v1/models                                   ← PR #1+PR #2           │
│   /health                                       ← unauthenticated      │
│  ────────────────────────────────────────────────────────────          │
│   AuthMiddleware    │ 解 Bearer → registry → request.state.customer   │
│   LoggingMiddleware │ request_id + structlog                            │
│   Error handler     │ 所有錯誤 → OpenAI envelope (一致 4xx/5xx)         │
└───────────┬───────────────────────────────────┬────────────────────────┘
            │ chat / RAG                        │ pure embedding
            ▼                                   ▼
┌────────────────────────────────┐    ┌──────────────────────────────────┐
│  客戶專屬 Dify 部署            │    │  vLLM (--task embed) 直連        │
│  + per-(customer,model) App   │    │  bge-m3, dim=1024                │
│  + retriever_resources → ref  │    │  跳過 Dify 不繞                  │
└─────────┬──────────────────────┘    └──────────────────────────────────┘
          ▼
┌────────────────────────────────┐
│  vLLM (--task generate)        │
│  Gemma 3n E4B / Qwen3 / ...   │
└────────────────────────────────┘
```

---

## 2. 至今所有實作的功能（依 spec ID）

| Spec ID | 名稱 | 屬於 | 狀態 |
|---|---|---|---|
| **R1 (v1)** | `POST /v1/chat/completions` blocking + streaming | PR #1 | ✅ 已上 main |
| **R2 (v1)** | SDK key → registry → customer 路由 | PR #1 | ✅ |
| **R3 (v1)** | OpenAI `messages` → Dify `query` 翻譯；`extra_body.llm_model` 動態切模型 | PR #1 | ✅ |
| **R4 (v1)** | Dify SSE → OpenAI `chat.completion.chunk` 串流轉換 | PR #1 | ✅ |
| **R5 (v1)** | 知識庫管理封裝（datasets / files） | PR #1 規劃 | ⏳ PR #3 才做 |
| **R6 (v1)** | `retriever_resources` → `message.metadata.references` | PR #1 | ✅ |
| **R7 (v1)** | 內部錯誤 → OpenAI envelope (401/404/400/502/504) | PR #1 | ✅ |
| **R1 (v2)** | `POST /v1/embeddings` (OpenAI 相容，bypass Dify) | PR #2 | ✅ 已上 PR branch |
| **R6 (v2)** | OpenAI 2025 deprecation aliases: `max_completion_tokens`、`safety_identifier` | PR #2 | ✅ |
| — | `/v1/models` `owned_by` 改成發布者身份（修 tenant 資訊外洩） | PR #2 | ✅ |
| — | 上游 embedding 4xx 透傳（修 review-2 P2） | PR #2 | ✅ |
| — | LLM/embedding cross-list id collision 驗證（修 review-2 P2） | PR #2 | ✅ |
| **R2 (v2)** | `/v1/datasets` CRUD | PR #3 預定 | ⏳ |
| **R3 (v2)** | `/v1/files` multipart 上傳 | PR #3 預定 | ⏳ |
| **R4 (v2)** | `/v1/datasets/{id}/retrieve` 純檢索 | PR #3 預定 | ⏳ |
| **R5 (v2)** | Embedding model lazy-provisioning | PR #3 預定 | ⏳ |
| **R7 (v2)** | Streaming `reasoning_content`（Qwen3 `<think>` 透傳） | PR #3 預定 | ⏳ |

---

## 3. 模組架構 — 程式碼長什麼樣

### 3.1 套件樹

```
gateway/src/gateway/
│
├── main.py             ← FastAPI factory: middleware + router + handler 組裝
├── config.py           ← 從環境讀 Settings（timeouts、log level、registry 路徑）
│
├── errors.py           ← 領域錯誤 hierarchy ─ 都會被 handler 映射到 OpenAI envelope
│                          │
│                          ├─ GatewayError (base)
│                          ├─ InvalidSdkKeyError      → 401
│                          ├─ UnknownModelError       → 404
│                          ├─ InvalidRequestError     → 400
│                          ├─ UpstreamClientError     → 4xx (NEW, PR #2)
│                          ├─ DifyUpstreamError       → 502
│                          ├─ DifyTimeoutError        → 504
│                          └─ ServiceUnavailableError → 503
│
├── registry.py         ← 客戶設定載入（YAML）+ Pydantic 驗證
│                          │
│                          ├─ ModelEntry           (LLM 條目，含 owner)
│                          ├─ EmbeddingModelEntry  (NEW, PR #2)
│                          ├─ DifyConnection
│                          └─ CustomerEntry        (跨 list collision 驗證, PR #2)
│
├── schemas.py          ← OpenAI 相容 Pydantic models
│                          │
│                          ├─ ChatCompletionRequest  (含 R6 alias 欄位)
│                          ├─ ChatCompletionResponse / Chunk
│                          ├─ EmbeddingsRequest / Response  (NEW, PR #2)
│                          └─ ModelInfo / ModelList
│
├── middleware/
│   ├── auth.py         ← Bearer token → registry → request.state.customer
│   └── logging.py      ← X-Request-Id 注入 + structlog 設定
│
├── routers/
│   ├── chat.py         ← /v1/chat/completions (blocking + streaming pre-flight)
│   ├── embeddings.py   ← /v1/embeddings (NEW, PR #2)
│   └── models.py       ← /v1/models (owned_by = publisher，修 tenant leak)
│
├── dify/
│   ├── client.py       ← Dify HTTP client（cookie + CSRF console session）
│   ├── app_manager.py  ← Lazy-build + TTL cache per (customer, model)
│   └── dsl.py          ← Dify App DSL 建構
│
├── embeddings/         (NEW, PR #2)
│   └── client.py       ← OpenAI-相容 embedding endpoint 上游 httpx 呼叫
│
└── streaming/
    └── converter.py    ← Dify SSE → OpenAI chunks（含 reference 末尾掛載）
```

### 3.2 一個請求生命週期 — `POST /v1/chat/completions`

```
client → /v1/chat/completions   Authorization: Bearer <sdk_key>
   │
   ├─ LoggingMiddleware              建 request_id（或 echo X-Request-Id）
   │
   ├─ AuthMiddleware                 extract Bearer → registry.lookup
   │                                  → request.state.customer = CustomerEntry
   │                                  失敗：直接 JSON 401（不能 raise，因 middleware
   │                                  在 ExceptionMiddleware 之外）
   │
   ├─ routers.chat.chat_completions
   │     • Pydantic 驗 ChatCompletionRequest
   │     • effective_user = safety_identifier > user > <customer_id>:<request_id>
   │     • app_manager.get_app_key(customer, model)
   │         - 命中 TTL cache 或 lazy-build 一個新 Dify App
   │     • body.stream == True →
   │           dify_client.open_chat_stream(...)   ← pre-flight：先進 context manager
   │           StreamingResponse 包 dify_to_openai_chunks 轉換器
   │       body.stream == False →
   │           dify_client.chat_messages_blocking(...)
   │           組 ChatCompletionResponse（含 references、usage、metadata）
   │
   └─ exception handler
         • GatewayError       → status_code + OpenAI envelope
         • RequestValidationError → 400 invalid_request + 完整 errors list
```

### 3.3 一個請求生命週期 — `POST /v1/embeddings`（PR #2 新功能）

```
client → /v1/embeddings   { model: "bge-m3", input: "..." }
   │
   ├─ Logging + Auth 同上
   │
   ├─ routers.embeddings.create_embeddings
   │     • Pydantic 驗 EmbeddingsRequest
   │     • customer.find_embedding_model("bge-m3") → EmbeddingModelEntry
   │         (找不到 → 404 model_not_found，跟 chat 共用同一個 code)
   │     • upstream_body.model 改寫成 entry.name（上游 served name）
   │     • upstream_body.user = effective_user 或 fallback
   │
   ├─ embeddings.client.invoke_embeddings
   │     • httpx POST <endpoint_url>/embeddings
   │     • 200            → resp.json()
   │     • 4xx            → UpstreamClientError（status 保留透傳，body 進 message）
   │     • 5xx            → DifyUpstreamError (502)
   │     • timeout/transport → DifyTimeoutError (504) / DifyUpstreamError
   │
   └─ response.model 改回客戶送進來的 id（讓客戶看見一致的名字）
```

**關鍵設計**：embeddings **不走 Dify**。Dify 在純向量化下沒有任何 add-on 價值
（沒對話、沒 RAG、沒 agent），多繞只會多 latency 跟失敗點。Gateway 還是要
經過為了：(a) 共用 SDK key、(b) per-customer 路由、(c) 統一 logging。

---

## 4. Registry 設定範例

```yaml
# gateway/registry.yaml — 客戶設定，啟動時讀進來
customers:
  - sdk_key: "bsa_test_local_a0b727..."
    customer_id: "test-customer"
    dify:
      base_url: "http://localhost:80"
      console_email: "admin@local"
      console_password: "..."
      dataset_api_key: "..."
    models:
      - id: "gemma-3n-e4b"                # 客戶看到的 id
        provider: "langgenius/openai_api_compatible/openai_api_compatible"
        name: "gemma-3n-e4b"               # 上游 served name
        owner: "google"                    # /v1/models owned_by 顯示
        completion_params:
          temperature: 0.3
    embedding_models:                       # PR #2 新欄位
      - id: "bge-m3"
        name: "bge-m3"
        owner: "BAAI"
        endpoint_url: "http://localhost:9997/v1"   # vllm-embed
        api_key: "EMPTY"                     # vLLM 不驗 key
        dimensions: 1024                     # 資訊性，方便客戶讀 /v1/models
```

Pydantic v2 `extra="forbid"` + `frozen=True` — 任何 typo 啟動就會炸，不會
變 silent default。

---

## 5. 測試清單 — 每個測試在驗什麼、為什麼必須有

> **129 tests, 100% PASSED**。所有測試都用 `pytest-asyncio` + `respx`
> （httpx mock）+ FastAPI `ASGITransport`，沒有真正打外部服務。
>
> 「為什麼必須測」這欄回答的是：**這個測試刪掉，會放掉哪個 bug**。

### 5.1 `test_auth.py` — SDK key 解析（PR #1）

| 測試 | 在驗什麼 | 為什麼必須測 |
|---|---|---|
| `TestExtractSdkKey::*` | 各種 Authorization header 形狀：缺 header、缺 prefix、空 key、有 trailing space | 解析錯誤的話會炸 500 而不是 401；前端會無法正常引導使用者重設 key |

### 5.2 `test_registry.py` — 客戶設定載入（PR #1 + PR #2）

| 測試 | 在驗什麼 | 為什麼必須測 |
|---|---|---|
| `TestRegistryFromEntries::test_lookup_returns_entry_for_known_sdk_key` | SDK key → CustomerEntry | 整個 auth + routing 的根本 |
| `test_lookup_returns_none_for_unknown_key` | 未知 key → None | 必須在 auth 層擋下，不能 silent fallthrough |
| `test_contains` / `test_len_counts_entries` | `in registry` / `len(registry)` 行為 | 啟動 log 跟 health 用到 |
| `test_duplicate_sdk_key_rejected` | 重複 sdk_key YAML 啟動就炸 | 不擋的話兩個客戶共用 key，計費 / quota 邏輯爛掉 |
| `TestCustomerEntryValidation::test_at_least_one_model_required` | 客戶至少要有一個 model | 沒模型的客戶啟動不該載入；防止 silent 0-model 客戶 |
| `test_duplicate_model_ids_rejected` | 同客戶 model id 唯一 | 不擋會看 `find_model` 順序，silent bug |
| `test_find_model_returns_match` / `test_find_model_returns_none_when_missing` | model lookup 行為 | router 直接依賴 |
| `test_default_model_is_first` | 不傳 model 時 fallback | PR #1 R3 client 不指定時的預設行為 |
| `test_extra_fields_forbidden` | YAML 多打 typo → 啟動炸 | 防止 `endpiont_url` 之類 typo 變 silent miss |
| `test_embedding_models_default_to_empty_list` | PR #1 registry 沒這 block 也能載 | **backward compat**：PR #2 升級不能把舊客戶炸了 |
| `test_embedding_model_lookup` | `find_embedding_model` 正常 | embeddings router 整個吃這個 helper |
| `test_duplicate_embedding_model_ids_rejected` | 同 list 重複 id 啟動炸 | 跟 LLM 一樣，silent bug 防火牆 |
| `test_id_collision_across_llm_and_embedding_rejected` | **review-2 P2**：跨 list 同 id 啟動炸 | `/v1/models` flatten 後會重複 entry，且 chat vs embeddings 默默路到不同後端 |
| `test_disjoint_llm_and_embedding_ids_accepted` | 不衝突就放行 | 防止驗證器擋到合法 config |
| `TestEmbeddingModelEntry::test_defaults` | `owner`="ai-sdk-gateway", `api_key`="EMPTY", `dimensions`=None | 預設值是設計合約 |
| `test_owner_can_be_publisher` / `test_extra_fields_forbidden` / `test_dimensions_must_be_positive` | 各種驗證行為 | 同 LLM ModelEntry 的守門 |
| `TestRegistryFromYaml::test_loads_valid_yaml` / `test_missing_file_raises` / `test_invalid_yaml_raises` / `test_root_without_customers_key_raises` / `test_schema_violation_in_yaml_raises` / `test_duplicate_sdk_key_in_yaml_raises` | YAML 載入流程的各種失敗都會清楚報錯 | startup-time 失敗訊息要明確，半夜值班才不會 debug 一小時 |

### 5.3 `test_errors.py` — 錯誤層級（PR #1）

| 測試 | 在驗什麼 | 為什麼必須測 |
|---|---|---|
| `test_error_attributes[...]` (parametrized 7 種) | 每個 GatewayError 子類的 `status_code` / `code` / `message` | 改錯一個就改錯 client 看到的 status，外部 SDK 邏輯會跟著爛 |
| `test_envelope_shape` | `to_openai_envelope()` 結構符合 OpenAI 規範 | 客戶用 try/except 接 `OpenAIError` 子類，envelope 不對會被當成 generic 500 |
| `test_envelope_omits_param_when_not_provided` | `param=None` 時不寫死字串 | 防止 `"param": "None"` 這種 stringification bug |
| `test_subclass_relationship` | `issubclass(*, GatewayError)` | exception handler 用 `isinstance` 統一接，要確保繼承沒斷 |

### 5.4 `test_dify_client.py` — Dify HTTP client（PR #1）

| 測試 | 在驗什麼 | 為什麼必須測 |
|---|---|---|
| `test_chat_messages_blocking_returns_parsed_body` | 正常 blocking 呼叫回應解析 | 整個 chat 路徑依賴這個 |
| `test_chat_messages_blocking_includes_conversation_id_when_provided` / `..._omits_conversation_id_when_absent` | conversation_id 條件性傳遞 | Dify 對「空字串 vs 不傳」處理不同；錯了會建錯對話 |
| `test_chat_messages_blocking_raises_on_5xx` / `..._raises_on_timeout` | upstream 失敗 → 對應的 GatewayError | client 不該把 5xx silent return |
| `test_console_login_returns_session_from_cookies` | **review-1 P1 修正點**：Dify 用 cookie 不是 token | 沒這個測試會錯把空字串當 token，整個 lazy-build 失敗 |
| `test_console_login_sends_base64_encoded_password` | 密碼 base64 編碼 | Dify console 接受的格式，不照規定每次都 401 |
| `test_console_login_supports_host_prefixed_cookies` | HTTPS 部署的 `__Host-` cookie 前綴 | **review-2 P1**：production HTTPS 直接全爛，這是部署相容性的硬要求 |
| `test_console_calls_echo_host_prefixed_cookie_names` | 後續呼叫要 echo `__Host-` 名 | 同上，缺一不可 |
| `test_console_login_missing_cookies_raises` | 沒拿到 cookie → 報錯 | 避免拿到空 session 還繼續往下跑 |
| `test_console_import_app_sends_csrf_header_and_cookies` | CSRF 防護完整實作 | Dify 強制 CSRF；少了會 403 |
| `test_console_import_app_accepts_id_field` | 接受 Dify 的 `id` 欄位 | Dify 版本變動的相容性 |
| `test_console_create_app_api_key` | 建 App API key 流程 | Lazy-build 的最後一步，缺了沒辦法 chat |
| `test_console_delete_app_treats_404_as_idempotent` | 刪不存在的 App 不報錯 | GC 路徑會跑這個；race condition 下不擋 |
| `test_console_delete_app_raises_on_other_failures` | 其他失敗要報錯 | 跟 404 idempotent 對應，不能整段吞 |
| `test_open_chat_stream_yields_lines` | 串流正常 yield | 串流功能的基石 |
| `test_open_chat_stream_raises_before_yielding_on_5xx` | **review-2 P2 修正點**：5xx 在第一個 byte 前就 raise | 不然 SSE header 已送，client 看到斷流不是 error JSON |
| `test_open_chat_stream_raises_on_connect_timeout` | 連線 timeout → DifyTimeoutError | 跟上行對應 |

### 5.5 `test_app_manager.py` — Lazy-build + TTL cache（PR #1）

| 測試 | 在驗什麼 | 為什麼必須測 |
|---|---|---|
| `test_first_call_builds_app` | 第一次 (customer, model) 建一個 App | 整個 lazy 策略的入口 |
| `test_second_call_hits_cache` | 第二次命中 cache，不重建 | 避免每個 request 都打 Dify 建 App（latency + Dify 負載） |
| `test_unknown_model_raises` | 不在 registry 的 model → UnknownModelError | client 看到 404 不是 500 |
| `test_concurrent_first_calls_build_once` | 同 key 併發只建一次 | 高併發剛冷啟動的場景，多次建會造出垃圾 App 在 Dify |
| `test_different_models_build_separate_apps` | 不同 model 各自一個 App | 模型切換隔離 |
| `test_session_refresh_on_auth_failure` | 401 後重新登入並 retry | Dify console session TTL 過期不能整段請求爛掉 |
| `test_gc_evicts_idle_entries` | 閒置太久的 entry 被回收 | 不回收 cache 會無限長 + Dify 端 App 數爆掉 |
| `test_gc_keeps_recently_used_entries` | 最近用過的不回收 | GC 策略沒誤殺 hot key |
| `test_gc_swallows_delete_errors` | 刪 Dify App 失敗時 GC 不爆 | GC 是 background loop，掛了會 leak |

### 5.6 `test_chat_blocking.py` — `/v1/chat/completions` blocking（PR #1 + PR #2 R6）

| 測試 | 在驗什麼 | 為什麼必須測 |
|---|---|---|
| `test_blocking_happy_path` | 完整 happy path，含 references + usage + metadata | golden path 守門 |
| `test_blocking_unknown_sdk_key_returns_401` | 假 key → 401 OpenAI envelope | 整個 auth 防線 |
| `test_blocking_missing_authorization_returns_401` | 沒 header → 401 | 同上不同切點 |
| `test_blocking_unknown_model_returns_404` | 假 model → 404 model_not_found | 客戶錯打模型名要立刻知道，不要走到後端 |
| `test_blocking_no_user_message_returns_400` | messages 沒 user → 400 | Dify 沒 user message 會炸；要在 gateway 早攔 |
| `test_blocking_forwards_history_and_conversation_id` | 多輪 history + conversation_id 傳到 Dify | OpenAI 是 stateless、Dify 是 stateful，銜接邏輯要對 |
| `test_pydantic_validation_error_returns_openai_envelope` | **review-3 P2**：Pydantic 422 → 400 invalid_request | 缺這個守門客戶看到 FastAPI 預設 `{"detail":[...]}` 不是 OpenAI envelope |
| `test_validation_error_out_of_range_temperature` | `temperature > 2.0` → 400 | 同上不同欄位 |
| `test_safety_identifier_preferred_over_user` | **PR #2 R6**：兩個欄位都送，Dify 收到新欄位的值 | unit test 過不代表 router 真的用 effective_user，要 E2E |
| `test_user_field_alone_still_accepted` | 只送舊 user 還是 OK | backward compat 守門 |
| `test_max_completion_tokens_accepted` | 新欄位過 Pydantic 不被當 extra forbidden | 沒宣告會 400，整段請求被拒 |
| `test_both_max_tokens_fields_accepted_together` | 兩個 max 都送不會 400 | SDK 升級期混送的容錯 |
| `test_extra_body_llm_model_overrides_app_selection` | **review-3 P2**：`extra_body.llm_model` 覆蓋 body.model | 不修客戶想動態切模型完全爛掉 |
| `test_extra_body_llm_model_unknown_returns_404` | 假 llm_model → 404 | 對應上行的負面測試 |
| `test_request_id_echoed_in_response_header` | `X-Request-Id` 入→出回傳 | 客戶要能用這個 trace 一個請求；缺了 debug 痛苦 |

### 5.7 `test_chat_streaming.py` — `/v1/chat/completions` 串流（PR #1）

| 測試 | 在驗什麼 | 為什麼必須測 |
|---|---|---|
| `test_streaming_yields_openai_chunks` | 串流的 chunk 結構符合 OpenAI 規範 | 客戶端用 SDK 解析會炸 |
| `test_streaming_unknown_model_returns_404_json` | 串流 + 假 model → 404 JSON（不是壞掉的 SSE） | 客戶端看不到 error 會以為連線斷 |
| `test_streaming_passes_conversation_id_to_dify` | conversation_id 串流模式也傳遞 | 跟 blocking 對齊 |
| `test_streaming_dify_5xx_returns_502_json_not_broken_sse` | **review-2 P2**：Dify 5xx 在 SSE 開始前就轉成 502 JSON | streaming response 一旦 header 送出，就再也轉不回 error JSON。pre-flight 是這個架構的核心 |
| `test_streaming_dify_timeout_returns_504_json` | timeout 同上 → 504 | 對應 5xx |

### 5.8 `test_sse_converter.py` — Dify SSE → OpenAI chunks 轉換器（PR #1）

| 測試 | 在驗什麼 | 為什麼必須測 |
|---|---|---|
| `TestParseDifySseLine::*` (6 case) | 各種 SSE line 解析：data JSON、blank、`[DONE]`、bad JSON、非 data line、array JSON | converter 第一層；錯了整條串流爛 |
| `test_message_chunks_translated_to_openai_chunks` | Dify message event → OpenAI chunk | 主功能 |
| `test_references_attached_to_final_chunk` | `retriever_resources` 掛在最後一個 chunk | client 要拿到 references 才能顯示出處 |
| `test_error_event_short_circuits_with_content_filter_finish` | Dify error event → `finish_reason: content_filter` | 不轉的話 client 看到串流神祕中斷 |
| `test_ping_events_ignored` | Dify ping/heartbeat → 不轉發 | 不然會多送雜訊到 client |
| `test_empty_answer_chunks_skipped` | 空字串 chunk → 跳過 | OpenAI 規範不該送 empty delta，會讓 SDK 計算錯誤 |

### 5.9 `test_embeddings.py` — `/v1/embeddings`（PR #2 R1）

| 測試 | 在驗什麼 | 為什麼必須測 |
|---|---|---|
| `test_embeddings_single_string_input` | 單字串 input 完整 happy path | golden path |
| `test_embeddings_list_input` | list input → list response，index 對 | batch 上向量庫場景；index 錯了向量配錯文件 |
| `test_embeddings_forwards_optional_params` | `encoding_format` / `dimensions` 透傳 | gateway 不該自作主張濾掉 |
| `test_embeddings_safety_identifier_preferred` | R6 alias 兩欄位都送，新欄位贏 | 同 chat 端對應 |
| `test_embeddings_user_fallback_to_customer_request` | 都沒給時 gateway 生 fallback | Dify/vLLM 強制要 user，缺了上游 400 |
| `test_embeddings_unknown_model_returns_404` | 假 embedding model → 404 model_not_found | 跟 chat 共用 envelope，client 寫一個 handler 就夠 |
| `test_embeddings_missing_auth_returns_401` | 沒 Bearer → 401 | 確認 middleware 也保護新 router |
| `test_embeddings_chat_model_id_not_treated_as_embedding` | LLM id 不能 embed | 兩個 namespace 不可混用，混了給空向量 |
| `test_embeddings_upstream_5xx_returns_502` | 上游 503 → 502 dify_upstream_error | 上游真的掛，client 要看到正確 outage 訊號 |
| `test_embeddings_upstream_4xx_passes_through[400/413/422]` | **review-2 P2**：4xx 透傳保留原 status + message | 不修的話客戶 input 爛被當成 outage，會 retry → 越打越爛 |
| `test_embeddings_upstream_timeout_returns_504` | timeout → 504 dify_timeout | 跟 5xx 用不同 code，SLO/alerting 分流 |
| `test_models_endpoint_includes_embedding_models` | `/v1/models` flatten LLM + embedding | OpenAI list 是 type-agnostic，少了客戶 list models 看不到向量模型 |

### 5.10 `test_models_endpoint.py` — `/v1/models`（PR #1 + PR #2）

| 測試 | 在驗什麼 | 為什麼必須測 |
|---|---|---|
| `test_models_endpoint_returns_customer_models` | 每個 model 各自的 owned_by 對 | **review-1 P1 修正**：原 `all(...)` 假設被 fixture 變動打爆，現用 per-id pin 防再爆 |
| `test_models_endpoint_owned_by_does_not_leak_customer_id` | regression：owned_by ≠ customer_id | **PR #2 跨 tenant 資訊外洩防火牆**；任何重構不小心 leak 立刻擋下 |
| `test_model_entry_owner_defaults_to_gateway` | 未指定 owner → "ai-sdk-gateway" | 安全 fallback |
| `test_model_entry_owner_can_be_overridden` | `owner="Qwen"` 可覆寫 | 接 publisher 名稱（OpenAI/Meta/Qwen/BAAI 等） |
| `test_models_endpoint_requires_auth` | 沒 Bearer → 401 | 認證守門 |
| `test_health_endpoint_does_not_require_auth` | `/health` 不需要 auth | k8s liveness probe / LB health check 用 |

### 5.11 `test_schemas.py` — R6 alias 純 unit 邏輯（PR #2）

| 測試 | 在驗什麼 | 為什麼必須測 |
|---|---|---|
| `TestEffectiveMaxTokens::test_only_old_field_returns_old` | 只 max_tokens → effective == old | backward compat |
| `..._only_new_field_returns_new` | 只 max_completion_tokens → effective == new | 新 SDK 預期 |
| `..._both_set_new_wins` | 兩個都送 → 新欄位贏 | OpenAI 官方 migration 規定 |
| `..._neither_set_returns_none` | 都不送 → None | 防 default 默默變動 |
| `TestEffectiveUser` 4 個對應 | user vs safety_identifier 4 種組合 | 同 max_tokens 邏輯 |

> 為什麼這 8 個是 pure unit、不是 E2E？因為 alias 規則錯一個就是長遠投訴源；
> 在最低層先 lock 死，再用 `test_chat_blocking::test_safety_identifier_preferred_over_user`
> 確認 router 真的有用 property。

---

## 6. Codex Review 歷史（5 輪獨立 AI 程式碼審查）

PR #1 跑了 3 輪、PR #2 跑了 2 輪。每輪用 OpenAI Codex CLI v0.130.0
`model_reasoning_effort=high`，當第二意見抓 Claude 看不見的 bug。

### 6.1 PR #1 — feat-ai-sdk-v1

| Round | Findings | 主要修正 |
|---|---|---|
| 1 | 1 × P1, 1 × P2 | Dify console 用 cookie + CSRF 而非 token；middleware 不能 raise 要直接 return JSON |
| 2 | 1 × P1, 1 × P2 | HTTPS 部署的 `__Host-` cookie 前綴 round-trip；streaming 用 pre-flight 把 upstream 失敗在 SSE 開始前轉成 JSON |
| 3 | 0 × P1, 2 × P2 | `extra_body.llm_model` 覆蓋 App 選擇；Pydantic 422 → OpenAI envelope |

Round 3 0 P1 = 收斂，PR #1 合進 main。

### 6.2 PR #2 — feat-ai-sdk-v2

| Round | Findings | 主要修正 |
|---|---|---|
| 1 | 1 × P1 | `test_models_endpoint` 的 `all(...)` 假設被 fixture 變動打爆 |
| 2 | 0 × P1, 2 × P2 | 上游 embedding 4xx 不能壓成 502；LLM/embedding cross-list id collision 要擋 |
| 3 | 0 × P1, 2 × P2 | round-2 4xx 透傳太寬：401/403/404/429 是 gateway-side failure，要拆出；2xx body 要驗（HTML / array 不能炸 500） |

Round 3 收斂，三輪累計 1 P1 + 6 P2 全修，PR #2 準備 push 跟開 PR。

每輪都有完整的 `review-N.md`（codex 原始 findings）+ `review-N-response.md`
（我這邊的處理紀錄），都在 `ai-review/reviews/feat-ai-sdk-v{1,2}/`。

---

## 7. 端到端驗證（Jetson AGX Thor 實機）

跑在 production-shape 硬體上：NVIDIA Jetson AGX Thor、Gemma 3n E4B 當 LLM、
bge-m3 當 embedding，所有 endpoint 都通：

| 步驟 | 端點 | 結果 |
|---|---|---|
| 1 | `GET /health` | ✅ `{"status":"ok"}` |
| 2 | `GET /v1/models` | ✅ 看到 `gemma-3n-e4b` (owned_by=google) + `bge-m3` (owned_by=BAAI) |
| 3 | `POST /v1/embeddings` `{model:"bge-m3", input:"基站告警"}` | ✅ dim=1024 向量，response.model echo `bge-m3`，usage 有 prompt_tokens |
| 4 | `POST /v1/chat/completions` `{model:"gemma-3n-e4b", ...}` | ✅ Gemma 回 817 tokens 繁體中文工程回答，整條 Gateway → Dify → vLLM 通 |

Streaming + OpenAI Python SDK 完整 E2E 還在 todo（在 `feat/ai-sdk-gateway-pr2`
本地驗證後會補上紀錄）。

---

## 8. 後續 Roadmap（PR #3）

| Spec ID | 內容 |
|---|---|
| R2 (v2) | `/v1/datasets` CRUD（建/列/刪客戶的知識庫） |
| R3 (v2) | `/v1/files` multipart 上傳檔案進知識庫（PDF / Word / Markdown 自動向量化） |
| R4 (v2) | `POST /v1/datasets/{id}/retrieve` 純檢索通道（hit-testing API） |
| R5 (v2) | Embedding model lazy-provisioning（dataset 第一次建立鎖定 embed model；registry 已支援） |
| R7 (v2) | Streaming `reasoning_content` chunks（Qwen3 `<think>` 透傳給 client） |

PR #3 接 PR #2 的 branch 繼續做，預期 +800~1000 LOC。

---

## 9. 參考文件

| 文件 | 位置 |
|---|---|
| PR #1 spec | `ai-review/specs/feat-ai-sdk-v1.md` |
| PR #2 spec | `ai-review/specs/feat-ai-sdk-v2.md` |
| PR #1 review 全紀錄 (3 輪) | `ai-review/reviews/feat-ai-sdk-v1/` |
| PR #2 review 全紀錄 (2 輪 + 仍進行中) | `ai-review/reviews/feat-ai-sdk-v2/` |
| PR #2 工程內部實作紀錄（更細） | `ai-review/reviews/feat-ai-sdk-v2/IMPLEMENTATION.md` |
| Registry 範例設定 | `gateway/registry.example.yaml` |

---

> **文件版本**：2026-05-18，對應 `feat/ai-sdk-gateway-pr2` HEAD `793cdcdec`。
> 之後有新 commit / 新功能進來請更新本檔。
