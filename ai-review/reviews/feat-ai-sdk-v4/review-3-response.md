# Review Response: feat-ai-sdk-v4 — Round 3

> Response to `reviews/feat-ai-sdk-v4/review-3.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 0 | — | — |
| [P2] | 3 | 3 | 0 |

Round 3 gate PASS。三個 P2 都是 round 2 修法**留下的後續邊角**：

- 修了 ownership check 順序（review-2 P2 #3）→ codex 指出 FastAPI 的 multipart binding 在 handler 跑之前就已經 parse body 了，**reorder 在 handler 內無效**
- 加了 cross-customer base_url 一致性檢查（review-1 P1 #1 周邊）→ codex 指出 trailing slash 會逃過 grouping
- 加了 prefixed dataset name 長度檢查（review-2 P2 #2）→ codex 指出 customer_id 太長時根本沒名字 budget，應該啟動就擋

全修 + 6 個 regression test（含修改既有 8 個 form-based test）。

## Findings 處理紀錄

---

### Finding 1: [P2] Don't rely on handler order to avoid multipart spooling

- **Severity**: [P2]
- **影響檔案**: `gateway/src/gateway/routers/files.py:79`
- **動作**: ✅ Fixed（大幅改寫 upload handler）

#### 驗證

我 review-2 把 ownership check 移到 `await file.read()` **之前**，
但 codex 指出更深的問題：FastAPI 為了把 `file: Annotated[UploadFile, File(...)]`
跟 `dataset_id: Annotated[str, Form(...)]` 綁進 handler 參數，
**在 handler 第一行執行之前**就會 trigger Starlette 的 multipart parser
spool 整個 body 到 disk/memory。我在 handler 內怎麼 reorder 都沒用。

要真正 cheap-fail，dataset_id 必須**完全在 multipart body 之外**（query 或
header），這樣 ownership check 可以在 `await request.form()` 被呼叫**之前**
完成。

#### 修復內容

**Breaking change** — `/v1/files` upload 介面改成：

```javascript
// Before
POST /v1/files
Content-Type: multipart/form-data
body: file=<binary>, dataset_id=<uuid>, indexing_technique=<...>

// After
POST /v1/files?dataset_id=<uuid>&indexing_technique=<...>
Content-Type: multipart/form-data
body: file=<binary>          ← only the binary
```

Handler 重寫成 explicit 流程：

```python
@router.post("/v1/files")
async def upload_file(request: Request) -> Any:
    # 1. Get dataset_id from query — body NOT touched
    dataset_id = request.query_params.get("dataset_id")
    if not dataset_id:
        raise InvalidRequestError(...)
    
    # 2. Ownership check (in shared mode does one Dify GET) — body still NOT touched
    await _verify_dataset_ownership_for_files(...)
    
    # 3. Validate indexing_technique from query
    indexing_technique = request.query_params.get("indexing_technique") or "high_quality"
    if indexing_technique not in _VALID_INDEXING_TECHNIQUES:
        raise InvalidRequestError(...)
    
    # 4. NOW parse the body
    form = await request.form()
    file = form.get("file")
    ...
```

額外的小坑：`request.form()` 回的是 `starlette.datastructures.UploadFile`，
不是 `fastapi.UploadFile`。我一開始用 `isinstance(file, fastapi.UploadFile)`
失敗（雖然 FastAPI 「歷史上」re-export Starlette 的 UploadFile，但新版可能
strict-subclass 過）。改成 import Starlette 的版本做 isinstance check，
更 robust。

**Migration**：所有 PR #3 existing tests（8 個 upload tests）的 `data={"dataset_id":...}`
都改成 URL query。不向下相容 form-based dataset_id — 但 PR #4 還沒 ship，所以
沒有 prod migration 成本。

#### 測試

- 更新 8 個既有 test_files.py + 4 個 test_shared_mode.py 的 upload tests
  把 dataset_id 從 form 移到 query
- **新** `TestReview3Fix_UploadDatasetIdInQuery::test_upload_with_form_only_dataset_id_is_rejected`
  — 確認舊的 form-based 介面確實被擋下，message.param = "dataset_id"
- **新** `test_upload_query_indexing_technique_forwarded` — `indexing_technique=economy`
  從 query 帶進來確實有 forward 到 Dify

---

### Finding 2: [P2] Normalize base URLs before consistency grouping

- **Severity**: [P2]
- **影響檔案**: `gateway/src/gateway/registry.py:325`
- **動作**: ✅ Fixed

#### 驗證

`_check_dify_consistency` 用 `e.dify.base_url` 當 dict key，但
`http://dify` 跟 `http://dify/` 是不同 string。實際呼叫時 `DifyClient.__init__`
自己有 `rstrip("/")`，所以兩個指向同一個 Dify upstream — registry validator
卻當成兩個不同的 deployment，**mixed-mode config 就溜過**。

攻擊腳本：
```yaml
customers:
  - sdk_key: bsa_a
    customer_id: a
    dify: {base_url: "http://dify-shared.test", mode: shared, ...}
  - sdk_key: bsa_b
    customer_id: b
    dify: {base_url: "http://dify-shared.test/", mode: dedicated}  # trailing slash!
```

