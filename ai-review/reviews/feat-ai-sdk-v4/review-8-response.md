# Review Response: feat-ai-sdk-v4 — Round 8

> Response to `reviews/feat-ai-sdk-v4/review-8.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 0 | — | — |
| [P2] | 2 | 2 | 0 |

兩個 P2 都修：search keyword 在 shared mode 應用 public name 上、Field-level
max_length=64 套到 dedicated mode 也是 backward-compat 破壞。

## Findings 處理紀錄

---

### Finding 1: [P2] Filter shared dataset keywords on public names

- **Severity**: [P2]
- **影響檔案**: `gateway/src/gateway/routers/datasets.py:395`
- **動作**: ✅ Fixed

#### 驗證

我的 `_collect_owned_datasets` 把 `keyword` 直接轉發 Dify：

```python
resp = await dify_client.list_datasets(
    ...
    keyword=keyword,  # ← 客戶 facing name 的 keyword，但 Dify 比 prefixed name
)
```

問題：
- 客戶 `tenant-a` 搜 `keyword=tenant-a` → Dify substring-match 找到所有 `tenant-a__...` → 全部命中（包括公開名沒 `tenant-a` 的 dataset）
- 客戶 `tenant-a` 搜 `keyword=alarm` → Dify substring-match 找 `tenant-a__alarms` 等 → 正確
- 但客戶以為他搜的是「public name」，gateway 卻在 prefixed name 上 match

實作 leak 出 prefix 細節。

#### 修復內容

`list_datasets` shared mode 分支：

```python
if strategy.is_shared:
    # NEVER forward keyword to Dify in shared mode — strip prefix first
    owned = await _collect_owned_datasets(
        dify_client, customer, strategy, keyword=None  # ← always None
    )
    if keyword:
        # Filter on the customer-facing name
        keyword_lower = keyword.lower()
        owned = [
            d for d in owned
            if keyword_lower in (
                strategy.dataset_name_from_dify(customer.customer_id, d.get("name", ""))
                or ""
            ).lower()
        ]
    # paginate client-side
    total = len(owned)
    ...
```

Trade-off：shared mode 的 list 完全在 gateway 端做 search（O(workspace size)
per request）。對 shared mode 預期使用情境（PoC / 小規模）可接受。

#### 測試

`TestReview8Fix_SharedKeywordOnPublicName`：
- `test_keyword_matches_only_public_name` — 三個 dataset 兩個含 "rsrp" →
  搜 `keyword=rsrp` → 只回 2 個 + names 是 stripped 後的
- `test_keyword_matching_customer_id_does_not_match_all` —
  **正是 codex 描述的 attack case**：搜 `keyword=tenant-a` →
  public name 都沒 "tenant-a" → 回空陣列（之前會回全部 2 個）
- `test_shared_list_does_not_forward_keyword_to_dify` —
  **implementation property**：斷言送給 Dify 的 keyword 是 None

---

### Finding 2: [P2] Keep dedicated customer IDs backward-compatible

- **Severity**: [P2]
- **影響檔案**: `gateway/src/gateway/registry.py:207`
- **動作**: ✅ Fixed

#### 驗證

我 review-1 修 customer_id slug pattern 時加了 `max_length=64`。Review-4 codex
把 slug pattern 移到 shared-mode validator，**但我留下 max_length=64 在 Field
level**。

Round 8 codex 抓出：max_length=64 ALSO 套到 dedicated mode。PR #1-#3 deployment
若 customer_id 超過 64 字元（沒人 explicitly 限制過）→ PR #4 升級即 startup fail。

這是 review-4 的同 pattern 再犯一次：「保護範圍寫太寬」。修法時兩個 Field-level
約束我只移走了 slug pattern，length cap 沒注意。

#### 修復內容

```python
# Before
customer_id: str = Field(
    min_length=1,
    max_length=64,
    description="...",
)

# After
customer_id: str = Field(
    min_length=1,
    description="...",
)
```

完全移除 `max_length`。Shared mode 已經有 `_shared_mode_customer_id_fits_name_budget`
validator 隱性把 customer_id 限制在 ~37 字元（Dify dataset name 40 字元 - prefix `__` 2 - name min 1 = 37）。Dedicated mode 不需要任何 length cap。

#### 測試

`TestReview8Fix_DedicatedCustomerIdNoLengthCap::test_long_dedicated_customer_id_accepted` —
100 字元 dedicated customer_id 載入成功 → backward compat 守門。

---

## 整體決策

- Round 8 後狀態：**進 round 9 確認真的收斂**
- 全測試 **263 PASSED**（259 → 263：+4 review-8 tests）
- Ruff `check .` 全綠

## Round 8 觀察

這輪兩個 finding 都是「**設計新功能時忘了把保護範圍 narrow 到對的 scope**」。
跨 8 輪共 7 個類似 pattern：

| Round | Pattern |
|---|---|
| 1 | Slug pattern 套到所有 customer (review-1) |
| 4 | 同上 (review-4 抓出 backward compat) |
| 8 | max_length 套到所有 customer (這輪) |
| 8 | keyword 套到 Dify 端搜 (這輪) |

對應到 process 啟示：**每加一個新 invariant / 行為，分別問**：
1. 「dedicated mode 跟 shared mode 都要這個嗎？」
2. 「Dify-side 跟 gateway-side 都要這個嗎？」
3. 「同 base_url 跟 cross-base_url 都要這個嗎？」

預期 round 9 應該真的收斂（連續 PR #4 第八輪了，每輪 1-2 個 P2，找到的東西越來越窄）。
