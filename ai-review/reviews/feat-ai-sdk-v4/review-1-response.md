# Review Response: feat-ai-sdk-v4 — Round 1

> Response to `reviews/feat-ai-sdk-v4/review-1.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 2 | 2 | 0 |
| [P2] | 2 | 2 | 0 |

四個都是 shared-mode isolation 設計層級的 gap — codex 一眼看穿我用 `startswith`
做 ownership 但沒驗 customer_id 的 slug、又用 field presence 判斷 mode、又把
filter 做在 page 內。這些都是 round 1 經典「想得不夠細」的問題。全修。

## Findings 處理紀錄

---

### Finding 1: [P1] Validate customer_id before prefix ownership checks

- **Severity**: [P1]
- **Codex 描述**:
  > In shared mode, ownership is decided with a plain
  > `startswith("{customer_id}__")`, but `CustomerEntry.customer_id` only
  > has `min_length=1` validation. If one customer is `acme` and another
  > is `acme__beta`, datasets for `acme__beta` are named
  > `acme__beta__...` and will also pass the `acme__` check, allowing
  > `acme` to list / get / delete the other customer's datasets.
- **影響檔案**: `gateway/src/gateway/mode.py:127`
- **動作**: ✅ Fixed

#### 驗證

Codex 完全正確。我在 mode.py 的 comment 寫「customer_id slug pattern
already forbids `__`」但**我從沒實際 enforce 那個 pattern** — 只有
`min_length=1` 不能阻止任何攻擊。具體攻擊路徑：

1. 註冊 customer A `customer_id="acme"`
2. 註冊 customer B `customer_id="acme__beta"`
3. B 建 dataset `kb` → Dify name = `acme__beta__kb`
4. A `GET /v1/datasets/{B 的 UUID}` →
   `strategy.dataset_belongs_to("acme", "acme__beta__kb")`
   → `"acme__beta__kb".startswith("acme__")` → **True**
   → 視為 A 的 dataset，允許 A 讀 / 刪

#### 修復內容

`gateway/src/gateway/registry.py`：customer_id 加 slug pattern：

```python
customer_id: str = Field(
    min_length=1,
    max_length=64,
    pattern=r"^[a-z0-9][a-z0-9-]*$",
)
```

- 小寫英數 + 連字號（hyphen）
- 不允許底線 → 確保 `{customer_id}__` 是無歧義的 prefix
- 大寫也擋（一致性，避免 "Acme" vs "acme" 混淆）
- 長度上限 64 防止異常輸入

既有的 customer_id（如 `test-a`、`tenant-a`、`customer-a`）都符合。**現有 PR #1-#3
測試 + production registry 不受影響**。

#### 測試

`test_shared_mode.py::TestReviewFix_CustomerIdSlug`：
- `test_customer_id_with_underscore_rejected` — `acme_beta` 拒絕
- `test_customer_id_with_double_underscore_rejected` — **正是 codex 講的 attack** `acme__beta` 拒絕
- `test_customer_id_uppercase_rejected` — `Acme` 拒絕
- `test_customer_id_hyphen_lowercase_accepted` — `tenant-a-1` 通過

---

### Finding 2: [P1] Normalize missing dataset errors in ownership checks

- **Severity**: [P1]
- **Codex 描述**:
  > When this `get_dataset` call returns Dify's 404, it propagates as
  > `UpstreamClientError` with code `upstream_invalid_request`, while an
  > existing-but-foreign dataset raises `UnknownDatasetError` with code
  > `dataset_not_found`. In shared mode, a caller who can try dataset
  > UUIDs can therefore distinguish "does not exist" from "exists but
  > not yours".
- **影響檔案**: `gateway/src/gateway/routers/datasets.py:177-180` +
  `gateway/src/gateway/routers/files.py:207`
- **動作**: ✅ Fixed

#### 驗證

我設計時想到 cross-customer 用 404 不洩漏存在性，但忘了 missing UUID
也會 404 — **如果它們 envelope 不一樣，照樣洩漏**。
Codex 透過 trace `_verify_dataset_ownership` 兩條路徑發現的：

| 情境 | 之前的 envelope | 現在 |
|---|---|---|
| Foreign UUID（屬於別人） | `404 dataset_not_found` | 同左 |
| Missing UUID（不存在） | `404 upstream_invalid_request` | **`404 dataset_not_found`**（normalized） |

#### 修復內容

`_verify_dataset_ownership`（datasets.py）跟
`_verify_dataset_ownership_for_files`（files.py）都加 try/except：

```python
try:
    meta = await dify_client.get_dataset(...)
