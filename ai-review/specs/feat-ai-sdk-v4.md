# Feature: AI SDK Gateway PR #4 — Shared-Dify Deployment Mode

## Feature ID

`ai-sdk-gateway-shared-mode`

## Owner

luluwen

## Status

- [x] Draft
- [ ] Ready for implementation
- [ ] In review
- [ ] Approved
- [ ] Merged

## Related PRs

- PR #1: `ai-review/specs/feat-ai-sdk-v1.md`（chat path）
- PR #2: `ai-review/specs/feat-ai-sdk-v2.md`（embeddings + aliases）
- PR #3: `ai-review/specs/feat-ai-sdk-v3.md`（KB + reasoning）

---

## Goal

讓 Gateway 同時支援兩種 Dify 部署拓撲，**讓 mentor / 內部 PoC / 教學
情境**可以用較低成本（一份 Dify 同時服務多客戶）試水，**production 仍
維持每客戶獨立 Dify 的合規路徑**。

模式由 `registry.yaml` 的 `dify.mode` 欄位顯示切換，**不引入 fork**，
共用同一份 Gateway code。

## Non-goals

- **不**改 Dify 核心源碼或 fork Dify
- **不**做 row-level security / SQL 層的真正隔離（Dify 不支援）
- **不**做 rate limiting（PR #5）
- **不**支援動態 mode 切換（要改 mode 需重啟 Gateway）

## User Story

```
As 系統管理員
I want 在 registry.yaml 用 `dify.mode: shared` 配置同 Dify 多客戶
So that 我能在一台機器上跑 demo / 教學 / 同部門內部多人共用而不需要
   為每個人開獨立 Dify 部署
```

```
As 開發者
I want Gateway 自動處理 shared mode 下的 App / Dataset 命名與過濾
So that 客戶端 API 完全透明 — 用同一個 OpenAI SDK code 跑 dedicated /
   shared 都能正常運作
```

---

## Deployment-Mode 對比

| 面向 | `dedicated`（PR #1-#3） | `shared`（PR #4 新增） |
|---|---|---|
| Dify 部署數 | per customer | 1 份服務多客戶 |
| Workspace 數 | per customer | 1 個（license 邊界） |
| Per-customer DB / Redis | ✅ 完全分開 | ❌ 共用 |
| 信任邊界 | DB / 部署層 | Gateway 軟性隔離（App / Dataset name prefix） |
| 合規場景 | 付費客戶 / production | 內部 demo / PoC / 教學 |
| Resource 成本 | N × 10 GB RAM | 共用 |
| License 風險 | 0 | 0（仍是單 workspace；Dify 把 tenant 定義成 workspace） |

---

## Gateway 軟性隔離設計（PR #4 核心）

### 信任邊界分析

**Gateway 能在 shared mode 控制什麼**：

| 隔離面 | 怎麼做 |
|---|---|
| **App 名稱衝突** | App 前綴 `{customer_id}-{model_id}` |
| **Dataset 名稱衝突** | Dataset 前綴 `{customer_id}__{name}` |
| **跨客戶 list dataset** | Dify response 按前綴過濾再回傳 |
| **跨客戶 get / delete / retrieve** | 操作前驗 `dataset.name.startswith(f"{customer_id}__")` |
| **跨客戶 file upload** | 同上，繼承 dataset 的 ownership 檢查 |
| **App-level API key** | Dify 本來就是 per-App `app-*` key，Gateway cache 已經按 (customer, model) 分開 |

**Gateway 無法控制（接受的限制）**：

- ❌ Postgres / Redis / S3 共用 — Dify code-level bug 仍可能跨租戶
- ❌ Workspace-level model provider key 共用
- ❌ Console admin 帳號可看光所有 App
- ❌ Resource exhaustion（noisy neighbor）— 部分待 PR #5 rate limiting 緩解

**安全等級結論**：shared mode 的隔離**只能防正常呼叫路徑下的跨客戶讀取**，
不防 Dify code bug、admin 帳號濫用、DB 直接讀取。**信任邊界仍然是
workspace**，所以絕對**不可**用於付費 / production / 合規場景。

---

## Requirements

### R1：Registry mode flag

- [ ] `DifyConnection` 加 `mode: Literal["dedicated", "shared"] = "dedicated"`
    - 預設 `dedicated`，舊 registry.yaml 不需要改也可繼續跑（backward compat）
    - 用 `extra="forbid"` 的 Pydantic 阻止 typo（`shred` 之類）
- [ ] Registry 驗證：所有共用同一 `base_url` 的客戶要嘛全 `dedicated`、要嘛
      全 `shared`；混用拒絕載入
    - 為什麼擋：避免一個 customer 走 prefix 路徑、另一個不走，結果同
      workspace 內 dataset 名稱可能撞到

### R2：App lazy-build 加 customer prefix

- [ ] `app_manager.AppManager.get_app_key(customer, model)` 在 shared mode 下
      建出 App 名稱 `f"gateway-{customer.customer_id}-{model_id}"`，dedicated
      mode 保持原樣 `f"gateway-{model_id}"`
- [ ] Cache key 已經是 `(customer_id, model_id)` 不用改
- [ ] GC 清理時用同樣的命名規則砍對 App

### R3：Datasets router 加 ownership

- [ ] **Create**：dataset name 自動加前綴 `{customer_id}__`
    - Customer 看到的 name 維持原樣（response 把前綴拿掉）
- [ ] **List**：query Dify → 只回前綴匹配的 dataset
    - Page / limit 在 Gateway 端二次過濾後再 page
