# Review Response: feat-ai-sdk-v3 — Round 3

> Response to `reviews/feat-ai-sdk-v3/review-3.md`.

## Summary

| 嚴重度 | 找到 | 已修 | 不修 |
|---|---|---|---|
| [P1] | 0 | — | — |
| [P2] | 1 | 1 | 0 |

Round 3 收斂結果：0 P1，1 P2 修完。PR #3 三輪 codex review 累計**找到
6 個 finding（1 P1 + 5 P2）全部修完**，**現在可以開 PR**。

## Findings 處理紀錄

---

### Finding 1: [P2] Pass through Dify file/client 4xx statuses

- **Severity**: [P2]
- **Codex 描述**:
  > Dify's Service API can return 415 for `UnsupportedFileTypeError`
  > during `create-by-file` and 403 for disabled dataset API/quota
  > checks in the dataset wrappers; because this set omits those
  > statuses, `_raise_for_dify_status(..., pass_client_errors=True)`
  > turns those client-actionable `/v1/files`/dataset failures into a
  > 502 `dify_upstream_error`.
- **影響檔案**: `gateway/src/gateway/dify/client.py:53`
- **動作**: ✅ Fixed

#### 驗證

Codex 直接 grep Dify source 證實：

- **415 `UnsupportedFileTypeError`**：
  `api/controllers/service_api/dataset/document.py` 在 `create-by-file`
  端點，若 file extension / MIME 不在 allow-list，會 raise
  `UnsupportedFileTypeError` → HTTP 415。例如客戶上傳 `.exe` 或 `.pkl`
  → 415。**這是真客戶錯**（他們選錯檔案），不該被當 gateway 502。

- **403 disabled dataset / quota**：Dify 對 `dataset.disabled = True`
  或 per-tenant quota 不足的情境會回 403。客戶**能採取行動**（換 dataset、
  申請 quota 升級）。

Round 2 我把 `_DATASET_CLIENT_STATUSES = {400, 404, 409, 413, 422}` 設這樣
是參考 PR #2 review-3 對 embeddings 的決定（401/403/429 都 gateway-side）。
**但 dataset/file context 不同**：403 在 Dify 這條路徑語義就是「per-resource
disabled」而不是「auth fail」。Codex 點到的是 **語義細節**：同樣的 HTTP 碼，
在不同 service 路徑代表不同意義，要分開處理。

#### 修復內容

`gateway/src/gateway/dify/client.py:53`：

```python
# Before
_DATASET_CLIENT_STATUSES = frozenset({400, 404, 409, 413, 422})

# After
_DATASET_CLIENT_STATUSES = frozenset(
    {400, 403, 404, 409, 413, 415, 422}
)
```

順手在 docstring 列每個 status 在 dataset context 的意義，避免下個 reviewer
（或未來的我）再次踩同樣的細節 trap：

```python
#   400 — invalid request shape
#   403 — per-dataset disabled / per-tenant quota refused
#   404 — wrong dataset UUID / document id
#   409 — duplicate dataset name
#   413 — file payload too large
#   415 — unsupported file type (create-by-file allow-list)
#   422 — schema validation
```

401/429 仍然是 upstream error（gateway-side credential / rate-limit）。

#### 測試

- 既有 `test_dataset_create_4xx_raises_upstream_client_error` 的 parametrize
  從 `[400, 404, 409, 413, 422]` 改成 `[400, 403, 404, 409, 413, 415, 422]`。
- 既有 `test_dataset_create_non_shape_4xx_still_502` 的 parametrize 從
  `[401, 403, 429]` 改成 `[401, 429]`（403 移出去）。
- 新 `test_create_document_by_file_415_raises_upstream_client_error`：模擬
  上傳 `.exe`，斷言 415 + `upstream_invalid_request` envelope。
- 新 `test_dataset_403_disabled_raises_upstream_client_error`：模擬 disabled
  dataset 上的 delete document，斷言 403 透傳。

---

## 整體決策

- Round 3 後狀態：**PR #3 收斂、可開 PR**
- Round 3 收斂性：0 P1，1 P2 全修，無 deferral
- 全測試 **201 PASSED**（198 → 201：+3 新測試 — 415 case + 403 case + parametrize 加 2）
- Ruff `check .` 維持 **all checks passed**
- 對應 commit：精確的 status set 擴充 + 對應測試擴充，single commit

## PR #3 整體三輪 review 累計成果

| Round | Findings | 已修 |
|---|---|---|
| 1 | 1 P1 + 2 P2 | 3 |
| 2 | 2 P1 + 1 P2 | 3 |
| 3 | 0 P1 + 1 P2 | 1 |
| **總計** | **3 P1 + 4 P2 = 7 findings** | **7（無 deferral）** |

Plus 27 ruff auto-fix lint debt 收回 + 1 F811 duplicate test 修掉。

每輪剩餘問題範圍持續縮小：
- Round 1：**重大邏輯錯**（unwrap response、4xx 分類、cumulative thought）
- Round 2：**CI 自洽性 + 細節邏輯**（lint + silent fallback）
- Round 3：**特定上下文細節**（status 集合涵蓋哪些 code）

這是經典的收斂曲線——next round 應該不會找出新東西。

## Process 教訓總結（記憶已存）

PR #3 三輪 review 確認了兩個 recurring patterns：

1. **「Second-order bug」**：PR #1, #2, #3 round 1-2 都出現過 — 修了 A 路徑但
   相鄰 B/C 路徑沿用舊 pattern。三輪都遇到 → 不是 one-off，是工作流結構問題。
   對策：每個 PR 開工前 grep 上次 review 的 lesson 對應的 helpers，確認新
   code 也套用。
2. **「CI 自抓」**：PR #3 round 2 — 同個 PR 既加 CI 又加 code 時 CI 第一次
   跑就抓自己。對策：寫 CI workflow 後本地跑一遍每個 step 才推。

兩個對策都需要 PR 開工前的 5 分鐘 checklist：
- [ ] grep `_REQUEST_SHAPE_STATUSES`, `_DATASET_CLIENT_STATUSES`, `_unwrap_document`, `_to_*` helpers
- [ ] 如果 PR 改 CI workflow：本地跑 `ruff check .` + `mypy src/` + `pytest`
- [ ] 看每個新 router 是否複製了既有 helper — 如有，套同樣的 status 分類規則

## 下一步

- 把 PR #3 的成果同步進 Notion sub-page
- 開 PR #4 實作（已有 spec on `feat/ai-sdk-gateway-pr4`）