except UpstreamClientError as exc:
    if strategy.is_shared and exc.status_code == 404:
        raise UnknownDatasetError(
            f"dataset '{dataset_id}' not found",
            param="dataset_id",
        ) from exc
    raise
```

只在 shared mode 做 normalization。dedicated mode 保留上游 envelope —
dedicated customer 只能看到自己的 dataset，所以「不存在 vs 別人擁有」
這個差別本來就無從利用。

#### 測試

`TestReviewFix_DatasetNotFoundNormalization`：
- `test_shared_get_missing_uuid_returns_dataset_not_found` — shared 模式
  下 missing UUID → `dataset_not_found`（不再 `upstream_invalid_request`）
- `test_shared_file_upload_missing_dataset_returns_dataset_not_found` —
  同樣 normalization 在 files.py
- `test_dedicated_get_missing_uuid_keeps_upstream_envelope` —
  **regression**：dedicated mode envelope 不變（仍 `upstream_invalid_request`）

---

### Finding 3: [P2] Page shared dataset lists after filtering

- **Severity**: [P2]
- **Codex 描述**:
  > In shared mode this still returns Dify's workspace-wide `has_more`
  > while `total` is only the count of owned items on the current
  > upstream page. If page 1 is filled with other customers' datasets,
  > this customer can get `data=[]`, `total=0`, `has_more=true` even
  > when their own datasets are on later pages.
- **影響檔案**: `gateway/src/gateway/routers/datasets.py:284-287`
- **動作**: ✅ Fixed

#### 驗證

兩個問題：
1. **客戶看不到自己的 datasets**：customer A 的 datasets 在 Dify 第 5 頁，
   客戶 A request page=1 → 拿到空陣列。完全不能用。
2. **`has_more=true` 洩漏**：客戶 A 沒有自己的 datasets，但 Dify
   workspace 還有其他客戶的，回應 `has_more=true` → 客戶 A 知道
   workspace 還有別人的資料。

我原本的設計理由是「filter 在 page 內」省 roundtrip。Codex 指出這個
trade-off 不可接受 — soft isolation 的 contract 就是不洩漏 workspace
state，這比省一次 Dify call 重要。

#### 修復內容

新 helper `_collect_owned_datasets`：在 shared mode 下，遍歷 Dify pagination
（每頁 100，最多 100 頁 = 10000 datasets workspace-wide 安全上限），
累積屬於這客戶的 datasets，再 client-side paginate。

```python
async def _collect_owned_datasets(...) -> list[dict]:
    owned = []
    for dify_page in range(1, _DIFY_LIST_MAX_PAGES + 1):
        resp = await dify_client.list_datasets(page=dify_page, limit=100, ...)
        for d in resp.get("data") or []:
            if strategy.dataset_belongs_to(customer_id, d["name"]):
                owned.append(d)
        if not resp.get("has_more"):
            break
    else:
        logger.warning("datasets.shared_list.cap_hit", ...)
    return owned
