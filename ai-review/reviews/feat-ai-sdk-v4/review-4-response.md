# Review Response: feat-ai-sdk-v4 — Round 4

> Response to `reviews/feat-ai-sdk-v4/review-4.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 0 | — | — |
| [P2] | 1 | 1 | 0 |

Single P2，gate PASS。**經典的 backward-compat 漏網**：review-1 P1 我為了
擋 shared-mode prefix attack 加了 customer_id slug pattern，但用 Pydantic
Field 套到所有 entry — 包含 dedicated mode 既有部署。Codex 透過 attack
model 想完 shared mode 後反問「這個保護對 dedicated mode 有意義嗎？」答案：
沒意義，反而破壞 backward compat。

## Findings 處理紀錄

---

### Finding 1: [P2] Keep dedicated customer IDs backward compatible

- **Severity**: [P2]
- **影響檔案**: `gateway/src/gateway/registry.py:210-213`
- **動作**: ✅ Fixed

#### 驗證

我 review-1 加的：

```python
customer_id: str = Field(
    min_length=1,
    max_length=64,
    pattern=r"^[a-z0-9][a-z0-9-]*$",  # ← 套到所有 customer
)
```

Pattern 在 Field level 跑 — 任何 customer entry 都被驗證，不管 mode。
PR #4 是 opt-in feature；dedicated mode 是 PR #1-#3 已經 ship 的預設。
所以這個 pattern 把「PR #4 shared mode 的安全需求」**強加給所有 dedicated
deployments**。

具體 break：
- 既有客戶 `customer_id: "Customer_A"`（大寫 + underscore）→ PR #4 升級後 startup raise
- 既有客戶 `customer_id: "acme_prod"` → 同上
- 那些 ID 在 dedicated mode 完全安全（每個 customer 有自己的 Dify，沒有共用 prefix）

#### 修復內容

把 pattern 從 Field 移除，加進一個 mode-aware 的 model_validator：

```python
# Field: 只保留 length + description
customer_id: str = Field(
    min_length=1,
    max_length=64,
    description=("... see _shared_mode_customer_id_is_slug ..."),
)

# Module-level compiled pattern (faster than recompiling)
_SHARED_CUSTOMER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

@model_validator(mode="after")
def _shared_mode_customer_id_is_slug(self) -> CustomerEntry:
    """只在 shared mode 強制 slug pattern。Dedicated mode 接受任何 string。"""
    if self.dify.mode == "shared" and not self._SHARED_CUSTOMER_ID_PATTERN.match(
        self.customer_id
    ):
        raise ValueError(
            f"customer_id '{self.customer_id}' is not a valid shared-mode slug. "
            "Shared mode requires lowercase ASCII letters, digits, and hyphens "
            "only (must start with a letter or digit). Underscores are reserved "
            "as the shared-mode prefix separator. Dedicated mode has no such "
            "restriction; switch dify.mode if you want flexibility."
        )
    return self
```

訊息明確：「規則只在 shared mode 適用、要更彈性就用 dedicated」。

#### 測試

`TestReviewFix_CustomerIdSlug` 改造：

- **改寫**：原 4 個 shared-mode rejection tests 加上 `mode="shared"` +
  `shared_embedding_model=...` 的 helper，確保只 fire 在 shared mode 下
- **新** `test_dedicated_customer_id_with_underscore_accepted` — `acme_prod`
  在 dedicated mode 接受（backward compat）
- **新** `test_dedicated_customer_id_uppercase_accepted` — `Customer_A`
  同樣接受

兩個新 test 是 regression test：未來 PR 不小心又把 pattern 套到 Field level
時，這兩條會立刻 fail。

---

## 整體決策

- Round 4 後狀態：**進 round 5 確認收斂**
- 全測試 **248 PASSED**（246 → 248：+2 dedicated-compat regression，
  原 4 個 shared-mode test 改造）
- Ruff `check .` 全綠
- **Backward compat 完全恢復**：PR #1-#3 既有 dedicated 客戶不會因 PR #4
  升級就 startup fail

## Process 觀察

這輪 finding 的 root cause 是 review-1 修法的「**保護範圍過廣**」：
- review-1 抓到 shared-mode prefix attack
- 我修法時用了「整個欄位都驗」的最強烈手段
- review-4 抓到「dedicated mode 沒有 attack surface，不該被約束」

對應到累計 5 個 process patterns 的第 5 條：

5. **「保護範圍寫太寬」** — 用 Field-level 驗證解決了 mode-specific 問題，
   被當成全局約束套到所有 customer

future PR checklist 第 5 條：**修法時問「這個約束的 enforcement scope 是什麼？
是該對所有 entry 還是 conditional」**。

## 預備 Round 5 的觀察點

預期 round 5 應該找不到新東西（收斂），但可能會看：

1. `_SHARED_CUSTOMER_ID_PATTERN` compiled module-level — 多 process worker
   時記憶體共用 OK
2. validator 順序：`_shared_mode_customer_id_is_slug` 在
   `_shared_mode_customer_id_fits_name_budget` 之前。如果 customer_id 同時
   有大寫又超長，會先報 slug error。順序合理（先報基本格式）
3. 新 error message 是否太長 / 該濃縮
