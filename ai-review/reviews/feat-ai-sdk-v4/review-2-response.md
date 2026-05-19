# Review Response: feat-ai-sdk-v4 — Round 2

> Response to `reviews/feat-ai-sdk-v4/review-2.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 0 | — | — |
| [P2] | 3 | 3 | 0 |

Round 2 全綠（gate PASS）。三個 P2 都是**「設計交付完整性」級別**的問題：

- 我寫了 `IsolationStrategy.app_name` 但沒在 AppManager 用 — codex 抓「contract vs 實作不一致」
- 我在 router prefix dataset name 沒驗合併長度 — codex 抓「邊界值忘了 propagate」
- 我把 ownership check 放在 `file.read()` 之後 — codex 抓「攻擊面 = 順序錯」

全修 + 4 個 regression test。

## Findings 處理紀錄

---

### Finding 1: [P2] Wire app naming strategy into AppManager

- **Severity**: [P2]
- **影響檔案**: `gateway/src/gateway/dify/app_manager.py:179`
- **動作**: ✅ Fixed

#### 驗證

我在 round 0 的 commit message 寫「R2 app_manager: skip (現有 naming 已 collision-safe in both modes)」— 邏輯是對的（colon-separated naming 在 dedicated 跟 shared 都不會撞），但 codex 點出：**strategy 定義了 `app_name` method 但沒人 call**，這是 dead code 跟 misleading contract。未來開發者讀 mode.py 會以為 strategy 控制了 app 命名，但實際上 hardcoded 在 `_build_app`。

修正動機不是邏輯 bug，是**「一致性的 design contract」**。

#### 修復內容

1. `mode.py`：把 `_APP_PREFIX_SEP` 從 `-` 改成 `:`，跟既有的 `auto:{customer_id}:{model_id}` 對齊。兩個 strategy 都返回 `{customer_id}:{model_id}` — 不會 orphan 既有 Dify Apps。

2. `dify/app_manager.py:_build_app`：

```python
from gateway.mode import isolation_strategy_for
strategy = isolation_strategy_for(customer)
app_label = strategy.app_name(customer.customer_id, model.id)
dsl = build_chat_app_dsl(name=f"auto:{app_label}", ...)
```

- Dedicated mode 用 `f"{customer_id}:{model_id}"`：跟 PR #1-#3 完全一樣（preserve legacy naming，避免 orphan）
- Shared mode 也是 `f"{customer_id}:{model_id}"`：colon-separated 同樣 collision-safe

未來如果要加新 mode（regional、env-tagged）覆寫 strategy.app_name 就能客製化。

#### 測試

- 更新 `TestIsolationStrategy::test_dedicated_passthrough`：`app_name("tenant-a", "model-x") == "tenant-a:model-x"`（之前 `"model-x"` no prefix）
- 更新 `TestIsolationStrategy::test_shared_prefixes_and_strips`：用 colon 而非 hyphen
- **新** `TestReview2Fix_AppManagerWiresStrategy::test_dedicated_app_name_uses_strategy`：實際打 chat completion 觸發 App build，斷言 DSL YAML 內含 `auto:test-a:m1` (走 strategy 路徑) — 不是 hardcoded

---

### Finding 2: [P2] Validate the prefixed dataset name length

- **Severity**: [P2]
- **影響檔案**: `gateway/src/gateway/routers/datasets.py:245`
- **動作**: ✅ Fixed

#### 驗證

`DatasetCreateRequest.name`：`max_length=40`（跟 Dify 的 dataset name 上限一致）。
`customer_id`：round-1 加了 `max_length=64`。
Prefix 邏輯：`{customer_id}__{name}` → 最壞 `64 + 2 + 40 = 106` 字元，遠超 Dify 40。

客戶送 `name="kb"` (2 char) + customer_id `tenant-abcdefghij` (16 char) → prefixed = `tenant-abcdefghij__kb` (21 char) OK。
客戶送 `name="my-knowledge-base-for-product-x"` (29 char) + 同 customer → prefixed = 49 char → **超過**。

Codex 指出：這種情況 gateway 接受，Dify 拒絕，客戶看到 Dify 的 4xx — 而 Dify 的錯誤訊息不會解釋「是因為加了 customer_id prefix 所以爆」。

#### 修復內容

`routers/datasets.py`：加 `_DIFY_DATASET_NAME_MAX = 40` 常數 + 在 `create_dataset` 做 length 驗證：

```python
dify_name = strategy.dataset_name_to_dify(customer.customer_id, body.name)
if len(dify_name) > _DIFY_DATASET_NAME_MAX:
    budget = _DIFY_DATASET_NAME_MAX - (len(dify_name) - len(body.name))
    raise InvalidRequestError(
        f"dataset name '{body.name}' exceeds Dify's {_DIFY_DATASET_NAME_MAX}-char "
        f"limit once prefixed for shared mode "
        f"(customer_id='{customer.customer_id}' uses {len(prefix)} chars of the budget; "
        f"max remaining for the name is {max(budget, 0)})",
        param="name",
    )