- [ ] **Get / Delete / Retrieve**：先 `get_dataset` 驗 prefix；不符合 → 404
      `dataset_not_found`（**不能回 403**，會洩漏「這個 ID 確實存在但不屬於你」）
- [ ] **dedicated mode**：以上行為全部 no-op

### R4：Files router 繼承 dataset ownership

- [ ] Upload / list / delete 一律先驗 `dataset_id` 的 ownership（reuse R3 helper）
    - 不擋的話：customer A 知道 customer B 的 dataset UUID（透過 log / 猜）
      就能往裡塞檔案 / 刪檔案

### R5：Embedding model 限制

- [ ] Shared mode 下，registry 必須在 `dify` 區塊指定**全域**的
      `shared_embedding_model: { name, provider }`
    - 因為 embedding model 是 workspace-scoped，所有客戶用同一個
- [ ] 客戶若在 `POST /v1/datasets` body 帶 `embedding_model` 跟全域不符 →
      400 `invalid_request_error`，訊息提示 shared mode 限制
- [ ] dedicated mode：維持 PR #3 的 R5 邏輯（從客戶 `embedding_models` 解析）

### R6：Models endpoint 不洩漏其他客戶

- [ ] `/v1/models` 還是只回該客戶在 registry 內的 model / embedding —
      registry 已經是 per-customer，這項天然成立
- [ ] **驗證**：寫 regression test 防止未來不小心改成「列全 workspace 的 Apps」

---

## Acceptance Criteria

- [ ] Registry 載入：`dify.mode` 預設 `dedicated`、可顯示設 `shared`、typo 拒絕
- [ ] 同一 `base_url` 下 mode 一致性檢查：混用 → 啟動拒絕
- [ ] Shared mode 下兩個客戶呼叫 `POST /v1/datasets {name: "kb"}` →
      Dify 真的建出兩個不同 dataset（`tenant-a__kb` / `tenant-b__kb`）
- [ ] Shared mode 下 customer A `GET /v1/datasets` 看不到 customer B 的
      dataset；猜對 UUID 也 `GET / DELETE / retrieve` 不到（404）
- [ ] Shared mode 下 file upload 必須有合法 dataset_id ownership
- [ ] Shared mode 下兩個客戶 lazy-build 不同 model 都正常，App 名稱無 collision
- [ ] dedicated mode 完全沒有 regression（PR #1-#3 行為不變）
- [ ] Test coverage：dedicated + shared 都跑同一套核心測試

---

## Technical Notes

### registry schema 變動

```yaml
customers:
  - sdk_key: "bsa_a_..."
    customer_id: "tenant-a"
    dify:
      mode: "shared"                  # ← NEW (default "dedicated")
      base_url: "http://dify-shared.local"
      console_email: "shared-admin@example.com"
      console_password: "..."
      dataset_api_key: "ds-shared-key"
      # Shared mode 必填：workspace 全域的 embedding 模型
      shared_embedding_model:
        name: "bge-m3"
        provider: "langgenius/openai_api_compatible/openai_api_compatible"
    models: [...]
    embedding_models: [...]            # 仍可為 direct /v1/embeddings 路徑用
```

### App / Dataset 命名範例

| 客戶 | model_id | Shared mode App name | Dedicated mode App name |
|---|---|---|---|
| `tenant-a` | `gemma-3n-e4b` | `gateway-tenant-a-gemma-3n-e4b` | `gateway-gemma-3n-e4b` |
| `tenant-b` | `gemma-3n-e4b` | `gateway-tenant-b-gemma-3n-e4b` | `gateway-gemma-3n-e4b`（會跟 A 撞，所以才需要 prefix） |

Dataset 同理：`tenant-a__rsrp-manuals` vs `tenant-b__rsrp-manuals`。

### Helper module: `gateway/mode.py`

統一收一個 `IsolationStrategy` 介面，dedicated / shared 各一個實作。
Router 不直接判 mode flag，呼叫 strategy method（避免 if-else 散落各
router 變成漏網之魚）。

```python
class IsolationStrategy(Protocol):
    def app_name(self, customer_id: str, model_id: str) -> str: ...
    def dataset_name_to_dify(self, customer_id: str, name: str) -> str: ...
    def dataset_name_from_dify(self, customer_id: str, name: str) -> str | None: ...
        # None when not owned by this customer
    def dataset_belongs_to(self, customer_id: str, dify_name: str) -> bool: ...
```

兩個實作：`DedicatedStrategy`（all no-op）、`SharedStrategy`（前綴）。

### 為什麼 dataset 用 `__` 兩底線

避免跟 dataset name 內部正常的 `_` 衝突；兩底線 + customer_id slug
規則（`^[a-z0-9-]+$` 已限制）→ split 一定回正確值。

---

## Out of Bounds

- 不改 Dify 核心源碼
- 不引入新 Python 套件
- 不破壞 PR #1-#3 的 API contract
- 不做動態 mode 切換（要改 mode 必須重啟）
- 不防 Dify code 層 bug / admin 帳號濫用 / DB 直接讀取

## References

- PR #3 IMPLEMENTATION 紀錄：`ai-review/reviews/feat-ai-sdk-v2/IMPLEMENTATION.md`
- 父頁 Notion overview §4.5 雙模式對照
- Dify license 對 multi-tenant 定義：
  > "one tenant corresponds to one workspace. The workspace provides a separated area for each tenant's data and configurations."

## Spec 變更歷史

- 2026-05-19：建立初稿，回應 mentor 對 shared-Dify mode 的需求
