# Review Response: feat-ai-sdk-v4 — Round 7

> Response to `reviews/feat-ai-sdk-v4/review-7.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 0 | — | — |
| [P2] | 1 | 1 | 0 |

Single P2，gate PASS。這輪是 review-6 P2 #3 修法的**範圍寫窄**：我擋了
「同 base_url + 同 customer_id」，但 AppManager 的 cache key 是
`(customer_id, model_id)` — 跟 base_url 完全無關。所以**任何兩個** customer
撞 customer_id 都會 cache collision，不限同 base_url。

正確 invariant：`customer_id` 全域唯一。

## Finding 處理紀錄

---

### Finding: [P2] Use globally unique customer IDs or app-cache keys

- **Severity**: [P2]
- **影響檔案**: `gateway/src/gateway/registry.py:427-430`
- **動作**: ✅ Fixed

#### 驗證

我 review-6 的修法：

```python
if next(iter(modes)) == "shared":
    customer_ids = [m.customer_id for m in members]  # within base_url group
    duplicates = ...
```

只擋同 base_url group 內的 duplicate。但 codex 提醒我整個 gateway 的 cache 結構：

```python
# gateway/dify/app_manager.py
self._apps: dict[tuple[str, str], CachedApp]  # key = (customer_id, model_id)
self._sessions: dict[str, ConsoleSession]    # key = customer_id

# gateway/registry.py
def find_by_customer_id(self, customer_id: str) -> CustomerEntry | None:
    # GC sweep 用這個 resolve
```

→ **沒有任何 cache 把 base_url 加進 key**。所以：
- customer A `customer_id="tenant-a"` on dify-1 → 第一次請求 build App，cache key `("tenant-a", "model-x")`
- customer B `customer_id="tenant-a"` on dify-2 → 同 cache key → **拿到 deployment 1 的 app_key**
- B 的請求被打到 deployment 1 的 Dify → 完全錯誤的 routing

更糟的是 GC：`find_by_customer_id("tenant-a")` 回傳「第一個」找到的 entry — 可能不是這個 cache entry 真正所屬的 customer。GC 把錯誤 deployment 的 App 給刪了。

Codex 給的兩個 fix option：
- A. **全域** customer_id 唯一
- B. Cache / session key 加入 base_url 或 sdk_key

選 **A**（簡單、sane invariant、不需要 cache key migration）。Customer_id 全域
唯一本來就是合理的 system invariant — 兩個 customer 同名是一個 product
模糊問題，不該透過 deployment 區分。

#### 修復內容

`CustomerRegistry.from_entries`：

```python
@classmethod
def from_entries(cls, entries: list[CustomerEntry]) -> CustomerRegistry:
    by_key: dict[str, CustomerEntry] = {}
    seen_customer_ids: set[str] = set()
    for entry in entries:
        if entry.sdk_key in by_key:
            raise ValueError(f"duplicate sdk_key in registry: {entry.sdk_key}")
        # Codex review-7 P2: customer_id MUST be globally unique
        if entry.customer_id in seen_customer_ids:
            raise ValueError(
                f"duplicate customer_id in registry: '{entry.customer_id}'. "
                f"customer_id must be globally unique because gateway caches "
                f"(AppManager apps, console sessions, GC lookup) are keyed by "
                f"customer_id alone; duplicates would target the wrong Dify "
                f"deployment."
            )
        seen_customer_ids.add(entry.customer_id)
        by_key[entry.sdk_key] = entry
    cls._check_dify_consistency(by_key.values())
    return cls(by_key)
```

Remove 重複的 within-group check from `_check_dify_consistency`（global 已經
涵蓋更嚴格的條件）— 留 comment 解釋為什麼那段被拿掉。

#### 測試

`TestReview7Fix_GlobalCustomerIdUniqueness`（取代 review-6 的 `TestReview6Fix_DuplicateCustomerIdInSharedGroup`）：

- `test_globally_duplicate_customer_id_rejected` — 任何兩個同 customer_id → reject
- `test_duplicate_customer_id_different_base_url_rejected` —
  **review-6 之前 expected pass，現在 expected reject**（codex 抓的 case）
- `test_dedicated_duplicate_customer_id_also_rejected` —
  **review-6 之前 expected pass for dedicated, 現在 also reject**（同 cache 結構）
- `test_distinct_customer_ids_accepted` — sanity

刪掉 review-6 加的兩個過度寬鬆 test：
- `test_shared_same_customer_id_different_base_url_allowed`（codex 點到的 bug case）
- `test_dedicated_duplicate_customer_id_same_base_url_allowed`（同 cache 結構問題）

---

## 整體決策

- Round 7 後狀態：**進 round 8 確認真的收斂**（PR #4 第七輪了，PR #1/2/3 都 3 輪結束，看 codex 是否還能找）
- 全測試 **259 PASSED**（258 → 259 net：+4 review-7 tests，-3 review-6 over-permissive tests）
- Ruff clean

## Round 6 vs round 7 對照

Review-6 的我加 `_check_dify_consistency` 內的 within-group duplicate check
是「正確方向但範圍太窄」的典型案例。Codex 連續兩輪剝洋蔥：

- Round 6：「同 base_url + 同 customer_id 會撞 prefix」→ 我加了 within-group check
- Round 7：「跨 base_url + 同 customer_id 會撞 cache」→ 我擴大為 global check

每輪都正確但越來越廣。對應 future PR 啟示：**新加 invariant 時，從最寬廣
的 scope 開始想（global / cross-deployment / cross-mode）再縮窄；不要從窄
開始往外加**。

## 預備 Round 8 的觀察點

PR #4 第七輪了，預期 round 8 應該真的收斂。如果還有 finding，可能會看：

1. `_check_dify_consistency` 內 dead comment 寫得清不清楚（codex 過去抓
   過 comment vs 實作不一致的 pattern）
2. Error message 是否在所有 raise path 上一致（duplicate sdk_key vs
   duplicate customer_id format）
3. AppManager cache 是否在某些路徑下仍有 customer_id collision risk
   （eg session 是 customer_id 為 key，cache 改 global 之後其他 cache 也該 audit）
4. registry 的測試 covered 順序變動（同 entries 兩種順序載入結果都該一樣）
