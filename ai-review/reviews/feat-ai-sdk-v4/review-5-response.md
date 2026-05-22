# Review Response: feat-ai-sdk-v4 — Round 5

> Response to `reviews/feat-ai-sdk-v4/review-5.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 0 | — | — |
| [P2] | 1 | 1 | 0 |

Single P2，gate PASS。**Contract regression**：PR #3 R2 / R3 已經寫明
DELETE 是 idempotent — Dify 端 404 視為已刪。但我 PR #4 加的 shared-mode
ownership pre-flight 把 Dify 404 改寫成 `UnknownDatasetError(404)`，
然後就直接 raise — cleanup loop 在 stale UUID 上拿到 404，違反 contract。
Codex 透過 trace cleanup-loop 視角發現的。修法：把 missing 跟 foreign
分開處理。

## Findings 處理紀錄

---

### Finding 1: [P2] Preserve idempotent shared dataset deletes

- **Severity**: [P2]
- **影響檔案**: `gateway/src/gateway/routers/datasets.py:475-480` +
  `routers/files.py:203` (codex 沒明寫但同個 pattern)
- **動作**: ✅ Fixed

#### 驗證

PR #3 R2/R3 contract（docstring 寫的）：

> Idempotent: returns 200 whether or not the dataset existed (Dify 404 →
> treated as already-deleted in the client).

PR #4 shared mode 的 ownership pre-flight：
```python
async def _verify_dataset_ownership(...):
    try:
        meta = await dify_client.get_dataset(...)
    except UpstreamClientError as exc:
        if strategy.is_shared and exc.status_code == 404:
            raise UnknownDatasetError(...) from exc  # ← 把 404 改寫
        raise
    if not strategy.dataset_belongs_to(...):
        raise UnknownDatasetError(...)
    return meta
```

所以：
- Stale UUID（不存在）→ get_dataset 404 → 改寫成 `UnknownDatasetError(404)` → 客戶看到 404 **(contract 違反)**
- Foreign UUID（屬於別人）→ get_dataset 200 + name mismatch → `UnknownDatasetError(404)` → 客戶看到 404 (預期，no leak)

修前後對比：

| 情境 | Dedicated | Shared (PR #4 修前) | Shared (修後) |
|---|---|---|---|
| Missing UUID delete | 200 ✓ idempotent | **404 ✗ contract 違反** | 200 ✓ idempotent |
| Foreign UUID delete | N/A (no foreign in dedicated) | 404 ✓ reject | 404 ✓ reject |
| Own UUID delete | 200 ✓ | 200 ✓ | 200 ✓ |

#### 修復內容

`delete_dataset` 跟 `delete_file` 兩個 handler 都**不再用** `_verify_dataset_ownership` helper（它會把兩個情境塞同一個 error）。改成 inline distinguish：

```python
if strategy.is_shared:
    try:
        meta = await dify_client.get_dataset(...)
    except UpstreamClientError as exc:
        if exc.status_code == 404:
            # 已經不見 — honour idempotent contract
            logger.info("datasets.deleted", ..., status="already-missing")
            return JSONResponse({"id": dataset_id, "deleted": True})
        raise  # 其他上游錯誤 (502 / 504 / ...) 仍然 propagate
    # Got meta — check ownership
    if not strategy.dataset_belongs_to(customer.customer_id, meta.get("name", "")):
        raise UnknownDatasetError(...)
# fall through to actual delete
```

兩個檔的 docstring 都更新 explicit 寫出 missing-vs-foreign 的不同處理 +
記註 codex review-5 P2。

##### Existence leak trade-off

修後 missing 200 / foreign 404 確實能透過 DELETE 端點區分 — 但：
- GET / RETRIEVE / UPLOAD 都仍是 404 for both missing+foreign（沒 leak）
- DELETE 是 mutation，蝴 customer 不會用來 probe（除非已經有 attack）
- 替代方案「foreign 也 200」會讓蝴 customer 以為自己刪了別人的資料（語義污染）

Codex 自己也 explicit 推薦這個 trade-off。Accept it。

#### 測試

`TestReview5Fix_IdempotentSharedDelete`：

- `test_shared_delete_missing_dataset_returns_idempotent_success` —
  stale UUID + shared mode → 200，且關鍵：`dataset_delete` Dify call **沒被觸發**
- `test_shared_delete_foreign_dataset_still_returns_404` — regression：
  foreign UUID 仍 404，確認 fix 沒拉錯走向
- `test_shared_delete_file_missing_dataset_returns_idempotent_success` —
  同 pattern apply 在 files endpoint

---

## 整體決策

- Round 5 後狀態：**進 round 6 確認真的收斂**（PR #1-#3 三輪結束，PR #4 已經 5 輪，看是否真的飽和）
- 全測試 **251 PASSED**（248 → 251：+3 idempotent regression tests）
- Ruff `check .` 全綠
- 修法**完全沒影響 dedicated mode**

## 累計 process 觀察：5 個 recurring pattern

PR #4 5 輪 review 把所有 5 個 pattern 都重現了一次，作為紀錄：

1. **Second-order bug** — PR #1 r2, PR #2 r2-3, PR #3 r1-2 (修了 A 忘了 B/C)
2. **Design contract vs 實作不一致** — PR #4 r1 (slug invariant 沒 enforce), r2 (app_name 寫了沒用)
3. **攻擊面 = 順序** — PR #4 r2 (ownership 在 file.read 後)
4. **「修了一半」** — PR #4 r3 (handler 順序 vs FastAPI binding)
5. **保護範圍寫太寬** — PR #4 r4 (slug pattern 套到所有 customer 不只 shared)
6. **NEW: Contract regression** — PR #4 r5 (修 isolation 時破壞 idempotent contract)

第 6 個 pattern 是 codex review 之外加的：**當你加 wrapping 邏輯時，
仔細列出**被 wrap 的 endpoint contract / docstring，逐條 verify 包裝後仍
滿足**。我的 ownership pre-flight 是 wrapping pattern，沒檢查 wrap 後是否
仍滿足 idempotent — 直到 codex 提醒。

對應 future PR checklist 第 6 條：**加任何「在原邏輯前 / 後跑」的程式
碼時，把原 endpoint 的 docstring contract 唸一遍**。

## 預備 Round 6 的觀察點

PR #1/2/3 都 3 輪結束，PR #4 已經 5 輪。round 6 預期應該真的收斂（或頂多
非常 minor 的 P2 nitpick）。可能會看：

1. `delete_dataset` 跟 `delete_file` 的 inline ownership 程式碼有點重複 —
   抽 helper 還是內聯？
2. Idempotent 200 訊息 `{"deleted": True}` for missing UUID 是 plain dict
   沒 schema 驗證 — minor consistency 跟 OpenAPI doc 不對 (但 PR #3 也這樣 dump 出來)
3. `_verify_dataset_ownership` helper 仍然存在（被 get/retrieve 用）— 是否該
   重構成統一的 ownership-resolver 一次處理 4 種行為（get / retrieve / upload / delete）？