之前：registry 載入成功（被認為不同 base_url）→ runtime 兩個 customer 走不同
mode → A 的 datasets 有 prefix、B 的沒有 → 在同一個 Dify workspace 撞名 / 看到彼此。

#### 修復內容

`_check_dify_consistency`：

```python
groups[e.dify.base_url.rstrip("/")].append(e)
```

跟 `DifyClient` 內部 normalize 邏輯一致。

#### 測試

`TestReview3Fix_BaseUrlNormalization::test_trailing_slash_grouped_same` — A 用
`http://dify-shared.test` shared mode、B 用 `http://dify-shared.test/`
dedicated mode → registry load 必須 raise `disagree on isolation mode`。

---

### Finding 3: [P2] Reject shared customer IDs that leave no dataset-name budget

- **Severity**: [P2]
- **影響檔案**: `gateway/src/gateway/registry.py:212`
- **動作**: ✅ Fixed

#### 驗證

Shared mode dataset name 是 `{customer_id}__{name}`。Dify 上限 40 字元。
我的 customer_id 上限 64 — 如果 customer_id = 38 字元，加 `__` (2) = 40 字元已
等於 Dify 上限，name 沒有任何 budget。**registry 載入成功，每個 dataset create
失敗** — operator 要打開第一個 POST 才會發現。

#### 修復內容

`CustomerEntry` 加 model_validator (mode="after")：

```python
_DIFY_DATASET_NAME_LIMIT: int = 40  # mirrors routers/datasets.py

@model_validator(mode="after")
def _shared_mode_customer_id_fits_name_budget(self) -> CustomerEntry:
    if self.dify.mode == "shared":
        prefix_overhead = len(self.customer_id) + 2  # "__"
        if prefix_overhead >= self._DIFY_DATASET_NAME_LIMIT:
            budget = self._DIFY_DATASET_NAME_LIMIT - 2 - 1
            raise ValueError(
                f"customer_id '{self.customer_id}' ({len(self.customer_id)} chars) "
                f"is too long for shared mode: prefix '{self.customer_id}__' would use "
                f"{prefix_overhead}/{self._DIFY_DATASET_NAME_LIMIT} of Dify's "
                f"dataset-name budget, leaving no room for the name. "
                f"Use a customer_id of at most {budget} chars for shared mode."
            )
    return self
```

訊息明確 actionable：操作者看到 customer_id 太長，知道改成多短可以接受。

Dedicated mode**不受影響** — customer_id 在 dedicated mode 不被拿來當前綴，
所以長度沒約束（保留 64 char 上限只是合理性 cap）。

#### 測試

`TestReview3Fix_SharedCustomerIdLengthBudget`：
- `test_overflowing_customer_id_in_shared_mode_rejected` — 38 char customer_id + shared mode → 啟動 raise
- `test_short_customer_id_in_shared_mode_accepted` — 正常長度仍可載
- `test_dedicated_mode_ignores_length_budget` — **regression**：50 char customer_id
  在 dedicated mode 仍 OK（這條 invariant 只對 shared mode 適用）

---

## 整體決策

- Round 3 後狀態：**進 round 4 確認沒新 second-order bug**
- 全測試 **246 PASSED**（240 → 246：+6 review-3 regression tests）
- Ruff `check .` 全綠（順手修了一個 W605 invalid-escape）
- 修法**完全沒影響 dedicated mode**
- **Breaking change**：`/v1/files` upload 介面 dataset_id 從 form 改成 query —
  但 PR #4 還沒 ship，影響範圍 = 內部 tests + future client 文檔

## 跨 PR 累計 4 個 process pattern

1. **Second-order bug**（修 A 忘了 B/C）
2. **Design contract vs 實作**（comment 寫的沒 enforce、strategy 寫了沒用）
3. **攻擊面 = 順序**（valid input 配 foreign target → 前面步驟的 cost 被白燒）
4. **「修了一半」**（這輪：review-2 修了 ownership 順序但 FastAPI 內部 binding 比我以為的早；review-1 修了 customer_id pattern 但忘了驗證合併後長度）

對應 future PR 啟示：每次修完 codex 找的問題，**再問一次「這個修法在這個假設成立**才正確 — 那個假設真的成立嗎？」例如：
- review-2 假設「handler 內 reorder 能控制 I/O 順序」→ 假設錯，FastAPI binding 更早 → review-3 抓到
- review-1 修了 customer_id pattern，但「pattern 不允許 underscore」這個假設加上「customer_id 還是可以 64 字元」交互後，name budget 出問題 → review-3 抓到

下個 PR 開工前 checklist 加一條：**列出我修法依賴的 invariants，逐一驗證**。

## 預備 Round 4 的觀察點

預期 codex round 4 可能會看：

1. `_DIFY_DATASET_NAME_LIMIT` 在 routers/datasets.py 跟 registry.py 都
   hardcode 為 40 — 重複常數
2. customer_id slug 用 byte 計算 (`len()`) 但中文 customer_id 不會走進來
   （pattern 限制只接受 a-z0-9- ASCII）— OK
3. `request.form()` 一旦呼叫，整個 body 被 buffer — 對 multi-GB 上傳仍是
   bottleneck（不是 review-3 範圍，但 follow-up）
4. delete_file / list_files 也是 query-param dataset_id — 跟 upload 一致 ✓