```

`list_datasets` 改成：
- Shared mode：用 `_collect_owned_datasets` → 完整客戶 datasets list →
  client-side `start:end` paginate → `total`、`has_more`、`page` 全部
  描述 **filter 後的視圖**
- Dedicated mode：行為不變（直接 forward Dify pagination）

成本：每次 list call O(workspace_size / 100) Dify roundtrips。對 shared
mode 預期使用情境（PoC / 小規模 demo）OK。Production 多客戶情境本來就
該用 dedicated。

Safety cap：10000 datasets workspace-wide，超過會 logger.warning。

#### 測試

`TestReviewFix_SharedListPagination`：
- `test_shared_list_walks_multiple_dify_pages` — Dify 第 1 頁全是
  tenant-b、第 2 頁有 tenant-a 的 30 個 → gateway page=1 limit=20 →
  正確顯示 tenant-a 的前 20 個 + `has_more=true` + `total=30`
- `test_shared_list_second_page_shows_remaining` — gateway page=2
  limit=20 → 拿到 tenant-a 的剩餘 5 個（25 - 20）

---

### Finding 4: [P2] Gate shared embedding behavior on mode

- **Severity**: [P2]
- **Codex 描述**:
  > `shared_embedding_model` is documented as ignored in dedicated mode,
  > but this branch treats any dedicated config that happens to include
  > it as shared-mode resolution. In that scenario normal dedicated
  > dataset creates can be rejected for using a registered embedding id.
- **影響檔案**: `gateway/src/gateway/routers/datasets.py:97-98`
- **動作**: ✅ Fixed

#### 驗證

我寫的：

```python
shared = customer.dify.shared_embedding_model
if shared is not None:
    # treat as shared mode
```

但 `shared_embedding_model` 是 optional field — dedicated 模式如果誤
設這個欄位，會被當成 shared 模式處理。當時 registry validator 沒擋
這個情境，所以 dedicated + shared_embedding_model 是合法配置但會
silent 走錯邏輯。

#### 修復內容

**Defence in depth**：

1. Registry validator 拒絕「dedicated + shared_embedding_model」配置：
   ```python
   if self.mode == "dedicated" and self.shared_embedding_model is not None:
       raise ValueError(
           "dify.shared_embedding_model must not be set when dify.mode='dedicated' "
           "(the field is only meaningful in shared mode; remove it or change mode)"
       )
   ```
   啟動就炸，operator 看到明確錯誤 message 馬上知道改哪。

2. Resolver 改用 `customer.dify.mode == "shared"` 判斷：
   ```python
   if customer.dify.mode == "shared":
       assert customer.dify.shared_embedding_model is not None  # 由 validator 保證
       shared = customer.dify.shared_embedding_model
       ...
   ```
   即使 validator 被 bypass（不會，但保險），resolver 還是看真正的 mode flag。

#### 測試

`TestReviewFix_DedicatedRejectsSharedEmbedding::test_dedicated_with_shared_embedding_rejected` —
直接斷言 dedicated + shared_embedding_model 在 Pydantic 驗證階段就被擋下。

---

## 整體決策

- Round 1 後狀態：**進 round 2 確認沒新 second-order bug**
- 全測試 **236 PASSED**（226 → 236：+10 review-1 regression tests）
- Ruff `check .` 全綠（順手把一個 RUF043 metacharacter 警告也修了）
- 修法**完全沒影響 dedicated mode**：所有 PR #1-#3 tests 仍綠

## Process 觀察

這輪的 4 個 finding 全是 **isolation 設計層級**：codex 透過 trace 程式
邏輯 + 從 attack model 角度想「如果我是 customer B 怎麼利用？」。我設
shared mode 時想了**正向流程**對不對，沒從**對抗思路**驗證每條路徑。

對 future PR 啟示：寫安全相關的程式碼時，**列舉 attack scenarios**：
- 我用什麼 invariant 區分 customer？該 invariant 真的成立嗎？
- 兩條看似不同的 error path 是否該收歛成同一個 envelope？
- Pagination / filter 順序錯了會洩漏 workspace state 嗎？

這比 review 之後修便宜很多。下個 PR 開工前 5 分鐘 checklist 加一條：
「列出 3 個 attack scenarios，verify the design 擋得住」。

## 預備 Round 2 的觀察點

預期 codex round 2 可能會看：

1. `_collect_owned_datasets` 的 safety cap（10000 datasets）有沒有 case 漏網
2. customer_id pattern 是否該也擋 reserved words / 特殊 prefix（`__`、`sys-`）
3. App naming 在 shared mode 沿用 PR #1-#3 的 `auto:{customer_id}:{model.id}`
   是否真的 collision-safe（我有寫 comment 說 yes 但沒驗證 — codex 已經
   抓過一次「comment 沒驗證」的問題了）
4. 文件 / 規模上限的記錄是否完整