```

訊息明確：
- 客戶看到自己 name 超長
- 看到 customer_id 用掉多少
- 看到還剩多少預算

比 Dify 的「Internal Server Error」或「name too long」訊息友善太多。

#### 測試

- **新** `TestReview2Fix_SharedDatasetNameLength::test_long_prefixed_name_rejected_at_gateway`：customer_id 16 char + name 26 char → prefix 44 > 40 → 400，error.param="name"，message 含「40-char limit」，**critical**：沒打 Dify
- **新** `test_short_prefixed_name_accepted`：合理長度仍可建（regression）

---

### Finding 3: [P2] Check dataset ownership before reading uploads

- **Severity**: [P2]
- **影響檔案**: `gateway/src/gateway/routers/files.py:80-94`
- **動作**: ✅ Fixed

#### 驗證

Codex 指出 attack：

1. 攻擊者用 customer A 的 SDK key 登入
2. 把 dataset_id 設成 customer B 的 UUID（猜的或洩漏）
3. Upload 一個**大**檔案（例如 100MB）
4. 我的 router：
   - `await file.read()` 把 100MB 讀進記憶體 ← **wasted I/O**
   - 然後才 `_verify_dataset_ownership_for_files` → 404

攻擊者可以反覆送大檔，把 gateway 的記憶體 / disk spool 灌爆，即使最終會收 404。**SoC：dataset-not-found 應該在最少 cost 的點偵測**。

#### 修復內容

`routers/files.py::upload_file`：把 ownership 驗證移到 `file.read()` **之前**：

```python
# Before
content = await file.read()
if not content:
    raise InvalidRequestError(...)
await _verify_dataset_ownership_for_files(...)

# After
await _verify_dataset_ownership_for_files(...)
content = await file.read()
if not content:
    raise InvalidRequestError(...)
```

修法極小但攻擊面顯著縮小：100MB attack → gateway 只要做一次 Dify `get_dataset` (small) 就回 404，request body 完全不讀。FastAPI 的 multipart parser 也不會 buffer 整個 body（streaming）。

Dedicated mode 不受影響：`_verify_dataset_ownership_for_files` 在 dedicated 是 no-op (`return` early)，所以這 reorder 對 dedicated path 不增加任何 cost。

#### 測試

- **新** `TestReview2Fix_OwnershipBeforeFileRead::test_upload_to_foreign_dataset_skips_file_read`：模擬 cross-customer UUID + 大 payload (14KB)，斷言 404 `dataset_not_found` + Dify create-by-file 完全沒被呼叫

> 註：FakeDifyClient 沒有真的讀 multipart，所以這個 test 是行為等價驗證（順序 + Dify call 計數），不是 wall-clock 量測。要更嚴格的 attack 模擬要做 integration test。

---

## 整體決策

- Round 2 後狀態：**進 round 3 確認沒新 second-order bug**
- 全測試 **240 PASSED**（236 → 240：+4 review-2 regression tests）
- Ruff `check .` 全綠
- 修法**完全沒影響 dedicated mode**：所有 PR #1-#3 + dedicated tests 仍綠
- 三個 fix 合成 1 個 commit + docs 1 commit（fixes 在概念上是同一輪 hardening）

## 跨 PR 的 Process 反思

PR #1-#3 + PR #4 累計 9 輪 codex review，**recurring pattern** 已經穩定觀察到 2 個：

1. **Second-order bug**（修了 A 路徑忘了 B/C）— PR #1 r2, PR #2 r2-r3, PR #3 r1-r2
2. **Design contract vs 實作不一致**（comment 寫的 invariant 沒 enforce、strategy 寫了沒用）— PR #4 r1 (customer_id slug), PR #4 r2 (app_name)

這次新觀察的 pattern：

3. **「攻擊面 = 順序」**（valid 流程下 order 對，但攻擊者用 valid input 配 foreign target 時，前面 step 的 cost 被白燒）

對應 future PR 啟示：每加一個 isolation / ownership 驗證，問自己「在這個 check 之前已經做了多少 I/O？攻擊者能不能讓 gateway 在拒絕前先做完那些 I/O？」

## 預備 Round 3 的觀察點

預期 codex round 3 可能會看：

1. 我把 colon 設為 `_APP_PREFIX_SEP` — 跟 dataset 的 `__` 不對稱，要不要統一？
2. 新 length check 用 `len()` — 跟 Dify 的限制是 byte count 還是 char count？
   多 byte UTF-8 字符可能 trip
3. `_DIFY_DATASET_NAME_MAX = 40` 是 hardcoded — Dify 升版可能改，要不要可配置
4. 順手清掉 round 0 的 「skip R2 app_manager」commit message — 那是我當時的判斷，現在已經被 codex 翻盤
